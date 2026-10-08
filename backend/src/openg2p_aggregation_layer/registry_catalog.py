"""The registry catalog: which registries this service may query, and how.

Everything the Aggregation Layer knows about a registry lives in one YAML file
(``AGGREGATION_LAYER_REGISTRY_CATALOG_PATH``), loaded and validated once at
startup. Adding a registry is an entry in that file; no code changes. An
invalid file stops the service with every problem listed, rather than starting
and failing on the first request that touches the bad entry.

Per registry, keyed by its bene-360 ``registryCode`` (a free string):

``partner_api`` / ``binding`` / ``search``
    Where its OpenG2P partner API is, which Consent Manager binding the hop is
    validated against (``audience`` + ``controller_id``) and the DCI search
    parameters, including the ``id_type`` the beneficiary's foundational ID is
    searched with.
``registers``
    The bene-360 registers the registry's record maps onto, and where their
    identifiers and status come from in the rendered record.
``scopes``
    One entry per top-level block of the registry's outgest template - the unit
    consent works at. Each says where the block lands in the bene-360 response
    (a ``register``, or a ``table`` under a ``parent``) and which attribute
    paths may leave this service (``fields``).

**Data minimisation happens here.** A registry clamps its record to whole
top-level blocks (the consent scope); ``fields`` then keeps only the listed
paths of each block before the partner sees it. ``["*"]`` passes a block
through whole and has to be written out on purpose.

The partner names scopes as ``<registryCode>.<scope>`` (``scope_id``) in its
consent object; the Consent Manager treats them as opaque strings. Towards the
registry the plain block name is used, which is what the registry clamps on.

This module needs only pydantic and PyYAML, so the operator scripts
(``scripts/register-aggregator.py``, ``scripts/stack-check.py``) load and
validate the same file with the same rules.
"""
import re
from typing import Any, Dict, Iterable, List, Literal, Optional, Tuple

import yaml
from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

#: Separates the registry code from the block name in a partner-facing scope id.
SCOPE_SEPARATOR = "."
#: A ``fields`` entry that passes the whole block (or row) through.
WILDCARD = "*"

# A registry code may not contain the separator, so a scope id splits one way.
_CODE_RE = re.compile(r"^[A-Za-z0-9_-]{1,50}$")
_SCOPE_RE = re.compile(r"^[A-Za-z0-9_-]{1,100}$")
_MNEMONIC_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}$")
_SEGMENT_RE = re.compile(r"^[A-Za-z0-9_@$:-]+(\[\])?$")


class CatalogError(ValueError):
    """The catalog file is missing, unreadable or invalid."""


# ── paths ───────────────────────────────────────────────────────────────────


def _check_path(path: str, *, wildcard: bool = False) -> str:
    """A dotted path where a ``name[]`` segment maps over a list."""
    if wildcard and path == WILDCARD:
        return path
    if not isinstance(path, str) or not path:
        raise ValueError("a path must be a non-empty string")
    for segment in path.split("."):
        if not _SEGMENT_RE.match(segment):
            raise ValueError("invalid path '%s' (segment '%s')" % (path, segment))
    return path


def compile_fields(paths: List[str]) -> Optional[Dict[str, Any]]:
    """Turn a ``fields`` list into a projection tree. None means "whole value".

    Each node is ``{name: {"list": bool, "children": tree | None}}``; a None
    child keeps that value whole. A path that is a prefix of another keeps the
    whole value (``a`` beats ``a.b``). A name used both as ``a[]`` and as
    ``a`` is a contradiction and is rejected.
    """
    if WILDCARD in paths:
        if len(paths) > 1:
            raise ValueError("'*' cannot be combined with other fields")
        return None
    root: Dict[str, Any] = {}
    for path in paths:
        _check_path(path)
        node = root
        segments = path.split(".")
        for i, segment in enumerate(segments):
            is_list = segment.endswith("[]")
            name = segment[:-2] if is_list else segment
            last = i == len(segments) - 1
            entry = node.get(name)
            if entry is None:
                entry = node[name] = {"list": is_list, "children": None if last else {}}
            elif entry["list"] != is_list:
                raise ValueError("'%s': '%s' is used both as a list ('%s[]') and as a "
                                 "single value" % (path, name, name))
            elif entry["children"] is None:
                break  # already kept whole
            elif last:
                entry["children"] = None
                break
            node = entry["children"]
    return root


