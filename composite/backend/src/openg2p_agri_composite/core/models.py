"""Use-case configuration schema (one YAML file per use case).

Validation is strict: unknown keys are errors. Keys from the design
(Open Agri Stack docs: https://docs.openg2p.org/products/open-agri-stack/composite) that this service does not act on yet are accepted and
listed in ``NOT_ENFORCED`` so a file written against the full design still
loads; the loader logs them once.
"""

import re
from typing import Any, Dict, List, Literal, Optional

from pydantic import AliasChoices, BaseModel, ConfigDict, Field, field_validator, model_validator

SLUG = re.compile(r"^[a-z][a-z0-9-]*[a-z0-9]$")
SOURCE_ID = re.compile(r"^[a-z][a-z0-9_]*$")
SEMVER = re.compile(r"^(\d+)\.(\d+)\.(\d+)$")
OUTPUT_SEGMENT = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
RATE = re.compile(r"^\s*(\d+)\s*/\s*(s|sec|second|m|min|minute|h|hour)\s*$")
RATE_PERIODS = {"s": 1, "sec": 1, "second": 1, "m": 60, "min": 60, "minute": 60, "h": 3600, "hour": 3600}

# Design keys accepted but not acted on by this version of the service.
NOT_ENFORCED = (
    "owner",
    "consent.collection",
    "consent.mode",
    "response.schema",
    "response.correlate_on",
    "limits.daily_quota_per_partner",
)


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", populate_by_name=True)


class ParameterSpec(_Strict):
    type: Literal["integer", "number", "string", "boolean"] = "string"
    description: str = ""
    required: bool = False
    default: Any = None
    min: Optional[float] = None
    max: Optional[float] = None
    enum: Optional[List[Any]] = None

    @field_validator("type", mode="before")
    @classmethod
    def _alias_int(cls, v):
        return "integer" if v == "int" else v

    @model_validator(mode="after")
    def _check(self):
        if self.enum is not None and not self.enum:
            raise ValueError("enum must not be empty")
        if self.min is not None and self.max is not None and self.min > self.max:
            raise ValueError("min is greater than max")
        if self.default is not None:
            # A bad default would fail every request that omits the parameter.
            coerce_parameter("default", self, self.default)
        return self


class ParameterError(ValueError):
    pass


def coerce_parameter(name: str, spec: ParameterSpec, value: Any) -> Any:
    """Check one parameter value against its spec; returns the value (ints stay ints)."""
    t = spec.type
    if t == "integer":
        if isinstance(value, bool) or not isinstance(value, int):
            if isinstance(value, float) and value.is_integer():
                value = int(value)
            else:
                raise ParameterError(f"parameter '{name}' must be an integer")
    elif t == "number":
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise ParameterError(f"parameter '{name}' must be a number")
    elif t == "boolean":
        if not isinstance(value, bool):
            raise ParameterError(f"parameter '{name}' must be true or false")
    elif t == "string":
        if not isinstance(value, str):
            raise ParameterError(f"parameter '{name}' must be a string")
    if spec.enum is not None and value not in spec.enum:
        raise ParameterError(f"parameter '{name}' must be one of {spec.enum}")
    if t in ("integer", "number"):
        if spec.min is not None and value < spec.min:
            raise ParameterError(f"parameter '{name}' must be >= {spec.min:g}")
        if spec.max is not None and value > spec.max:
            raise ParameterError(f"parameter '{name}' must be <= {spec.max:g}")
    elif t == "string" and (spec.min is not None or spec.max is not None):
        if spec.min is not None and len(value) < spec.min:
            raise ParameterError(f"parameter '{name}' is shorter than {spec.min:g}")
        if spec.max is not None and len(value) > spec.max:
            raise ParameterError(f"parameter '{name}' is longer than {spec.max:g}")
    return value


class SubjectSpec(_Strict):
    id_types: List[str] = Field(..., min_length=1)


class BatchSpec(_Strict):
    max_subjects: int = 1

    @field_validator("max_subjects")
    @classmethod
    def _one(cls, v):
        if v != 1:
            raise ValueError("only max_subjects: 1 (one subject per request) is supported")
        return v


