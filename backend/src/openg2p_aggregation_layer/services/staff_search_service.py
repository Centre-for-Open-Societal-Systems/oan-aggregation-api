"""Staff search: one beneficiary, every registry, no consent; tabs on demand.

A registry staff user (realm ``staff`` token, one of ``staff_search_roles``)
looks a beneficiary up by foundational ID. This service calls each registry's
**staff-api** with its own service account and answers synchronously. Two
calls, as in the staff portal (lazy loading):

``POST /aggregation/v1/staff/registry/search``
    Per registry: found / not_found / error; per record found, the registry's
    list row as a summary and the record's tabs, each with the ready request
    that opens it. One staff-api call per register; tabs come from a cache.
``POST /aggregation/v1/staff/registry/tab``
    One tab of one record from that search: its sections (label, fields) and
    rows. One staff-api call; the tab's section metadata comes from a cache.

Tabs, sections and fields are the registry's own metadata, never listed
here: a tab added in a registry reaches the client by itself.
``staff_api.hidden_tabs`` in the catalog keeps a tab away from staff.

What is deliberately absent, compared with the partner path:

- no consent object, no Consent Manager call, no consent request, no OTP: the
  staff-api's own permission check (IAM roles) is the authority at each
  registry, the same one the staff portal relies on;
- no partner callback: the answer is the HTTP response.

What replaces it: the role check at this service's door, an audit log line
per search and per tab opened (logger ``<logging_default_logger_name>.audit``, i.e. ``app.audit``, one JSON
object each), and the binding of a tab request to its search: only a record
that search returned, only for the staff user who ran it, only for
``staff_search_ttl_sec``.

Nothing is stored. The ``searchId`` IS that binding: a ticket carrying the
staff user, the records found and its expiry, with a MAC by this service's
key (``CryptoService.mac_key``). A tab request is checked against the ticket
alone, so any worker or pod can answer it.

Registries are called in parallel. One registry failing does not fail the
search: its entry carries ``status: error`` and the reason, the others answer.
"""
import asyncio
import base64
import hashlib
import hmac
import json
import logging
import math
import time
import uuid
from collections import deque
from datetime import datetime, timedelta, timezone
from typing import Any, Deque, Dict, List, Optional

from openg2p_fastapi_common.service import BaseService

from ..auth import CallerIdentity
from ..config import Settings
from ..registry_catalog import get_catalog
from .aggregator_service import AggregationError
from .crypto_service import CryptoService
from .registry_client import RegistryError
from .staff_api_client import StaffApiClient

_config = Settings.get_config()
_logger = logging.getLogger(_config.logging_default_logger_name)
_audit = logging.getLogger(_config.logging_default_logger_name + ".audit")

TAB_PATH = "/aggregation/v1/staff/registry/tab"