def _empty(value: Any) -> bool:
    """Treat "", [], {} and None alike as 'not present'.

    Outgest templates emit an empty string rather than omitting a field nobody
    supplied (a phone number renders as ``[""]``). Passing that through would
    hand the partner a key that looks like data and is not.
    """
    return value is None or value == "" or value == [] or value == {}


def _clean(value: Any) -> Any:
    if isinstance(value, list):
        kept = [v for v in value if not _empty(v)]
        return kept or None
    return value


def project(value: Any, tree: Optional[Dict[str, Any]]) -> Any:
    """Keep only the paths in ``tree`` (from ``compile_fields``), structure intact.

    ``{"name": {"given_name": "A", "surname": "B"}, "sex": "f"}`` projected on
    ``["name.given_name"]`` is ``{"name": {"given_name": "A"}}``. A path that
    does not exist is simply absent - a record without a phone number is not an
    error. Returns None when nothing is left.
    """
    if tree is None:
        return None if _empty(value) else value
    if not isinstance(value, dict):
        return None
    out: Dict[str, Any] = {}
    for name, spec in tree.items():
        if name not in value:
            continue
        current, children = value[name], spec["children"]
        if spec["list"]:
            if not isinstance(current, list):
                continue
            items = [project(item, children) if children is not None else item
                     for item in current]
            items = [item for item in items if not _empty(item)]
            if items:
                out[name] = items
            continue
        kept = project(current, children) if children is not None else _clean(current)
        if not _empty(kept):
            out[name] = kept
    return out or None


def resolve(value: Any, path: str) -> Any:
    """Follow a path; a ``[]`` segment maps over a list and flattens the result."""
    node: List[Any] = [value]
    for segment in path.split("."):
        is_list = segment.endswith("[]")
        name = segment[:-2] if is_list else segment
        nxt: List[Any] = []
        for item in node:
            current = item.get(name) if isinstance(item, dict) else None
            if current is None:
                continue
            if is_list:
                if isinstance(current, list):
                    nxt.extend(current)
            else:
                nxt.append(current)
        node = nxt
    if not node:
        return None
    return node if len(node) > 1 or "[]" in path else node[0]


def first_scalar(value: Any, path: Optional[str]) -> Optional[str]:
    """The first non-empty scalar at ``path``, as a string (for identifiers)."""
    if not path:
        return None
    found = resolve(value, path)
    for item in (found if isinstance(found, list) else [found]):
        if not _empty(item) and not isinstance(item, (dict, list)):
            return str(item)
    return None


def rows_at(value: Any, path: str) -> List[Dict[str, Any]]:
    """The rows under ``path``: a list of objects, or one object."""
    found = resolve(value, path)
    items = found if isinstance(found, list) else [found]
    rows: List[Dict[str, Any]] = []
    for item in items:
        if isinstance(item, list):
            rows.extend(x for x in item if isinstance(x, dict))
        elif isinstance(item, dict):
            rows.append(item)
    return rows


# ── the file ────────────────────────────────────────────────────────────────


class _Strict(BaseModel):
    # A misspelt key must fail loudly: silently ignoring `feilds:` would
    # release nothing and look exactly like a beneficiary with no data.
    model_config = ConfigDict(extra="forbid")


class PartnerApi(_Strict):
    base_url: str = Field(description="Base URL of the registry's OpenG2P partner API")
    receiver_id: str = Field(min_length=1, description="DCI header.receiver_id")
    timeout_sec: Optional[float] = Field(default=None, gt=0)

    @model_validator(mode="after")
    def _url(self):
        if not self.base_url.lower().startswith(("http://", "https://")):
            raise ValueError("base_url must be an http(s) URL")
        return self


class Binding(_Strict):
    """The Consent Manager binding this service spends on every hop."""

    audience: str = Field(min_length=1)
    controller_id: str = Field(min_length=1)
    # Only read by scripts/register-aggregator.py when it writes the policy.
    allowed_purposes: List[str] = Field(default_factory=list)


