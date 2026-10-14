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

What replaces it: the role check at this service's door, an audit row per
search and per tab opened (``models/staff_search.py``), and the binding of a
tab request to its search: only a record that search returned, only for the
staff user who ran it, only for ``staff_search_ttl_sec``.

Registries are called in parallel. One registry failing does not fail the
search: its entry carries ``status: error`` and the reason, the others answer.
"""
import asyncio
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
from ..db import async_session
from ..models import StaffSearchAudit, StaffTabViewAudit
from ..registry_catalog import get_catalog
from .aggregator_service import AggregationError
from .registry_client import RegistryError
from .staff_api_client import StaffApiClient

_config = Settings.get_config()
_logger = logging.getLogger(_config.logging_default_logger_name)

TAB_PATH = "/aggregation/v1/staff/registry/tab"


class StaffSearchService(BaseService):
    def __init__(self, name="", **kwargs):
        super().__init__(name if name else "StaffSearchService", **kwargs)
        self.staff_api = StaffApiClient.get_component()
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
        search_id = str(uuid.uuid4())

        results = await asyncio.gather(*[
            self._one(search_id, code, catalog.get(code), foundational_id) for code in codes])

        searched_at = datetime.now(timezone.utc)
        outcome = {}
        for r in results:
            outcome[r["registryCode"]] = {k: r[k] for k in ("status", "error") if r.get(k)} | {
                "records": len(r["records"]),
                "found": [{"registerId": x["registerId"], "recordId": x["recordId"]}
                          for x in r["records"]]}
        claims = identity.raw_claims or {}
        audit = StaffSearchAudit(
            id=search_id,
            staff_subject=identity.subject or "unknown",
            staff_username=claims.get("preferred_username"),
            staff_name=identity.name,
            staff_roles=self.allowed_roles(identity),
            foundational_id=foundational_id,
            registries=codes,
            purpose=purpose,
            outcome=outcome,
        )
        async with async_session()() as session:
            session.add(audit)
            await session.commit()
        _logger.info("Staff search %s by %s (%s) for %s: %s", search_id,
                     audit.staff_username or audit.staff_subject, ",".join(audit.staff_roles),
                     _masked(foundational_id),
                     ", ".join("%s=%s" % (c, o["status"]) for c, o in outcome.items()))

        return {
            "searchId": search_id,
            "foundationalId": foundational_id,
            "searchedAt": searched_at.isoformat(),
            "searchedBy": audit.staff_username or audit.staff_subject,
            "expiresAt": (searched_at + timedelta(seconds=_config.staff_search_ttl_sec)).isoformat(),
            "purpose": purpose,
            "registries": results,
        }

    async def _one(self, search_id: str, code: str, entry,
                   foundational_id: str) -> Dict[str, Any]:
        base = {"registryCode": code, "registryName": entry.name}
        try:
            found = await self.staff_api.search(code, entry, foundational_id)
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
        records = [_record(search_id, code, f, tabs[f["registerId"]]) for f in found]
        return {**base, "status": "found" if records else "not_found", "records": records}

    # ── tab ─────────────────────────────────────────────────────────────────

    async def tab(self, identity: CallerIdentity, search_id: str, registry_code: str,
                  register_id: str, record_id: str, tab_id: str, page: int = 1,
                  page_size: Optional[int] = None) -> Dict[str, Any]:
        search = await self._search_of(identity, search_id)
        entry = get_catalog().get(registry_code)
        if entry is None or entry.staff_api is None or registry_code not in (search.registries or []):
            raise AggregationError(400, "unknown_registry",
                                   "registry %s was not part of this search" % registry_code)
        found = ((search.outcome or {}).get(registry_code) or {}).get("found") or []
        if {"registerId": register_id, "recordId": record_id} not in found:
            raise AggregationError(400, "record_not_in_search",
                                   "this search did not return that record")
        size = min(page_size or _config.staff_tab_page_size, _config.staff_tab_max_page_size)

        claims = identity.raw_claims or {}
        audit = StaffTabViewAudit(
            search_id=search_id, staff_subject=identity.subject or "unknown",
            staff_username=claims.get("preferred_username"), registry_code=registry_code,
            register_id=register_id, record_id=record_id, tab_id=tab_id)
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
            audit.outcome = {"status": "error", "error": {"reason": exc.reason,
                                                          "detail": exc.detail}}
            await _save(audit)
            raise AggregationError(502, exc.reason, exc.detail) from exc
        except AggregationError as exc:
            audit.outcome = {"status": "error", "error": {"reason": exc.reason}}
            await _save(audit)
            raise

        sections = _sections(metas, groups, {v: k for k, v in names.items()}, page, size)
        audit.outcome = {"status": "ok", "sections": len(sections),
                         "records": sum(len(s["records"]) for s in sections)}
        await _save(audit)
        return {
            "searchId": search_id,
            "registryCode": registry_code,
            "registerId": register_id,
            "recordId": record_id,
            "tabId": tab_id,
            "label": tab.get("tab_label") or tab_id,
            "sections": sections,
        }

    async def _search_of(self, identity: CallerIdentity, search_id: str) -> StaffSearchAudit:
        async with async_session()() as session:
            search = await session.get(StaffSearchAudit, search_id)
        # Someone else's search is reported as missing, not as forbidden.
        if search is None or search.staff_subject != (identity.subject or "unknown"):
            raise AggregationError(404, "search_not_found", "no such search for this user")
        created = search.created_at
        if created is not None:
            if created.tzinfo is None:
                created = created.replace(tzinfo=timezone.utc)
            if datetime.now(timezone.utc) - created > timedelta(
                    seconds=_config.staff_search_ttl_sec):
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


async def _save(row):
    async with async_session()() as session:
        session.add(row)
        await session.commit()


def _text(value: Any) -> Optional[str]:
    return None if value is None else str(value)


def _masked(value: str) -> str:
    """The ID as it goes to the log: last four characters only."""
    return ("*" * max(len(value) - 4, 0)) + value[-4:]