class InputSpec(_Strict):
    subject: SubjectSpec
    parameters: Dict[str, ParameterSpec] = {}
    batch: BatchSpec = BatchSpec()

    @field_validator("parameters")
    @classmethod
    def _names(cls, v):
        for name in v:
            if not OUTPUT_SEGMENT.match(name):
                raise ValueError(f"parameter name '{name}' must be an identifier")
        return v


class ConsentSpec(_Strict):
    required: bool = True
    collection: Optional[Literal["cm-originated", "partner-embedded"]] = None
    mode: Optional[Literal["single"]] = None


class DciSpec(_Strict):
    reg_type: str = Field(..., min_length=1)
    reg_record_type: str = Field(..., min_length=1)
    # Inline Jinja (renders the search criteria's query part as JSON), or the
    # name of a template file next to the use-case file.
    query_template: str = Field(..., min_length=1)


class SourceSpec(_Strict):
    id: str
    controller: str = Field(..., min_length=1)
    requirement: Literal["mandatory", "optional"] = "mandatory"
    depends_on: List[str] = []
    # The registry's data scopes this source needs (IDs <controller>.<name>, from the
    # registry's catalogue): the partner's consent must grant every one of
    # ``scopes``; ``optional_scopes`` may be left out (their fields then come back
    # null). The composite asks for no other scope. Empty: no scope check for this
    # source (the consent's grant for the registry is used as it is).
    # ``request_scopes`` is the earlier name of ``scopes``.
    scopes: List[str] = Field(default_factory=list, validation_alias=AliasChoices("scopes", "request_scopes"))
    optional_scopes: List[str] = []
    dci: DciSpec
    timeout_ms: Optional[int] = Field(None, ge=50, le=120000)
    retries: int = Field(0, ge=0, le=5)

    @field_validator("id")
    @classmethod
    def _id(cls, v):
        if not SOURCE_ID.match(v):
            raise ValueError(f"source id '{v}' must match {SOURCE_ID.pattern}")
        return v

    @field_validator("depends_on", mode="before")
    @classmethod
    def _one_or_many(cls, v):
        return [v] if isinstance(v, str) else v

    @model_validator(mode="after")
    def _scopes(self):
        for name in ("scopes", "optional_scopes"):
            values = getattr(self, name)
            dupes = sorted({v for v in values if values.count(v) > 1})
            if dupes:
                raise ValueError(f"source '{self.id}': {name} lists {dupes} twice")
            wrong = [v for v in values if not v.startswith(self.controller + ".") or v == self.controller + "."]
            if wrong:
                raise ValueError(f"source '{self.id}': {name} {wrong} are not scopes of its registry "
                                 f"'{self.controller}' (IDs are '{self.controller}.<name>')")
        both = sorted(set(self.scopes) & set(self.optional_scopes))
        if both:
            raise ValueError(f"source '{self.id}': {both} are both in scopes and optional_scopes")
        return self


class ResponseSpec(_Strict):
    mode: Literal["merged"] = "merged"
    schema_: Optional[str] = Field(None, alias="schema")
    correlate_on: Optional[str] = None
    mapping: Dict[str, str] = {}
    derived: Dict[str, str] = {}
    source_status: bool = True


class ExecutionSpec(_Strict):
    fan_out: Literal["parallel"] = "parallel"
    overall_timeout_ms: Optional[int] = Field(None, ge=100, le=300000)
    # allowed: only a failing mandatory source fails the request.
    # denied:  any source that is denied / unavailable / error fails it.
    partial_response: Literal["allowed", "denied"] = "allowed"


class LimitsSpec(_Strict):
    rate_per_partner: Optional[str] = None
    daily_quota_per_partner: Optional[int] = None

    @field_validator("rate_per_partner")
    @classmethod
    def _rate(cls, v):
        if v is not None and not RATE.match(v):
            raise ValueError("rate_per_partner must look like '60/min' (units: s, min, hour)")
        return v

    def rate(self) -> Optional[tuple[int, int]]:
        """(requests, period seconds) or None."""
        if not self.rate_per_partner:
            return None
        m = RATE.match(self.rate_per_partner)
        return int(m.group(1)), RATE_PERIODS[m.group(2)]


