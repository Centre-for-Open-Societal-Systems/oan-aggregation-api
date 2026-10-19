"""Calls a registry's staff-api (the staff portal's backend) for the staff search.

Not the partner path: no consent object, no signature, no Consent Manager. The
staff-api checks only the caller's permissions, and the caller is this
service's own Keycloak service account (``staff_api_client_id``) with a
view-only role on the registry's staff-portal client. IAM turns that role into
``register:view`` etc., exactly as for a staff user in the portal.

The staff search is two calls from the client, as in the staff portal:

- **search**: ``/register-data/search_in_a_register`` per register, with the
  foundational ID as ``search_text`` (a substring search; hits are checked
  exactly by the caller, ``RegistryEntry.staff_hit_identifies``). The hit is
  the list row: ids, record name, display fields. Plus the register's tabs.
- **tab**: ``/register-data/get_tab_records`` for one record and one tab, when
  the user opens that tab, plus the tab's sections (labels, fields).

Tabs and sections are the registry's own metadata
(``/register-tab-metadata/get_all_tabs``, ``/register-tab-metadata/get_sections``),
read here and cached for ``staff_api_metadata_ttl_sec``: a tab or section
added in a registry reaches the client without a change to this service.

Every staff-api POST needs the double-submit CSRF pair: the same random value
in the ``X-CSRF-Token`` cookie and header (iam_core's CsrfMiddleware).

Under load: one pooled HTTP client for all calls (keep-alive), at most
``staff_api_max_concurrency`` calls in flight per registry, the service token
reused until shortly before it expires.
"""
import asyncio
import logging
import os
import secrets
import time
import uuid
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple

import httpx
from openg2p_fastapi_common.service import BaseService

from ..config import Settings
from ..registry_catalog import RegistryEntry
from .registry_client import RegistryError

_config = Settings.get_config()
_logger = logging.getLogger(_config.logging_default_logger_name)

_CSRF = "X-CSRF-Token"   # iam_core: cookie and header share the name
# An unknown tab id refreshes the tab list early, but not more often than this.
_MIN_REFRESH_SEC = 30


class _TtlCache:
    """``key -> value`` for ``ttl`` seconds; remembers when each key was loaded."""

    def __init__(self):
        self._items: Dict[Any, Tuple[float, Any]] = {}

    def get(self, key, ttl: float):
        item = self._items.get(key)
        if item and time.monotonic() - item[0] < ttl:
            return item[1]
        return None

    def age(self, key) -> float:
        item = self._items.get(key)
        return time.monotonic() - item[0] if item else float("inf")

    def put(self, key, value):
        self._items[key] = (time.monotonic(), value)
        return value