class StaffSearchService(BaseService):
    def __init__(self, name="", **kwargs):
        super().__init__(name if name else "StaffSearchService", **kwargs)
        self.staff_api = StaffApiClient.get_component()
        self._mac_key = CryptoService.get_component().mac_key()
        self._calls: Dict[str, Deque[float]] = {}

    @staticmethod
    def allowed_roles(identity: CallerIdentity) -> List[str]:
        """The staff_search_roles the caller holds (empty: not allowed)."""
        return sorted(set(identity.roles) & set(_config.staff_search_roles))

    def check_rate(self, identity: CallerIdentity):
        """429 past ``staff_rate_limit_per_minute`` requests by this user (this worker)."""
        limit = _config.staff_rate_limit_per_minute
        if limit <= 0:
            return
        calls = self._calls.setdefault(identity.subject or "unknown", deque())
        now = time.monotonic()
        while calls and now - calls[0] >= 60:
            calls.popleft()
        if len(calls) >= limit:
            raise AggregationError(429, "rate_limited",
                                   "more than %d staff requests in a minute" % limit)
        calls.append(now)

    def registries_for(self, registry_filter: Optional[List[str]]) -> List[str]:
        catalog = get_catalog()
        available = catalog.staff_codes()
        if not available:
            raise AggregationError(503, "staff_search_not_configured",
                                   "no registry in the catalog has a staff_api")
        if not registry_filter:
            return available
        unknown = sorted(set(registry_filter) - set(available))
        if unknown:
            raise AggregationError(
                400, "unknown_registry",
                "not available to the staff search: %s (available: %s)"
                % (", ".join(unknown), ", ".join(available)))
        return [code for code in available if code in registry_filter]

    # ── search ──────────────────────────────────────────────────────────────

    async def search(self, identity: CallerIdentity, foundational_id: str,
                     registry_filter: Optional[List[str]] = None,
                     purpose: Optional[str] = None) -> Dict[str, Any]:
        foundational_id = (foundational_id or "").strip()
        if not foundational_id:
            raise AggregationError(400, "missing_foundational_id", "foundationalId is required")
        codes = self.registries_for(registry_filter)
        catalog = get_catalog()
        nonce = uuid.uuid4().hex
        results = await asyncio.gather(*[
            self._one(code, catalog.get(code), foundational_id) for code in codes])

        searched_at = datetime.now(timezone.utc)
        expires_at = searched_at + timedelta(seconds=_config.staff_search_ttl_sec)
        # One ticket per record, so its size never grows with the number of
        # records found: each opens only that record's tabs. The search's own
        # searchId names the search (and its expiry) but opens nothing.
        base = {"n": nonce, "sub": identity.subject or "unknown",
                "exp": int(expires_at.timestamp())}
        search_id = self._ticket(base)
        for r in results:
            for record in r["records"]:
                ticket = self._ticket(
                    base | {"r": [r["registryCode"], record["registerId"], record["recordId"]]})
                record["searchId"] = ticket
                for t in record["tabs"]:
                    t["request"]["body"]["searchId"] = ticket

        claims = identity.raw_claims or {}
        outcome = {r["registryCode"]: {k: r[k] for k in ("status", "error") if r.get(k)}
                   | {"records": len(r["records"])} for r in results}
        _audit.info(json.dumps({
            "event": "staff_search", "search": nonce,
            "staff_subject": identity.subject or "unknown",
            "staff_username": claims.get("preferred_username"),
            "staff_name": identity.name, "staff_roles": self.allowed_roles(identity),
            "foundational_id": _masked(foundational_id), "registries": codes,
            "purpose": purpose, "outcome": outcome}, default=str))

        return {
            "searchId": search_id,
            "foundationalId": foundational_id,
            "searchedAt": searched_at.isoformat(),
            "searchedBy": claims.get("preferred_username") or identity.subject,
            "expiresAt": expires_at.isoformat(),
            "purpose": purpose,
            "registries": results,
        }

    async def _one(self, code: str, entry, foundational_id: str) -> Dict[str, Any]:
        base = {"registryCode": code, "registryName": entry.name}
        try:
            found, complete = await self.staff_api.search(code, entry, foundational_id)
            tabs = {}
            for register_id in {f["registerId"] for f in found}:
                tabs[register_id] = [t for t in await self.staff_api.tabs(code, entry, register_id)
                                     if entry.staff_api.shows_tab(t.get("tab_id"))]
        except RegistryError as exc:
            _logger.warning("Staff search: registry %s failed: %s %s",
                            code, exc.reason, exc.detail)
            return {**base, "status": "error", "records": [],
                    "error": {"reason": exc.reason, "detail": exc.detail}}
        except Exception as exc:  # noqa: BLE001 - one registry must not sink the rest
            _logger.exception("Staff search: registry %s failed unexpectedly", code)
            return {**base, "status": "error", "records": [],
                    "error": {"reason": "internal_error", "detail": str(exc)}}
        # The searchId is filled into each tab request once the ticket exists.
        records = [_record("", code, f, tabs[f["registerId"]]) for f in found]
        # Pages left unread: no record is not proof of absence.
        status = "found" if records else ("not_found" if complete else "incomplete")
        return {**base, "status": status, "records": records, "complete": complete}

    # ── tab ─────────────────────────────────────────────────────────────────

    async def tab(self, identity: CallerIdentity, search_id: str, registry_code: str,
                  register_id: str, record_id: str, tab_id: str, page: int = 1,
                  page_size: Optional[int] = None) -> Dict[str, Any]:
        search = self._search_of(identity, search_id)
        entry = get_catalog().get(registry_code)
        bound = search.get("r")
        if bound is not None:
            # A record's ticket: exactly that record.
            in_search = bound[0] == registry_code
            in_found = in_search and bound[1:] == [register_id, record_id]
        elif "found" in search:
            # A ticket issued before per-record tickets (still within its TTL).
            found = search["found"].get(registry_code)
            in_search = found is not None
            in_found = in_search and [register_id, record_id] in found
        else:
            raise AggregationError(400, "record_not_in_search",
                                   "use the searchId from the record's tab request")
        if entry is None or entry.staff_api is None or not in_search:
            raise AggregationError(400, "unknown_registry",
                                   "registry %s was not part of this search" % registry_code)
        if not in_found:
            raise AggregationError(400, "record_not_in_search",
                                   "this search did not return that record")
        size = min(page_size or _config.staff_tab_page_size, _config.staff_tab_max_page_size)

        claims = identity.raw_claims or {}
        audit = {"event": "staff_tab_view", "search": search["n"],
                 "staff_subject": identity.subject or "unknown",
                 "staff_username": claims.get("preferred_username"),
                 "registry_code": registry_code, "register_id": register_id,
                 "record_id": record_id, "tab_id": tab_id}
        try:
            tab = await self.staff_api.find_tab(registry_code, entry, register_id, tab_id)
            if tab is None or not entry.staff_api.shows_tab(tab_id):
                raise AggregationError(400, "unknown_tab",
                                       "register %s has no tab %s" % (register_id, tab_id))
            metas, groups, names = await asyncio.gather(
                self.staff_api.tab_sections(registry_code, entry, register_id, tab_id),
                self.staff_api.tab_records(registry_code, entry, register_id, record_id, tab_id),
                self.staff_api.registers(registry_code, entry))
        except RegistryError as exc:
            _log_audit(audit, {"status": "error", "error": {"reason": exc.reason,
                                                            "detail": exc.detail}})
            raise AggregationError(502, exc.reason, exc.detail) from exc
        except AggregationError as exc:
            _log_audit(audit, {"status": "error", "error": {"reason": exc.reason}})
            raise

        sections = _sections(metas, groups, {v: k for k, v in names.items()}, page, size)
        _log_audit(audit, {"status": "ok", "sections": len(sections),
                           "records": sum(len(s["records"]) for s in sections)})
        return {
            "searchId": search_id,
            "registryCode": registry_code,
            "registerId": register_id,
            "recordId": record_id,
            "tabId": tab_id,
            "label": tab.get("tab_label") or tab_id,
            "sections": sections,
        }

    # ── the search ticket ───────────────────────────────────────────────────

    def _mac(self, body: str) -> str:
        digest = hmac.new(self._mac_key, body.encode("ascii"), hashlib.sha256).digest()
        return _b64(digest)

    def _ticket(self, payload: Dict[str, Any]) -> str:
        body = _b64(json.dumps(payload, separators=(",", ":"), sort_keys=True).encode("utf-8"))
        return body + "." + self._mac(body)

    def _search_of(self, identity: CallerIdentity, search_id: str) -> Dict[str, Any]:
        """The search a ticket stands for, if it is genuine, the caller's and fresh."""
        body, _, mac = (search_id or "").partition(".")
        search = None
        if body and mac and hmac.compare_digest(mac, self._mac(body)):
            try:
                search = json.loads(base64.urlsafe_b64decode(body + "=" * (-len(body) % 4)))
            except ValueError:
                search = None
        # Someone else's search is reported as missing, not as forbidden.
        if (not isinstance(search, dict)
                or search.get("sub") != (identity.subject or "unknown")
                or ("found" in search and not isinstance(search["found"], dict))
                or ("r" in search and not (isinstance(search["r"], list)
                                           and len(search["r"]) == 3))):
            raise AggregationError(404, "search_not_found", "no such search for this user")
        if time.time() > int(search.get("exp") or 0):
            raise AggregationError(410, "search_expired", "search again to open tabs")
        return search