class AuditSpec(_Strict):
    events: List[Literal["request", "source_call", "response"]] = ["request", "source_call", "response"]


class UseCase(_Strict):
    use_case: str
    version: str
    status: Literal["draft", "validated", "published", "deprecated", "retired"]
    title: str = Field(..., min_length=1)
    description: str = ""
    owner: Optional[str] = None

    policy: Optional[str] = None
    purpose: Optional[str] = None
    consent: ConsentSpec = ConsentSpec()
    # Which partners may call the use case is not here: each partner's use-case
    # policy in Partner Management grants it (``name@major``).

    input: InputSpec
    sources: List[SourceSpec] = Field(..., min_length=1)
    response: ResponseSpec = ResponseSpec()
    execution: ExecutionSpec = ExecutionSpec()
    limits: LimitsSpec = LimitsSpec()
    audit: AuditSpec = AuditSpec()

    @field_validator("use_case")
    @classmethod
    def _slug(cls, v):
        if not SLUG.match(v):
            raise ValueError(f"use_case '{v}' must be a lower-case slug (letters, digits, '-')")
        return v

    @field_validator("version")
    @classmethod
    def _semver(cls, v):
        if not SEMVER.match(str(v)):
            raise ValueError(f"version '{v}' must be semver MAJOR.MINOR.PATCH")
        return str(v)

    @model_validator(mode="after")
    def _graph_and_outputs(self):
        ids = [s.id for s in self.sources]
        dupes = sorted({i for i in ids if ids.count(i) > 1})
        if dupes:
            raise ValueError(f"duplicate source ids: {dupes}")
        for s in self.sources:
            for dep in s.depends_on:
                if dep == s.id:
                    raise ValueError(f"source '{s.id}' depends on itself")
                if dep not in ids:
                    raise ValueError(f"source '{s.id}' depends on unknown source '{dep}'")
        outputs = list(self.response.mapping) + list(self.response.derived)
        seen = set()
        for out in outputs:
            parts = out.split(".")
            if not all(OUTPUT_SEGMENT.match(p) for p in parts):
                raise ValueError(f"output path '{out}' must be dot-separated identifiers")
            if out in seen:
                raise ValueError(f"output path '{out}' is defined twice")
            seen.add(out)
        for a in seen:
            for b in seen:
                if a != b and b.startswith(a + "."):
                    raise ValueError(f"output path '{a}' is both a value and a parent of '{b}'")
        return self

    @property
    def major(self) -> int:
        return int(SEMVER.match(self.version).group(1))

    @property
    def ref(self) -> str:
        return f"{self.use_case}@{self.major}"

    def consent_scopes(self) -> Dict[str, Dict[str, List[str]]]:
        """{controller: {"required": [...], "optional": [...]}} over the sources that
        declare scopes: what the partner's consent must (and may) grant per registry.
        A scope one source needs and another lists as optional is required."""
        out: Dict[str, Dict[str, set]] = {}
        for src in self.sources:
            if not (src.scopes or src.optional_scopes):
                continue
            entry = out.setdefault(src.controller, {"required": set(), "optional": set()})
            entry["required"] |= set(src.scopes)
            entry["optional"] |= set(src.optional_scopes)
        return {
            c: {"required": sorted(e["required"]), "optional": sorted(e["optional"] - e["required"])}
            for c, e in sorted(out.items())
        }

    def not_enforced_keys(self) -> List[str]:
        used = []
        if self.owner:
            used.append("owner")
        if self.consent.collection:
            used.append("consent.collection")
        if self.consent.mode:
            used.append("consent.mode")
        if self.response.schema_:
            used.append("response.schema")
        if self.response.correlate_on:
            used.append("response.correlate_on")
        if self.limits.daily_quota_per_partner:
            used.append("limits.daily_quota_per_partner")
        return used
