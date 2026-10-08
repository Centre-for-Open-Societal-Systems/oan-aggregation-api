"""The OpenG2P Beneficiary-360 (bene-360) contract, as this service implements it.

Specification: https://github.com/OpenG2P/bene-360-api (``request.schema.json``,
``response.schema.json``). The partner's query is a bene-360 request, and the
record delivered on the callback is a bene-360 response; ``test/unit`` checks
both against the published schemas.

What is supported:

``REGISTRIES``
    Every registry in the catalog (or in ``registryFilter``) is searched by the
    beneficiary's ``foundationalId`` and its record is mapped onto
    ``registries[] -> registers[] -> tables[]``, with ``attributes`` holding
    only the fields the catalog allows.

What is not (yet):

``PROGRAMS`` / ``DISBURSEMENTS`` / ``BRIDGE_PROCESSING``
    No PBMS or G2P-Bridge is connected. Asking for them returns empty
    ``programs[]`` / ``bridgeProcessing[]`` and a ``meta.warnings[]`` entry.
History within the timeframe
    Registries are read through their DCI sync search, which returns the
    current record. ``meta.resolvedTimeframe`` echoes the requested window and
    reports each registry's actual coverage as the generation date.

This module needs only pydantic and the catalog, so it is unit-testable without
a database, a Consent Manager or a registry.
"""
from datetime import date, datetime, timedelta, timezone
from typing import Any, Dict, List, Literal, Optional, Union

from pydantic import BaseModel, ConfigDict, Field, field_validator

from .registry_catalog import (
    RegistryCatalog,
    RegistryEntry,
    compile_fields,
    first_scalar,
    project,
    rows_at,
)

CONTEXT_URL = "https://schemas.openg2p.org/beneficiary360/v1/context.jsonld"
RESPONSE_TYPE = "Beneficiary360Response"
#: DCI search_criteria.query_type that says "query is a bene-360 request".
QUERY_TYPE = "beneficiary360"
#: data.reg_type / reg_record_type on the on-search that carries the response.
DCI_REG_TYPE = "beneficiary360"
DCI_REG_RECORD_TYPE = RESPONSE_TYPE

SECTIONS = ("REGISTRIES", "PROGRAMS", "DISBURSEMENTS", "BRIDGE_PROCESSING")
#: Sections this service can answer. The rest are reported, not refused.
SUPPORTED_SECTIONS = ("REGISTRIES",)
#: Who would answer an unsupported section, for meta.warnings[].system.
SECTION_SYSTEMS = {"PROGRAMS": "PBMS", "DISBURSEMENTS": "PBMS",
                   "BRIDGE_PROCESSING": "G2P_BRIDGE"}
#: The lookback each Timeframe resolves to (None = all-time), as in the
#: specification's example (Short=90d, Medium=1y, Long=all-time).
TIMEFRAME_DAYS = {"Timeframe-Short": 90, "Timeframe-Medium": 365, "Timeframe-Long": None}
#: meta.warnings[].system for warnings about this service itself.
SELF_SYSTEM = "AGGREGATION_LAYER"

Timeframe = Literal["Timeframe-Short", "Timeframe-Medium", "Timeframe-Long"]
Section = Literal["REGISTRIES", "PROGRAMS", "DISBURSEMENTS", "BRIDGE_PROCESSING"]


class Beneficiary360Request(BaseModel):
    """``request.schema.json``, field for field (it forbids extra properties)."""

    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    context: Union[Literal["https://schemas.openg2p.org/beneficiary360/v1/context.jsonld"],
                   Dict[str, Any]] = Field(alias="@context")
    foundational_id: str = Field(alias="foundationalId", min_length=1)
    timeframe: Timeframe
    as_of_date: Optional[date] = Field(default=None, alias="asOfDate")
    sections: Optional[List[Section]] = Field(default=None, min_length=1)
    registry_filter: Optional[List[str]] = Field(default=None, alias="registryFilter")
    correlation_id: Optional[str] = Field(default=None, alias="correlationId")

    @field_validator("sections", "registry_filter")
    @classmethod
    def _unique(cls, value):
        if value is not None and len(set(value)) != len(value):
            raise ValueError("items must be unique")
        return value

    def wire(self) -> Dict[str, Any]:
        """The request as the partner sent it (aliases, no unset fields)."""
        return self.model_dump(mode="json", by_alias=True, exclude_none=True)


# ── what to query ───────────────────────────────────────────────────────────


def requested_sections(query: Dict[str, Any]) -> List[str]:
    return list(query.get("sections") or SECTIONS)


def unsupported_sections(query: Dict[str, Any]) -> List[str]:
    return [s for s in requested_sections(query) if s not in SUPPORTED_SECTIONS]


def planned_registries(catalog: RegistryCatalog, query: Dict[str, Any]) -> List[str]:
    """Registry codes to query: the filter (if any) ∩ the catalog, catalog order."""
    if "REGISTRIES" not in requested_sections(query):
        return []
    wanted = query.get("registryFilter")
    return [code for code in catalog.codes() if wanted is None or code in wanted]