# ── building the answers ────────────────────────────────────────────────────


def _record(search_id: str, code: str, found: Dict[str, Any],
            tabs: List[Dict[str, Any]]) -> Dict[str, Any]:
    hit = found["hit"]
    record_id = hit["internal_record_id"]
    summary = {
        "functionalId": hit.get("functional_record_id"),
        "name": hit.get("record_name"),
        "foundationalId": hit.get("foundational_id"),
        "linkFoundationalId": hit.get("link_foundational_id"),
        "imageUrl": hit.get("record_image_url"),
        "createdAt": _text(hit.get("created_at")),
        "lastApprovedAt": _text(hit.get("last_approved_at")),
        "displayFields": [{"name": f.get("field_name"), "value": f.get("value"),
                           "order": f.get("order") or 0}
                          for f in hit.get("display_fields") or [] if f.get("field_name")],
    }
    return {
        "register": found["register"],
        "registerId": found["registerId"],
        "recordId": record_id,
        "summary": summary,
        "tabs": [{
            "tabId": t["tab_id"],
            "label": t.get("tab_label") or t["tab_id"],
            "order": t.get("tab_order") or 0,
            "request": {"method": "POST", "url": TAB_PATH, "body": {
                "searchId": search_id, "registryCode": code,
                "registerId": found["registerId"], "recordId": record_id,
                "tabId": t["tab_id"]}},
        } for t in tabs],
    }