class StaffApiClient(BaseService):
    def __init__(self, name="", **kwargs):
        super().__init__(name if name else "StaffApiClient", **kwargs)
        self._init_state()

    def _init_state(self):
        # (token_url, client_id) -> (token, expires_at)
        self._tokens: Dict[Tuple[str, str], Tuple[str, float]] = {}
        # registry -> {register_mnemonic: register_id}
        self._registers_cache = _TtlCache()
        # (registry, register_id) -> [tab, ...] (active, in tab_order)
        self._tabs_cache = _TtlCache()
        # (registry, register_id, tab_id) -> [tab section, ...] (in section_order)
        self._sections_cache = _TtlCache()
        self._http: Optional[httpx.AsyncClient] = None
        self._slots: Dict[str, asyncio.Semaphore] = {}

    # ── plumbing ────────────────────────────────────────────────────────────

    def _client(self) -> httpx.AsyncClient:
        if self._http is None:
            self._http = httpx.AsyncClient(
                timeout=_config.staff_api_timeout,
                limits=httpx.Limits(max_connections=max(_config.staff_api_max_concurrency * 4, 20),
                                    max_keepalive_connections=_config.staff_api_max_concurrency))
        return self._http

    def _slot(self, registry: str) -> asyncio.Semaphore:
        if registry not in self._slots:
            self._slots[registry] = asyncio.Semaphore(max(_config.staff_api_max_concurrency, 1))
        return self._slots[registry]

    # ── service account ─────────────────────────────────────────────────────

    def _credentials(self, registry: str, entry: RegistryEntry) -> Tuple[str, str, str]:
        auth = entry.staff_api.auth
        if auth is not None:
            return auth.token_url, auth.client_id, os.environ.get(auth.client_secret_env, "")
        if not _config.staff_api_token_url:
            raise RegistryError(registry, "staff_api_not_configured",
                                "set AGGREGATION_LAYER_STAFF_API_TOKEN_URL or staff_api.auth")
        return (_config.staff_api_token_url, _config.staff_api_client_id,
                _config.staff_api_client_secret)

    async def _bearer(self, registry: str, entry: RegistryEntry) -> str:
        """Client-credentials token, cached until 30s before it expires."""
        token_url, client_id, secret = self._credentials(registry, entry)
        cached = self._tokens.get((token_url, client_id))
        if cached and time.time() < cached[1] - 30:
            return cached[0]
        try:
            response = await self._client().post(token_url, data={
                "grant_type": "client_credentials",
                "client_id": client_id,
                "client_secret": secret,
            })
            response.raise_for_status()
            payload = response.json()
        except Exception as exc:  # noqa: BLE001
            raise RegistryError(registry, "staff_auth_failed",
                                "could not get a service token: %s" % exc) from exc
        token = payload.get("access_token")
        if not token:
            raise RegistryError(registry, "staff_auth_failed", "no access_token in reply")
        self._tokens[(token_url, client_id)] = (
            token, time.time() + int(payload.get("expires_in") or 60))
        return token

    # ── one call ────────────────────────────────────────────────────────────

    async def _post(self, registry: str, entry: RegistryEntry, path: str,
                    payload: Dict[str, Any], page_size: int = 100,
                    search_text: Optional[str] = None) -> Any:
        """POST one staff-api request; return ``response_body.response_payload``
        (first page only — lists go through ``_post_all``)."""
        return (await self._post_page(registry, entry, path, payload, page_size,
                                      search_text))[0]

    async def _post_page(self, registry: str, entry: RegistryEntry, path: str,
                         payload: Dict[str, Any], page_size: int = 100,
                         search_text: Optional[str] = None,
                         current_page: int = 1) -> Tuple[Any, Optional[Dict[str, Any]]]:
        """One page: ``(response_payload, pagination_response)``; the latter
        is ``{number_of_items, number_of_pages}`` or None."""
        slot = self._slot(registry)
        try:
            await asyncio.wait_for(slot.acquire(), timeout=_config.staff_api_queue_timeout_sec)
        except asyncio.TimeoutError as exc:
            raise RegistryError(registry, "registry_busy",
                                "too many staff-api calls in flight to this registry") from exc
        try:
            return await self._post_now(registry, entry, path, payload, page_size, search_text,
                                        current_page)
        finally:
            slot.release()

    async def _post_all(self, registry: str, entry: RegistryEntry, path: str,
                        payload: Dict[str, Any], page_size: int = 100,
                        search_text: Optional[str] = None) -> Tuple[List[Any], bool]:
        """Every page of a list call, up to ``staff_api_max_pages``:
        ``(rows, complete)``; ``complete`` is False when pages were left unread.
        Without a pagination_response the first page is taken as the whole list."""
        rows: List[Any] = []
        max_pages = max(1, _config.staff_api_max_pages)
        for page in range(1, max_pages + 1):
            payload_rows, pagination = await self._post_page(
                registry, entry, path, payload, page_size, search_text, current_page=page)
            rows.extend(payload_rows or [])
            pages = (pagination or {}).get("number_of_pages") or 0
            if page >= pages or not payload_rows:
                return rows, True
        _logger.warning("Staff API %s %s: read %d of %s pages (staff_api_max_pages); "
                        "the list is incomplete", registry, path, max_pages, pages)
        return rows, False

    async def _post_now(self, registry, entry, path, payload, page_size, search_text,
                        current_page=1):
        token = await self._bearer(registry, entry)
        csrf = secrets.token_urlsafe(24)
        pagination: Dict[str, Any] = {"current_page": current_page, "page_size": page_size}
        if search_text is not None:
            pagination["search_text"] = search_text
        body = {
            "request_header": {
                "sender_app_mnemonic": _config.aggregator_sender_id,
                "sender_app_url": "aggregation-layer",
                "request_id": uuid.uuid4().hex,
                "request_timestamp": datetime.now(timezone.utc).isoformat(),
            },
            "request_body": {"request_payload": payload, "pagination_request": pagination},
        }
        url = entry.staff_api.base_url.rstrip("/") + path
        timeout = entry.staff_api.timeout_sec or _config.staff_api_timeout
        try:
            # The CSRF cookie goes as a header: the pooled client keeps no cookies.
            response = await self._client().post(url, json=body, timeout=timeout, headers={
                "Authorization": "Bearer " + token, _CSRF: csrf,
                "Cookie": "%s=%s" % (_CSRF, csrf)})
        except Exception as exc:  # network / timeout
            _logger.exception("Staff API of '%s' unreachable at %s", registry, url)
            raise RegistryError(registry, "unreachable", str(exc)) from exc

        if response.status_code in (401, 403):
            # A token the staff-api or IAM refuses, or a role without the
            # permission: name it, it is a deployment problem, not "no data".
            raise RegistryError(registry, "staff_api_forbidden",
                                "HTTP %s from %s: %s" % (response.status_code, path,
                                                         _error_message(response)))
        if response.status_code != 200:
            raise RegistryError(registry, "http_error",
                                "HTTP %s from %s: %s" % (response.status_code, path,
                                                         _error_message(response)))
        try:
            data = response.json()
        except Exception as exc:
            raise RegistryError(registry, "bad_response", "non-JSON body from %s" % path) from exc
        header = data.get("response_header") or {}
        if header.get("response_status") not in (None, "SUCCESS"):
            raise RegistryError(registry, header.get("response_error_code") or "error",
                                header.get("response_error_message") or "")
        body = data.get("response_body") or {}
        return body.get("response_payload"), body.get("pagination_response")

    # ── metadata (cached, TTL) ──────────────────────────────────────────────

    async def registers(self, registry: str, entry: RegistryEntry) -> Dict[str, str]:
        """``{register_mnemonic: register_id}`` for this registry."""
        ttl = _config.staff_api_metadata_ttl_sec
        cached = self._registers_cache.get(registry, ttl)
        if cached is not None:
            return cached
        rows, _ = await self._post_all(registry, entry, "/register-metadata/get_all_registers",
                                       {}, page_size=100)
        return self._registers_cache.put(registry, {
            r.get("register_mnemonic"): r.get("register_id")
            for r in rows if r.get("register_mnemonic") and r.get("register_id")})

    async def register_ids(self, registry: str, entry: RegistryEntry) -> Dict[str, str]:
        """``{register_mnemonic: register_id}`` for the catalog's ``staff_api.registers``."""
        ids = await self.registers(registry, entry)
        missing = [m for m in entry.staff_api.registers if m not in ids]
        if missing:
            raise RegistryError(registry, "unknown_register",
                                "register(s) %s not found in the staff-api" % ", ".join(missing))
        return {m: ids[m] for m in entry.staff_api.registers}

    async def tabs(self, registry: str, entry: RegistryEntry, register_id: str,
                   refresh: bool = False) -> List[Dict[str, Any]]:
        """The register's active tabs, in ``tab_order``."""
        key = (registry, register_id)
        cached = None if refresh else self._tabs_cache.get(key, _config.staff_api_metadata_ttl_sec)
        if cached is not None:
            return cached
        rows, _ = await self._post_all(registry, entry, "/register-tab-metadata/get_all_tabs",
                                       {"register_id": register_id}, page_size=100)
        return self._tabs_cache.put(key, sorted(
            (t for t in rows if t.get("tab_id") and t.get("is_active", True)),
            key=lambda t: t.get("tab_order") or 0))

    async def find_tab(self, registry: str, entry: RegistryEntry, register_id: str,
                       tab_id: str) -> Optional[Dict[str, Any]]:
        """The tab, re-reading the tab list once if it is not (yet) in the cache."""
        tab = _by_id(await self.tabs(registry, entry, register_id), tab_id)
        if tab is None and self._tabs_cache.age((registry, register_id)) > _MIN_REFRESH_SEC:
            tab = _by_id(await self.tabs(registry, entry, register_id, refresh=True), tab_id)
        return tab

    async def tab_sections(self, registry: str, entry: RegistryEntry, register_id: str,
                           tab_id: str) -> List[Dict[str, Any]]:
        """The tab's sections in ``section_order``, each with its ``section_data``
        (mnemonic, description, is_list, section_register_id, section_ui_schema)."""
        key = (registry, register_id, tab_id)
        cached = self._sections_cache.get(key, _config.staff_api_metadata_ttl_sec)
        if cached is not None:
            return cached
        rows, _ = await self._post_all(registry, entry, "/register-tab-metadata/get_sections",
                                       {"tab_id": tab_id}, page_size=100)
        return self._sections_cache.put(key, sorted(
            (r for r in rows if r.get("register_id") in (None, register_id)),
            key=lambda r: r.get("section_order") or 0))

    # ── data ────────────────────────────────────────────────────────────────

    async def search(self, registry: str, entry: RegistryEntry,
                     foundational_id: str) -> Tuple[List[Dict[str, Any]], bool]:
        """Every record of ``foundational_id`` in this registry's staff_api registers.

        ``([{register, registerId, hit}], complete)``, ``hit`` being the
        staff-api list row. The search is a substring match, so every page is
        read (up to ``staff_api_max_pages``): an exact match may sit on a later
        page. ``complete`` is False when pages were left unread. Raises
        RegistryError on anything but an answer, so the caller can report one
        registry's failure and keep the others.
        """
        ids = await self.register_ids(registry, entry)
        search_value = entry.search_value(foundational_id)
        page_size = entry.staff_api.page_size or _config.aggregator_page_size
        complete = True

        async def one(mnemonic: str, register_id: str):
            nonlocal complete
            hits, all_read = await self._post_all(
                registry, entry, "/register-data/search_in_a_register",
                {"register_id": register_id}, page_size=page_size,
                search_text=search_value)
            complete = complete and all_read
            exact = [h for h in hits if entry.staff_hit_identifies(h, foundational_id)
                     and h.get("internal_record_id")]
            if len(hits) != len(exact):
                _logger.info("Staff search %s/%s: %d of %d hit(s) dropped (no exact "
                             "foundational ID)", registry, mnemonic,
                             len(hits) - len(exact), len(hits))
            return [{"register": mnemonic, "registerId": register_id, "hit": h} for h in exact]

        per_register = await asyncio.gather(*[one(m, r) for m, r in ids.items()])
        return [record for records in per_register for record in records], complete

    async def tab_records(self, registry: str, entry: RegistryEntry, register_id: str,
                          record_id: str, tab_id: str) -> List[Dict[str, Any]]:
        """``get_tab_records``: ``[{section_register_id, is_list, records}]``."""
        return await self._post(registry, entry, "/register-data/get_tab_records", {
            "subject_register_id": register_id, "subject_record_id": record_id,
            "tab_id": tab_id}) or []


def _by_id(tabs: List[Dict[str, Any]], tab_id: str) -> Optional[Dict[str, Any]]:
    return next((t for t in tabs if t.get("tab_id") == tab_id), None)


def _error_message(response: httpx.Response) -> str:
    try:
        data = response.json()
    except Exception:  # noqa: BLE001
        return response.text[:200]
    header = data.get("response_header") if isinstance(data, dict) else None
    if isinstance(header, dict) and header.get("response_error_message"):
        return header["response_error_message"]
    if isinstance(data, dict) and data.get("errors"):
        return "; ".join(str(e.get("message")) for e in data["errors"] if isinstance(e, dict))
    return str(data)[:200]