def unknown_registries(catalog: RegistryCatalog, query: Dict[str, Any]) -> List[str]:
    return [code for code in (query.get("registryFilter") or [])
            if catalog.get(code) is None]


# ── record -> registers / tables ────────────────────────────────────────────


def _row_entry(mnemonic: str, name: Optional[str], row: Dict[str, Any], fields: List[str],
               id_path: Optional[str], status_path: Optional[str], position: int,
               parent_id: Optional[str], nested) -> Dict[str, Any]:
    """One bene-360 TableEntry from one row, with its nested tables."""
    internal_id = first_scalar(row, id_path) or "%s-%d" % (mnemonic, position)
    entry: Dict[str, Any] = {"tableMnemonic": mnemonic, "internalRecordId": internal_id}
    if name:
        entry["tableName"] = name
    if parent_id:
        entry["linkInternalRecordId"] = parent_id
    status = first_scalar(row, status_path)
    if status:
        entry["recordStatus"] = status
    entry["attributes"] = project(row, compile_fields(fields)) or {}
    children: List[Dict[str, Any]] = []
    for table in nested:
        for i, child in enumerate(rows_at(row, table.path), start=1):
            children.append(_row_entry(
                table.table, table.name, child, table.fields, table.internal_record_id,
                table.record_status, i, internal_id, table.tables))
    entry["tables"] = children
    return entry


def _iso_datetime(value: Optional[str]) -> Optional[str]:
    """Only a value that really is a date-time may go into a date-time field."""
    if not value:
        return None
    try:
        datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return value


def map_record(entry: RegistryEntry, record: Dict[str, Any], scopes: List[str],
               foundational_id: str) -> List[Dict[str, Any]]:
    """One registry record -> the bene-360 RegisterEntry list it represents.

    Only the blocks in ``scopes`` are read (the registry has already clamped to
    them; this does not trust that). Register attributes are grouped by the
    block they came from, so a partner can tell which consent scope each value
    rests on. A register appears when any of its blocks, or any table under it,
    has data.
    """
    registers: Dict[str, Dict[str, Any]] = {}

    def register(mnemonic: str) -> Dict[str, Any]:
        if mnemonic not in registers:
            rdef = entry.registers[mnemonic]
            reg: Dict[str, Any] = {"registerMnemonic": mnemonic}
            if rdef.name:
                reg["registerName"] = rdef.name
            for key, path in (("internalRecordId", rdef.internal_record_id),
                              ("functionalRecordId", rdef.functional_record_id)):
                value = first_scalar(record, path)
                if value:
                    reg[key] = value
            reg["foundationalId"] = foundational_id
            reg["recordStatus"] = (first_scalar(record, rdef.record_status)
                                   or rdef.default_record_status)
            approved = _iso_datetime(first_scalar(record, rdef.last_approved_at))
            if approved:
                reg["lastApprovedAt"] = approved
            reg["attributes"] = {}
            reg["tables"] = []
            registers[mnemonic] = reg
        return registers[mnemonic]

    # Rows of every top-level table block, kept with the raw row for joins.
    table_rows: Dict[str, List[tuple]] = {}
    for scope, sdef in entry.scopes.items():
        if scope not in scopes:
            continue
        block = record.get(scope)
        if block is None or block == "" or block == [] or block == {}:
            continue
        if sdef.register_mnemonic:
            attributes = project(block, compile_fields(sdef.fields))
            if attributes:
                register(sdef.register_mnemonic)["attributes"][scope] = attributes
            continue
        rows = block if isinstance(block, list) else [block]
        built = []
        for i, row in enumerate((r for r in rows if isinstance(r, dict)), start=1):
            built.append((row, _row_entry(sdef.table, sdef.name, row, sdef.fields,
                                          sdef.internal_record_id, sdef.record_status,
                                          i, None, sdef.tables)))
        table_rows[sdef.table] = built

    # Hang every table row off its parent. A register parent is this record's
    # register; a table parent is the row whose join key matches. A row whose
    # key matches nothing is kept, directly under its root register, rather
    # than dropped.
    for scope, sdef in entry.scopes.items():
        if sdef.table not in table_rows:
            continue
        for row, built in table_rows[sdef.table]:
            parent = None
            if sdef.join:
                key = first_scalar(row, sdef.join.field)
                parent = next((p for p_row, p in table_rows.get(sdef.parent, [])
                               if key is not None
                               and first_scalar(p_row, sdef.join.parent_field) == key), None)
            if parent is None:
                parent = register(entry.root_register(scope))
            link = parent.get("internalRecordId")
            if link:
                built["linkInternalRecordId"] = link
            parent["tables"].append(built)

    return list(registers.values())


def map_registry(code: str, entry: RegistryEntry, records: List[Dict[str, Any]],
                 scopes: List[str], foundational_id: str) -> Optional[Dict[str, Any]]:
    """A bene-360 RegistryMembership, or None when the beneficiary has no data here."""
    registers: List[Dict[str, Any]] = []
    for record in records:
        if isinstance(record, dict):
            registers.extend(map_record(entry, record, scopes, foundational_id))
    if not registers:
        return None
    return {"registryCode": code, "registryName": entry.name, "registers": registers}


