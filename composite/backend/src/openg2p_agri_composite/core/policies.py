"""Partner policies from Partner Management (PM): which use cases a partner may call.

A partner has one PM **use-case policy** per use case it may call (``use_case`` =
``name@major``): ``GET {partner_mgmt_api_url}/policies/PARTNER_<SENDER>?use_case=<name@major>``
answers that active policy of an active partner, or 404 (unknown or inactive partner,
no such policy: one answer for every case). 404 = not granted.

Cached per pod per (partner, use case), as partner keys are:

* Soft TTL — refresh after min(configured, the answer's ``Cache-Control: max-age``):
  bounds how long a revoked grant keeps working.
* Hard TTL — PM unreachable: the last answer is used up to this age (WARNING), then
  ``PolicyUnavailable`` (the request fails with 503, never "allowed").
* Negative TTL — a 404 (no grant) is remembered briefly.
* Refresh cooldown — at most one PM call per (partner, use case) per cooldown while PM fails.
* Single-flight — concurrent misses for one (partner, use case) make one call.
"""

import asyncio
import logging
import time
from dataclasses import dataclass
from typing import Any, Callable, Dict, List, Optional, Tuple

import httpx

from .crypto import partner_reference_id

_logger = logging.getLogger("agri_composite.policies")


class PolicyUnavailable(Exception):
    """PM could not be read and no usable answer is cached."""


@dataclass
class _Entry:
    policy: Optional[Dict[str, Any]]  # None: PM has no policy for this partner and use case (404)
    known: bool  # False: PM failed and nothing was known (an outage marker)
    fetched_at: float
    last_attempt: float
    ttl: float


def _max_age(cache_control: Optional[str]) -> Optional[int]:
    for part in (cache_control or "").split(","):
        name, _, value = part.strip().partition("=")
        if name.lower() == "max-age":
            try:
                return max(0, int(value.strip().strip('"')))
            except ValueError:
                return None
    return None


def _policies(data: Any) -> List[Dict[str, Any]]:
    """The policy list of a PM answer: ``{"policies": [...]}`` (or a bare list)."""
    items = data.get("policies") if isinstance(data, dict) else data
    return [p for p in (items if isinstance(items, list) else []) if isinstance(p, dict)]


def _is_policy_for(policy: Dict[str, Any], use_case_ref: str) -> bool:
    """An active use-case policy (no ``data_controller``) for ``use_case_ref``."""
    return (not policy.get("data_controller") and policy.get("use_case") == use_case_ref
            and policy.get("status", "active") == "active")


def policy_limits(policy: Dict[str, Any], *, signing_algs: bool = True) -> Dict[str, Any]:
    """The limits of a use-case policy, as the Consent Manager takes them (validate ``limits``,
    consent templates): an empty list or no duration is no limit and is left out;
    ``allow_partner_consents`` and ``allow_assisted_confirmation`` (the partner may collect consents
    by assisted confirmation: national-ID login and one "I agree" in the CM partner portal, no signed
    form, no approver) are always given (absent means false to the CM).
    The policy's purposes are not limits: granting a use case grants its purpose."""
    limits: Dict[str, Any] = {}
    names = ("allowed_subject_id_types", "allowed_signing_algs") if signing_algs else ("allowed_subject_id_types",)
    for name in names:
        values = policy.get(name)
        if isinstance(values, list) and values:
            limits[name] = [str(v) for v in values]
    if policy.get("max_validity_duration"):
        limits["max_validity_duration"] = str(policy["max_validity_duration"])
    limits["allow_partner_consents"] = policy.get("allow_partner_consents") is True
    limits["allow_assisted_confirmation"] = policy.get("allow_assisted_confirmation") is True
    return limits