class IdentifierMatch(_Strict):
    """Where the beneficiary's identifier sits in the registry's rendered record.

    An OpenG2P partner API answers an ``idtype-value`` query with a substring
    search over the register's ``search_text`` (it does not look at
    ``id_type``), so a record whose phone number or another ID merely contains
    the foundational ID comes back too. Every returned record is therefore
    checked here, exactly: it is kept only if one identifier entry at ``path``
    has ``value_key`` equal to the foundational ID and, unless ``type_key`` is
    null, ``type_key`` equal to the registry's ``search.id_type``.

    The exact DCI ``expression`` query is not used instead: the platform
    answers it without the record's child hierarchy, so every table would be
    lost.
    """

    # A list (``blk.member_identifier[]``) or a single object of identifiers.
    # Its first segment must be one of the registry's scopes: that block is
    # always fetched on the hop so the check can run, and is released to the
    # partner only when the consent covers it.
    path: str
    value_key: str = "identifier_value"
    type_key: Optional[str] = "identifier_type"

    @model_validator(mode="after")
    def _paths(self):
        _check_path(self.path)
        for key in (self.value_key, self.type_key):
            if key is not None and not _SEGMENT_RE.match(key):
                raise ValueError("invalid key '%s'" % key)
        return self

    @property
    def scope(self) -> str:
        """The top-level block the identifiers are read from."""
        head = self.path.split(".")[0]
        return head[:-2] if head.endswith("[]") else head


class Search(_Strict):
    reg_type: str = Field(min_length=1)
    reg_record_type: str = Field(min_length=1)
    # The DCI identifier type of the beneficiary's foundational ID: sent as
    # query.value.id_type, and required on the identifier ``match`` accepts.
    id_type: str = Field(min_length=1)
    match: IdentifierMatch
    page_size: Optional[int] = Field(default=None, ge=1, le=100)


class RegisterDef(_Strict):
    name: Optional[str] = None
    # Paths into the registry's whole rendered record. Values read here leave
    # this service as register identifiers, whatever ``fields`` says.
    internal_record_id: Optional[str] = None
    functional_record_id: Optional[str] = None
    record_status: Optional[str] = None
    last_approved_at: Optional[str] = None
    # bene-360 requires a recordStatus; used when the record carries none.
    default_record_status: str = "UNKNOWN"

    @model_validator(mode="after")
    def _paths(self):
        for path in (self.internal_record_id, self.functional_record_id,
                     self.record_status, self.last_approved_at):
            if path is not None:
                _check_path(path)
        return self


class Join(_Strict):
    """How a table row finds its parent row when the parent is a table."""

    field: str
    parent_field: str

    @model_validator(mode="after")
    def _paths(self):
        _check_path(self.field)
        _check_path(self.parent_field)
        return self


class TableDef(_Strict):
    """A table read from a list (or object) nested inside a row."""

    path: str
    table: str
    name: Optional[str] = None
    fields: List[str] = Field(min_length=1)
    # Paths relative to the row.
    internal_record_id: Optional[str] = None
    record_status: Optional[str] = None
    tables: List["TableDef"] = Field(default_factory=list)

    @model_validator(mode="after")
    def _check(self):
        _check_path(self.path)
        _check_rows(self)
        return self


class ScopeDef(_Strict):
    """One top-level block of the registry's outgest template."""

    # Exactly one of register / table.
    register_mnemonic: Optional[str] = Field(default=None, alias="register")
    table: Optional[str] = None
    name: Optional[str] = None
    parent: Optional[str] = None
    join: Optional[Join] = None
    fields: List[str] = Field(min_length=1)
    # Table placement only; paths relative to the row.
    internal_record_id: Optional[str] = None
    record_status: Optional[str] = None
    tables: List[TableDef] = Field(default_factory=list)

    @model_validator(mode="after")
    def _check(self):
        if bool(self.register_mnemonic) == bool(self.table):
            raise ValueError("set exactly one of 'register' or 'table'")
        if self.register_mnemonic:
            for key in ("name", "parent", "join", "internal_record_id", "record_status"):
                if getattr(self, key) is not None:
                    raise ValueError("'%s' applies to a table placement, not a register "
                                     "(register metadata goes under 'registers')" % key)
        elif not self.parent:
            raise ValueError("a table needs a 'parent' (a register or table mnemonic)")
        _check_rows(self)
        return self


def _check_rows(entry) -> None:
    """Shared checks for anything that produces rows with fields."""
    for mnemonic in (getattr(entry, "table", None), getattr(entry, "register_mnemonic", None)):
        if mnemonic is not None and not _MNEMONIC_RE.match(mnemonic):
            raise ValueError("invalid mnemonic '%s'" % mnemonic)
    compile_fields(entry.fields)
    if entry.tables and WILDCARD in entry.fields:
        # A wildcard row already carries the nested list the table is read
        # from, so the same data would leave twice under two names.
        raise ValueError("fields: ['*'] cannot be combined with nested 'tables'; list "
                         "the row's fields explicitly")
    for path in (entry.internal_record_id, entry.record_status):
        if path is not None:
            _check_path(path)


