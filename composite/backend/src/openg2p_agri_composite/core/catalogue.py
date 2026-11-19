"""Each configured registry's data scope catalogue, for the console.

A registry publishes its scopes at ``POST <partner api>/partner/data_scopes``,
a message signed like any partner call (the composite signs as its own partner
ID). The URL defaults to the registry's search URL with ``/dci/...`` replaced by
``/partner/data_scopes``; a registry entry may set ``catalogue_url`` instead.
Answers are cached per registry for ``ttl`` seconds; a failure is cached too
(briefly), so an unreachable registry does not slow every page.
"""

import asyncio
import logging
import time
import uuid
from datetime import datetime, timezone
from typing import Any, Callable, Dict, Optional

_logger = logging.getLogger("agri_composite.catalogue")

CATALOGUE_PATH = "/partner/data_scopes"
FAILURE_TTL_SECONDS = 30


def catalogue_url(endpoint: Dict[str, Any]) -> str:
    if endpoint.get("catalogue_url"):
        return endpoint["catalogue_url"]
    url = (endpoint.get("url") or "").rstrip("/")
    if "/dci/" in url:
        return url.split("/dci/", 1)[0] + CATALOGUE_PATH
    return url.rsplit("/", 1)[0] + CATALOGUE_PATH if "/" in url.split("://", 1)[-1] else url + CATALOGUE_PATH


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


class ScopeCatalogue:
    def __init__(self, registries: Dict[str, Dict[str, Any]], crypto, client_getter: Callable,
                 composite_id: str, ttl: int = 300, timeout: float = 10.0):
        self.registries = registries
        self.crypto = crypto
        self._client = client_getter
        self.composite_id = composite_id
        self.ttl = ttl
        self.timeout = timeout
        self._cache: Dict[str, tuple] = {}  # controller -> (expires_at, result)
        # One fetch per registry at a time (per worker): concurrent page loads and
        # refreshes share it instead of each sending a signed POST.
        self._inflight: Dict[str, asyncio.Future] = {}

    def cached(self, controller: str) -> Optional[Dict[str, Any]]:
        entry = self._cache.get(controller)
        return entry[1] if entry and entry[0] > time.monotonic() else None

    async def fetch(self, controller: str, refresh: bool = False) -> Dict[str, Any]:
        """{"data_controller", "data_scopes": [...], "fetched_at", "error"} (error: a message, scopes [])."""
        if not refresh:
            hit = self.cached(controller)
            if hit is not None:
                return hit
        pending = self._inflight.get(controller)
        if pending is not None:
            return await asyncio.shield(pending)
        future = asyncio.get_running_loop().create_future()
        self._inflight[controller] = future
        try:
            result = await self._fetch(controller, self.registries[controller])
            ttl = self.ttl if result["error"] is None else min(self.ttl, FAILURE_TTL_SECONDS)
            self._cache[controller] = (time.monotonic() + ttl, result)
            future.set_result(result)
            return result
        except BaseException as e:
            future.set_exception(e)
            future.exception()  # retrieved: no "never retrieved" warning when nobody else waited
            raise
        finally:
            self._inflight.pop(controller, None)

    async def _fetch(self, controller: str, endpoint: Dict[str, Any]) -> Dict[str, Any]:
        url = catalogue_url(endpoint)
        header = {"sender_id": self.composite_id, "message_id": str(uuid.uuid4()), "message_ts": _now_iso(),
                  "receiver_id": endpoint.get("receiver_id") or controller}
        message: Dict[str, Any] = {}
        result = {"data_controller": None, "data_scopes": [], "fetched_at": _now_iso(), "error": None}
        try:
            signature = await self.crypto.sign_detached({"header": header, "message": message})
        except Exception as e:
            result["error"] = f"the composite cannot sign the request: {e}"
            return result
        try:
            response = await self._client().post(
                url, json={"signature": signature, "header": header, "message": message}, timeout=self.timeout)
        except Exception as e:
            _logger.warning("Data scope catalogue of %s (%s) unreachable: %r", controller, url, e)
            result["error"] = f"unreachable: {type(e).__name__}"
            return result
        try:
            body = response.json()
        except ValueError:
            body = None
        if response.status_code != 200 or not isinstance(body, dict):
            result["error"] = f"HTTP {response.status_code} from {url}"
            return result
        if body.get("error_code"):
            result["error"] = f"{body.get('error_code')}: {body.get('error_message') or ''}".strip()
            return result
        result["data_controller"] = body.get("data_controller")
        result["data_scopes"] = [s for s in body.get("data_scopes") or [] if isinstance(s, dict)]
        return result