def _sections(metas: List[Dict[str, Any]], groups: List[Dict[str, Any]],
              register_names: Dict[str, str], page: int, size: int) -> List[Dict[str, Any]]:
    """The tab's sections in the registry's order, each with its rows.

    ``get_tab_records`` answers per section register; several sections can
    show the same register (e.g. identity and address of the main record),
    each with its own fields. A group no section metadata describes is still
    passed on, without fields, so no row is lost to a metadata gap.
    """
    rows = {g.get("section_register_id"): g for g in groups if g.get("section_register_id")}
    used = set()
    out: List[Dict[str, Any]] = []
    for i, meta in enumerate(metas):
        data = meta.get("section_data") or meta
        section_register_id = data.get("section_register_id")
        if not section_register_id:
            continue
        group = rows.get(section_register_id) or {}
        used.add(section_register_id)
        out.append(_section(
            section_id=data.get("section_id") or meta.get("section_id") or section_register_id,
            mnemonic=data.get("section_mnemonic"), label=data.get("section_description"),
            order=meta.get("section_order") if meta.get("section_order") is not None else i,
            is_list=bool(data.get("is_list", group.get("is_list"))),
            register_id=section_register_id, register=register_names.get(section_register_id),
            fields=_fields(data.get("section_ui_schema"), section_register_id),
            records=group.get("records") or [], page=page, size=size))
    last = max([s["order"] for s in out], default=0)
    for section_register_id, group in rows.items():
        if section_register_id in used:
            continue
        last += 1
        out.append(_section(
            section_id=section_register_id, mnemonic=None,
            label=register_names.get(section_register_id), order=last,
            is_list=bool(group.get("is_list")), register_id=section_register_id,
            register=register_names.get(section_register_id), fields=[],
            records=group.get("records") or [], page=page, size=size))
    return out


def _section(*, section_id, mnemonic, label, order, is_list, register_id, register,
             fields, records, page, size) -> Dict[str, Any]:
    pagination = None
    if is_list:
        total = len(records)
        pagination = {"page": page, "pageSize": size, "totalItems": total,
                      "totalPages": max(math.ceil(total / size), 1)}
        records = records[(page - 1) * size: page * size]
    return {"sectionId": section_id, "mnemonic": mnemonic, "label": label, "order": order,
            "isList": is_list, "registerId": register_id, "register": register,
            "fields": fields, "records": records, "pagination": pagination}


def _fields(ui_schema: Any, section_register_id: str) -> List[Dict[str, Any]]:
    """Fields of a section from its UI schema, in display order.

    The registry's schema nests panels of widgets. A widget bound to data has
    ``widget-data-path`` (``<register_id>.<field>``) and ``widget-label``; a
    composite widget (header, geo hierarchy) has a ``{part: path}`` map
    instead; a table widget lists its columns in ``widget-data-columns`` with
    paths relative to a row. The key returned is the field's key in a record.
    """
    out: List[Dict[str, Any]] = []
    seen = set()
    prefix = section_register_id + "."

    def add_one(widget: Dict[str, Any], path: Any, label: Optional[str]):
        if not isinstance(path, str) or not path:
            return
        key = path[len(prefix):] if path.startswith(prefix) else path
        if key in seen or key == "records" or key.endswith(".records"):
            return
        seen.add(key)
        field = {"key": key, "label": label, "type": widget.get("widget"), "order": len(out)}
        source = widget.get("widget-data-source")
        if isinstance(source, dict) and source.get("type") == "static" and isinstance(
                source.get("options"), list):
            field["options"] = [{"label": o.get("label"), "value": o.get("value")}
                                for o in source["options"] if isinstance(o, dict)]
        out.append(field)

    def add(widget: Dict[str, Any]):
        path, label = widget.get("widget-data-path"), widget.get("widget-label")
        if not isinstance(path, dict):
            add_one(widget, path, label)
            return
        names = widget.get("widget-labels") if isinstance(widget.get("widget-labels"), dict) else {}
        for part, part_path in path.items():
            add_one(widget, part_path, names.get(part) or (
                label if part == "value" else ("%s (%s)" % (label, part) if label else part)))

    def walk(node: Any):
        if isinstance(node, dict):
            columns = node.get("widget-data-columns")
            if isinstance(columns, list):
                for column in columns:
                    if isinstance(column, dict):
                        add(column)
                return
            if "widget" in node:
                add(node)
            for value in node.values():
                if isinstance(value, (dict, list)):
                    walk(value)
        elif isinstance(node, list):
            for item in node:
                walk(item)

    walk(ui_schema)
    return out


def _log_audit(audit: Dict[str, Any], outcome: Dict[str, Any]) -> None:
    _audit.info(json.dumps({**audit, "outcome": outcome}, default=str))


def _b64(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def _text(value: Any) -> Optional[str]:
    return None if value is None else str(value)


def _masked(value: str) -> str:
    """The ID as it goes to the log: last four characters only."""
    return ("*" * max(len(value) - 4, 0)) + value[-4:]