def _walk_tables(tables: Iterable[TableDef]):
    for table in tables:
        yield table
        yield from _walk_tables(table.tables)


class RegistryEntry(_Strict):
    name: str = Field(min_length=1, description="bene-360 registryName")
    partner_api: PartnerApi
    binding: Binding
    search: Search
    registers: Dict[str, RegisterDef] = Field(min_length=1)
    scopes: Dict[str, ScopeDef] = Field(min_length=1)

    @model_validator(mode="after")
    def _structure(self):
        for mnemonic in self.registers:
            if not _MNEMONIC_RE.match(mnemonic):
                raise ValueError("invalid register mnemonic '%s'" % mnemonic)
        tables: Dict[str, str] = {}   # table mnemonic -> where it is defined
        for scope, sdef in self.scopes.items():
            if not _SCOPE_RE.match(scope):
                raise ValueError("invalid scope '%s' (letters, digits, '_' and '-')" % scope)
            if sdef.register_mnemonic and sdef.register_mnemonic not in self.registers:
                raise ValueError("scope '%s': register '%s' is not under 'registers'"
                                 % (scope, sdef.register_mnemonic))
            names = ([sdef.table] if sdef.table else []) + [
                t.table for t in _walk_tables(sdef.tables)]
            for name in names:
                if name in self.registers or name in tables:
                    raise ValueError("mnemonic '%s' is defined twice" % name)
                tables[name] = scope
        used = {s.register_mnemonic for s in self.scopes.values() if s.register_mnemonic}
        for scope, sdef in self.scopes.items():
            if not sdef.table:
                continue
            if sdef.parent in self.registers:
                used.add(sdef.parent)
                if sdef.join:
                    raise ValueError("scope '%s': 'join' is only for a table parent" % scope)
            elif sdef.parent in tables:
                if not sdef.join:
                    # Several parent rows are normal (one per land, one per
                    # animal); without a key a child row has no defined home.
                    raise ValueError("scope '%s': parent '%s' is a table, so 'join' is "
                                     "required" % (scope, sdef.parent))
            else:
                raise ValueError("scope '%s': parent '%s' is neither a register nor a "
                                 "table of this registry" % (scope, sdef.parent))
        unused = sorted(set(self.registers) - used)
        if unused:
            raise ValueError("register(s) %s are not used by any scope" % ", ".join(unused))
        if self.search.match.scope not in self.scopes:
            raise ValueError("search.match.path '%s' must start with one of this registry's "
                             "scopes (%s)" % (self.search.match.path, ", ".join(self.scopes)))
        for scope in self.scopes:
            self.root_register(scope)   # raises on a cycle
        return self

    def hop_scopes(self, granted: Iterable[str]) -> List[str]:
        """The blocks to ask the registry for: what was granted, plus the block
        the identifier check reads (never released unless it was granted)."""
        return sorted(set(granted) | {self.search.match.scope})

    def identifies(self, record: Any, foundational_id: str) -> bool:
        """True if ``record`` belongs to ``foundational_id``, by exact match."""
        match = self.search.match
        for item in rows_at(record, match.path):
            if str(item.get(match.value_key) or "").strip() != foundational_id:
                continue
            if match.type_key is None or item.get(match.type_key) == self.search.id_type:
                return True
        return False

    def scope_for_table(self, mnemonic: str) -> Optional[str]:
        for scope, sdef in self.scopes.items():
            if sdef.table == mnemonic:
                return scope
        return None

    def root_register(self, scope: str) -> str:
        """The register a scope's rows ultimately hang off."""
        seen = set()
        sdef = self.scopes[scope]
        while not sdef.register_mnemonic and sdef.parent not in self.registers:
            if scope in seen:
                raise ValueError("scope '%s' is part of a parent cycle" % scope)
            seen.add(scope)
            scope = self.scope_for_table(sdef.parent)
            sdef = self.scopes[scope]
        return sdef.register_mnemonic or sdef.parent