class PartnerPolicyStore:
    def __init__(
        self,
        api_url: str,
        client_getter: Callable[[], httpx.AsyncClient],
        *,
        soft_ttl: float = 60,
        hard_ttl: float = 21600,
        negative_ttl: float = 30,
        refresh_cooldown: float = 10,
        timeout: float = 3.0,
        clock: Callable[[], float] = time.monotonic,
    ):
        self.api_url = (api_url or "").rstrip("/")
        self._client = client_getter
        self._soft_ttl = soft_ttl
        self._hard_ttl = hard_ttl
        self._negative_ttl = negative_ttl
        self._cooldown = refresh_cooldown
        self._timeout = timeout
        self._clock = clock
        self._cache: Dict[Tuple[str, str], _Entry] = {}  # (PM partner ID, name@major)
        self._flights: Dict[Tuple[str, str], asyncio.Lock] = {}

    # ── use-case grant (each partner call) ───────────────────────────────────

    async def granted(self, partner_id: str, use_case_ref: str) -> bool:
        """The partner has an active PM policy for ``use_case_ref`` (``name@major``).

        Raises ``PolicyUnavailable`` when PM cannot be read and nothing usable is cached."""
        return await self.use_case_policy(partner_id, use_case_ref) is not None

    async def use_case_policy(self, partner_id: str, use_case_ref: str) -> Optional[Dict[str, Any]]:
        """The partner's active policy for ``use_case_ref``, or None (PM has none)."""
        ref = (partner_reference_id(partner_id), use_case_ref)
        entry = self._cache.get(ref)
        if entry is not None and not self._needs_fetch(entry, self._clock()):
            return self._answer(ref, entry)
        lock = self._flights.setdefault(ref, asyncio.Lock())
        async with lock:
            entry = self._cache.get(ref)
            now = self._clock()
            if entry is not None and not self._needs_fetch(entry, now):
                return self._answer(ref, entry)
            return self._answer(ref, await self._fetch(ref, entry, now))

    def invalidate(self, partner_id: Optional[str] = None) -> None:
        if partner_id is None:
            self._cache.clear()
        else:
            pm_id = partner_reference_id(partner_id)
            for key in [k for k in self._cache if k[0] == pm_id]:
                self._cache.pop(key, None)

    def _needs_fetch(self, entry: _Entry, now: float) -> bool:
        if (now - entry.last_attempt) < self._cooldown and entry.last_attempt > entry.fetched_at:
            return False  # PM failed recently: do not call it again before the cooldown
        if not entry.known:
            return True
        ttl = entry.ttl if entry.policy is not None else self._negative_ttl
        return (now - entry.fetched_at) >= ttl

    @staticmethod
    def _answer(ref: Tuple[str, str], entry: _Entry) -> Optional[Dict[str, Any]]:
        if not entry.known:
            raise PolicyUnavailable(f"Partner Management cannot be read for {ref[0]}'s {ref[1]} policy")
        return entry.policy

    async def _fetch(self, ref: Tuple[str, str], prev: Optional[_Entry], now: float) -> _Entry:
        if not self.api_url:
            return self._failed(ref, prev, now, "partner_mgmt_api_url is not configured")
        pm_id, use_case_ref = ref
        try:
            response = await self._client().get(
                f"{self.api_url}/policies/{pm_id}", params={"use_case": use_case_ref}, timeout=self._timeout
            )
        except httpx.HTTPError as e:
            return self._failed(ref, prev, now, f"request failed: {type(e).__name__}")
        if response.status_code == 404:
            entry = _Entry(None, True, now, now, self._negative_ttl)
            self._cache[ref] = entry
            _logger.info("No %s policy for %s in Partner Management (404); cached %ss",
                         use_case_ref, pm_id, self._negative_ttl)
            return entry
        if response.status_code != 200:
            return self._failed(ref, prev, now, f"HTTP {response.status_code}")
        try:
            policies = _policies(response.json())
        except ValueError:
            return self._failed(ref, prev, now, "the answer is not JSON")
        policy = next((p for p in policies if _is_policy_for(p, use_case_ref)), None)
        ttl = self._soft_ttl
        server = _max_age(response.headers.get("cache-control"))
        if server is not None:
            ttl = min(ttl, server)
        entry = _Entry(policy, True, now, now, ttl)
        self._cache[ref] = entry
        return entry

    def _failed(self, ref: Tuple[str, str], prev: Optional[_Entry], now: float, why: str) -> _Entry:
        if prev is not None and prev.known and (now - prev.fetched_at) < self._hard_ttl:
            _logger.warning("Partner Management policy fetch for %s %s failed (%s); using the last answer "
                            "(age %.0fs)", ref[0], ref[1], why, now - prev.fetched_at)
            entry = _Entry(prev.policy, True, prev.fetched_at, now, prev.ttl)
        else:
            _logger.error("Partner Management policy fetch for %s %s failed (%s) and nothing usable is cached",
                          ref[0], ref[1], why)
            entry = _Entry(None, False, prev.fetched_at if prev else 0.0, now, 0)
        self._cache[ref] = entry
        return entry

    # ── listings (console) ───────────────────────────────────────────────────

    async def list_for_use_case(self, use_case_ref: str) -> List[Dict[str, Any]]:
        """Active policies of active partners for ``use_case_ref`` (PM ``GET /policies?use_case=``).

        Not cached (console only). Raises ``PolicyUnavailable`` on any PM failure."""
        if not self.api_url:
            raise PolicyUnavailable("partner_mgmt_api_url is not configured")
        try:
            response = await self._client().get(
                f"{self.api_url}/policies", params={"use_case": use_case_ref}, timeout=self._timeout
            )
        except httpx.HTTPError as e:
            raise PolicyUnavailable(f"Partner Management unreachable: {type(e).__name__}") from None
        if response.status_code == 404:
            return []
        if response.status_code != 200:
            raise PolicyUnavailable(f"HTTP {response.status_code} from Partner Management")
        try:
            policies = _policies(response.json())
        except ValueError:
            raise PolicyUnavailable("Partner Management answer is not JSON") from None
        return [p for p in policies if _is_policy_for(p, use_case_ref) and p.get("partner_id")]