# ── the response ────────────────────────────────────────────────────────────

_WARNINGS = {
    "not_consented": ("NOT_CONSENTED",
                      "no scope of this registry is within the consent; not queried"),
    "no_active_grant": ("NO_ACTIVE_GRANT",
                        "the subject's authorisation for this registry is no longer "
                        "active; not queried"),
}


def _zulu(moment: datetime) -> str:
    return moment.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def build_response(*, catalog: RegistryCatalog, query: Dict[str, Any],
                   outcomes: Dict[str, Dict[str, Any]], response_id: str,
                   generated_at: Optional[datetime] = None) -> Dict[str, Any]:
    """Assemble the bene-360 response from per-registry outcomes.

    ``outcomes`` is ``{registryCode: {"status": ..., ...}}`` where status is
    ``ok`` (with ``membership``, possibly None, and ``discarded``: records the
    registry returned that failed the exact identifier check), ``not_consented``,
    ``no_active_grant`` or ``error`` (with ``reason`` / ``detail``). Every
    registry that was asked for and gave nothing back is explained in
    ``meta.warnings`` - a short answer never looks like "no data".
    """
    generated_at = generated_at or datetime.now(timezone.utc)
    today = generated_at.astimezone(timezone.utc).date()
    sections = requested_sections(query)
    warnings: List[Dict[str, str]] = []

    for code in unknown_registries(catalog, query):
        warnings.append({"system": code, "code": "REGISTRY_NOT_CONFIGURED",
                         "message": "not in this service's registry catalog; not queried"})

    registries: List[Dict[str, Any]] = []
    queried: List[str] = []
    answered: List[str] = []
    for code in planned_registries(catalog, query):
        outcome = outcomes.get(code) or {"status": "not_consented"}
        status = outcome.get("status")
        if status == "ok":
            queried.append(code)
            answered.append(code)
            if outcome.get("membership"):
                registries.append(outcome["membership"])
            if outcome.get("discarded"):
                warnings.append({"system": code, "code": "IDENTIFIER_MISMATCH",
                                 "message": "%d record(s) matched the search but not the "
                                            "foundational ID exactly; discarded"
                                            % outcome["discarded"]})
        elif status == "error":
            queried.append(code)
            warnings.append({"system": code, "code": str(outcome.get("reason") or "error"),
                             "message": str(outcome.get("detail") or outcome.get("reason")
                                            or "registry query failed")})
        else:
            warn_code, message = _WARNINGS.get(status, ("NOT_QUERIED", "not queried"))
            warnings.append({"system": code, "code": warn_code, "message": message})

    for section in unsupported_sections(query):
        warnings.append({"system": SECTION_SYSTEMS[section], "code": "SECTION_NOT_SUPPORTED",
                         "message": "%s is not available: no %s is connected to this "
                                    "service" % (section, SECTION_SYSTEMS[section])})

    as_of = query.get("asOfDate")
    end = date.fromisoformat(as_of) if as_of else today
    if as_of and end != today:
        warnings.append({"system": SELF_SYSTEM, "code": "AS_OF_DATE_NOT_APPLIED",
                         "message": "registries return their current record; asOfDate "
                                    "only anchors meta.resolvedTimeframe"})
    resolved: Dict[str, Any] = {}
    days = TIMEFRAME_DAYS.get(query.get("timeframe"))
    if days is not None:
        resolved["start"] = (end - timedelta(days=days)).isoformat()
    resolved["end"] = end.isoformat()
    # What each registry that answered actually covered: its current record,
    # as of today.
    resolved["perSourceSystem"] = [
        {"system": code, "start": today.isoformat(), "end": today.isoformat()}
        for code in answered]

    parameters = {"foundationalId": query["foundationalId"], "timeframe": query["timeframe"]}
    if as_of:
        parameters["asOfDate"] = as_of
    if query.get("sections"):
        parameters["sections"] = list(query["sections"])

    meta: Dict[str, Any] = {"generatedAt": _zulu(generated_at)}
    if query.get("correlationId"):
        meta["correlationId"] = query["correlationId"]
    meta.update({"requestParameters": parameters, "resolvedTimeframe": resolved,
                 "sourceSystemsQueried": queried, "warnings": warnings})

    response: Dict[str, Any] = {
        "@context": CONTEXT_URL,
        "@type": RESPONSE_TYPE,
        "@id": response_id,
        "beneficiary": {"foundationalId": query["foundationalId"],
                        "matchedRegistryCount": len(registries)},
    }
    if "REGISTRIES" in sections:
        response["registries"] = registries
    if "PROGRAMS" in sections or "DISBURSEMENTS" in sections:
        response["programs"] = []
    if "BRIDGE_PROCESSING" in sections:
        response["bridgeProcessing"] = []
    response["meta"] = meta
    return response


__all__ = [
    "Beneficiary360Request",
    "CONTEXT_URL",
    "DCI_REG_RECORD_TYPE",
    "DCI_REG_TYPE",
    "QUERY_TYPE",
    "build_response",
    "map_record",
    "map_registry",
    "planned_registries",
    "unknown_registries",
    "unsupported_sections",
]