class RegistryCatalog(_Strict):
    version: Literal[1] = 1
    registries: Dict[str, RegistryEntry] = Field(min_length=1)

    @model_validator(mode="after")
    def _registries(self):
        audiences: Dict[str, str] = {}
        for code, entry in self.registries.items():
            if not _CODE_RE.match(code):
                raise ValueError("invalid registry code '%s' (letters, digits, '_' and "
                                 "'-', at most 50)" % code)
            other = audiences.setdefault(entry.binding.audience, code)
            if other != code:
                # One binding per registry: the binding's controller_id is what
                # stops one registry's hop being spent at another.
                raise ValueError("registries '%s' and '%s' share the binding audience '%s'"
                                 % (other, code, entry.binding.audience))
        return self

    # ── lookups ─────────────────────────────────────────────────────────────

    def codes(self) -> List[str]:
        return list(self.registries)

    def get(self, code: str) -> Optional[RegistryEntry]:
        return self.registries.get(code)

    @staticmethod
    def scope_id(code: str, scope: str) -> str:
        """The partner-facing name of one registry block."""
        return code + SCOPE_SEPARATOR + scope

    def scope_ids(self, codes: Optional[Iterable[str]] = None) -> List[str]:
        """Every partner-facing scope id, optionally for some registries only."""
        wanted = list(codes) if codes is not None else self.codes()
        return [self.scope_id(code, scope)
                for code in wanted if code in self.registries
                for scope in self.registries[code].scopes]

    def split_scope_ids(self, scope_ids: Iterable[str]) -> Dict[str, List[str]]:
        """``{registryCode: [block, ...]}`` for the ids this catalog knows.

        An id it does not know is dropped, never guessed at: it is not this
        service's to release.
        """
        out: Dict[str, List[str]] = {}
        for scope_id in scope_ids or []:
            code, _, scope = str(scope_id).partition(SCOPE_SEPARATOR)
            entry = self.registries.get(code)
            if entry is not None and scope in entry.scopes:
                out.setdefault(code, [])
                if scope not in out[code]:
                    out[code].append(scope)
        return {code: sorted(scopes) for code, scopes in out.items()}

    def describe(self) -> List[Dict[str, Any]]:
        """The catalog as data for the discovery endpoint.

        Placement and allowed fields only: no URL, binding or anything else a
        partner has no use for.
        """
        out = []
        for code, entry in self.registries.items():
            scopes = []
            for scope, sdef in entry.scopes.items():
                placement = ({"type": "register", "registerMnemonic": sdef.register_mnemonic}
                             if sdef.register_mnemonic else
                             {"type": "table", "tableMnemonic": sdef.table,
                              "parent": sdef.parent})
                scopes.append({
                    "scope": scope,
                    "scopeId": self.scope_id(code, scope),
                    "placement": placement,
                    "fields": list(sdef.fields),
                    "tables": [_describe_table(t) for t in sdef.tables],
                })
            out.append({
                "registryCode": code,
                "registryName": entry.name,
                "registers": [{"registerMnemonic": m, "registerName": r.name}
                              for m, r in entry.registers.items()],
                "scopes": scopes,
            })
        return out


def _describe_table(table: TableDef) -> Dict[str, Any]:
    return {"tableMnemonic": table.table, "path": table.path,
            "fields": list(table.fields),
            "tables": [_describe_table(t) for t in table.tables]}


def _format(exc: ValidationError) -> str:
    lines = []
    for error in exc.errors():
        where = ".".join(str(part) for part in error.get("loc", ()))
        lines.append("  - %s: %s" % (where or "(root)", error.get("msg")))
    return "\n".join(lines)


def parse_catalog(data: Any, source: str = "<data>") -> RegistryCatalog:
    try:
        return RegistryCatalog.model_validate(data)
    except ValidationError as exc:
        raise CatalogError("invalid registry catalog %s:\n%s" % (source, _format(exc))) from exc


def load_catalog(path: str) -> RegistryCatalog:
    """Read and validate the catalog file. Raises CatalogError with every problem."""
    if not path:
        raise CatalogError("no registry catalog configured: set "
                           "AGGREGATION_LAYER_REGISTRY_CATALOG_PATH to the YAML file")
    try:
        with open(path, encoding="utf-8") as fh:
            data = yaml.safe_load(fh)
    except FileNotFoundError as exc:
        raise CatalogError("registry catalog not found: %s" % path) from exc
    except (OSError, yaml.YAMLError) as exc:
        raise CatalogError("registry catalog %s is not readable YAML: %s" % (path, exc)) from exc
    return parse_catalog(data, path)


_loaded: Optional[Tuple[str, RegistryCatalog]] = None


def get_catalog() -> RegistryCatalog:
    """The service's catalog, loaded once from the configured path."""
    global _loaded
    from .config import Settings

    path = Settings.get_config().registry_catalog_path
    if _loaded is None or _loaded[0] != path:
        _loaded = (path, load_catalog(path))
    return _loaded[1]


TableDef.model_rebuild()
