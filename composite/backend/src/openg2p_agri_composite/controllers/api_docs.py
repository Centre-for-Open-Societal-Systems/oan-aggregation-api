"""OpenAPI documentation for the partner API and the console API.

Documentation only: these models and examples describe what the endpoints take
and return in the generated OpenAPI spec. The handlers do not validate against
them (the query handler reads the raw body so that the engine answers every
error with its own signed envelope).
"""

from typing import Any, Dict, List, Literal, Optional

from openg2p_fastapi_common.errors import ErrorListResponse
from pydantic import BaseModel, ConfigDict, Field

# ── shared ───────────────────────────────────────────────────────────────────

# On every route of the Composite's routers: an unexpected error (the framework's handler).
ROUTER_RESPONSES: Dict[int | str, Dict[str, Any]] = {
    500: {"model": ErrorListResponse, "description": "Unexpected server error."},
}


class ApiErrorBody(BaseModel):
    code: str = Field(..., description="Machine-readable error code.", examples=["unknown_use_case"])
    message: str = Field(..., description="What went wrong, for a person.",
                         examples=["no published use case 'loan-profile@2'"])


class ApiError(BaseModel):
    """An unsigned error from a describe or console endpoint."""

    error: ApiErrorBody


class Subject(BaseModel):
    """The person the query is about."""

    type: str = Field(
        ...,
        description="Identifier type. One of the use case's `input.subject.id_types`, "
        "e.g. `FAYDA_FAN` (the Fayda FAN) or `FARMER_ID` (the Farmer Registry's farmer ID).",
        examples=["FAYDA_FAN"],
    )
    value: str = Field(..., description="The identifier value.", examples=["123456789012"])


# ── describe ─────────────────────────────────────────────────────────────────


class ParameterSpec(BaseModel):
    model_config = ConfigDict(extra="allow")

    type: str = Field(..., description="`string`, `integer`, `number` or `boolean`.", examples=["integer"])
    description: Optional[str] = Field(None, examples=["Crop year, e.g. 2019"])
    required: Optional[bool] = Field(None, description="The partner must send it.")
    default: Optional[Any] = Field(None, description="Used when the partner leaves it out.")
    enum: Optional[List[Any]] = Field(None, description="Allowed values.")
    min: Optional[float] = None
    max: Optional[float] = None


class UseCaseInput(BaseModel):
    subject: Dict[str, List[str]] = Field(
        ..., description="`id_types`: the subject types this use case accepts.",
        examples=[{"id_types": ["FAYDA_FAN", "FARMER_ID"]}],
    )
    parameters: Dict[str, ParameterSpec] = Field(
        ..., description="Parameters the partner may send in `message.parameters`, by name.",
    )
    batch: Dict[str, int] = Field(..., examples=[{"max_subjects": 1}])


class UseCaseSource(BaseModel):
    id: str = Field(..., description="Source ID, used as the key in the response's `sources`.", examples=["farmer"])
    controller: str = Field(..., description="The registry (data controller) it queries.", examples=["farmer-registry"])
    requirement: Literal["mandatory", "optional"] = Field(
        ..., description="A failing mandatory source fails the request; a failing optional one leaves its fields null.",
    )
    depends_on: List[str] = Field(..., description="Sources whose records this one needs first.")
    scopes: List[str] = Field(..., description="Data scopes the consent must grant for this source.")
    optional_scopes: List[str] = Field(..., description="Data scopes the consent may grant; left out, their fields are null.")


class UseCaseDescription(BaseModel):
    """A published use case: what to send and what comes back. Configuration only."""

    use_case: str = Field(..., description="Reference `name@major`.", examples=["loan-profile@1"])
    name: str = Field(..., examples=["loan-profile"])
    version: str = Field(..., examples=["1.0.0"])
    status: str = Field(..., examples=["published"])
    title: str = Field(..., examples=["Farmer loan profile"])
    description: Optional[str] = None
    policy: Optional[str] = Field(None, examples=["credit-assessment"])
    purpose: Optional[str] = Field(None, description="The consent's purpose code must match.",
                                   examples=["credit-assessment"])
    consent: Dict[str, bool] = Field(..., description="`required`: the query needs the subject's consent.",
                                     examples=[{"required": True}])
    input: UseCaseInput
    sources: List[UseCaseSource]
    consent_grants_needed: List[str] = Field(
        ..., description="Registries the consent must have a grant for.",
        examples=[["crop-sown-registry", "farmer-registry"]],
    )
    consent_scopes: Dict[str, Dict[str, List[str]]] = Field(
        ..., description="Per registry, the data scopes the consent's grant must (`required`) and may (`optional`) name.",
        examples=[{"farmer-registry": {"required": ["farmer-registry.farmer_identifiers"],
                                       "optional": ["farmer-registry.land_location"]}}],
    )
    output_fields: List[str] = Field(..., description="Fields of the response's `data`, as dotted paths.",
                                     examples=[["farmer.name", "land.total_size"]])
    source_status: bool = Field(..., description="The response carries `sources` (one status per source).")
    partial_response: str = Field(
        ..., description="`allowed`: a failing optional source still gives a 200; `denied`: any failing source fails the request.",
    )
    rate_per_partner: Optional[str] = Field(None, description="Per-partner rate limit.", examples=["60/min"])


class UseCaseList(BaseModel):
    use_cases: List[UseCaseDescription]


# ── query: request ───────────────────────────────────────────────────────────


class QueryRequestHeader(BaseModel):
    version: str = Field("1.0.0", examples=["1.0.0"])
    message_id: str = Field(..., description="The partner's ID for this request; echoed as `meta.in_reply_to`.",
                            examples=["7f1c2a9e-3a51-4c0e-9d5b-0f6f3f2b1c11"])
    message_ts: str = Field(
        ..., description="ISO-8601 time of the request. Must be within 5 minutes of the Composite's clock.",
        examples=["2026-10-01T10:00:00Z"],
    )
    action: str = Field("query", examples=["query"])
    sender_id: str = Field(
        ..., description="The partner's ID in Partner Management. The signature is checked against this partner's keys.",
        examples=["bank-a"],
    )
    receiver_id: Optional[str] = Field(
        None, description="The Composite's partner ID. When sent, it must match.", examples=["agri-composite"],
    )


class QueryMessage(BaseModel):
    subject: Subject
    parameters: Dict[str, Any] = Field(
        default_factory=dict,
        description="Use-case parameters (see `input.parameters` of the use case). Unknown names are rejected.",
        examples=[{"crop_year": 2019, "season": "SEASON_MEHER"}],
    )
    consent_id: Optional[str] = Field(
        None,
        description="The ID of a consent the subject gave, held by the Exchange's Consent Manager. "
        "Send this or `consent_jws`, not both; send neither to have the Consent Manager look the consent up. "
        "Needs the Composite's Exchange consent mode.",
        examples=["c0a8012e-5b7d-4f0e-9a43-2f1d6e7b9a10"],
    )
    consent_jws: Optional[str] = Field(
        None,
        description="A consent the partner signed: a compact JWS whose claims name the subject, the purpose, "
        "a validity and one grant per registry with its data scopes. Send this or `consent_id`, not both. "
        "With neither (Exchange consent mode only), the Exchange's Consent Manager looks up the consent it holds "
        "from this partner for this subject and the use case's purpose (e.g. one collected in the partner portal).",
        examples=["eyJhbGciOiJFUzI1NiIsImtpZCI6ImJhbmstYS1rZXktMSJ9.eyJzdWJqZWN0X2lkIjp7fX0.c2ln"],
    )


class QueryRequest(BaseModel):
    """The signed request envelope."""

    signature: str = Field(
        ...,
        description="Detached JWS (`<protected>..<signature>`) over the canonical JSON of `{header, message}` "
        "(sorted keys, no whitespace), made with the partner's key. The JWS header names the key (`kid`). "
        "The Composite verifies it against the sender's keys in Partner Management.",
        examples=["eyJhbGciOiJFUzI1NiIsImtpZCI6ImJhbmstYS1rZXktMSJ9..MEUCIQ"],
    )
    header: QueryRequestHeader
    message: QueryMessage


# ── query: response ──────────────────────────────────────────────────────────


class ResponseHeader(BaseModel):
    version: str = Field("1.0.0", examples=["1.0.0"])
    message_id: str = Field(..., description="The Composite's ID for this response.")
    message_ts: str = Field(..., examples=["2026-10-01T10:00:01.234Z"])
    action: str = Field("on-query", examples=["on-query"])
    status: Literal["succ", "rjct"] = Field(..., description="`succ` on 200, `rjct` on an error.")
    sender_id: str = Field(..., description="The Composite's partner ID.", examples=["agri-composite"])
    receiver_id: str = Field(..., description="The partner's ID (the request's `sender_id`).", examples=["bank-a"])
    meta: Dict[str, Any] = Field(default_factory=dict, description="`in_reply_to`: the request's `message_id`.")
    status_reason_code: Optional[str] = Field(None, description="On `rjct`: the error code.")
    status_reason_message: Optional[str] = Field(None, description="On `rjct`: the error message.")


class SourceStatus(BaseModel):
    status: Literal["ok", "no_record", "denied", "unavailable", "error"] = Field(
        ...,
        description="`ok`: records found. `no_record`: the registry has none for the subject. "
        "`denied`: the registry or the consent refused (or the consent does not grant the source's scopes). "
        "`unavailable`: timeout, HTTP 5xx/408/429, or not reachable. `error`: any other failure.",
    )
    detail: Optional[str] = Field(None, description="Why, when not `ok`.")


class QueryResultMessage(BaseModel):
    use_case: str = Field(..., examples=["loan-profile@1"])
    version: str = Field(..., description="The use case version that served the query.", examples=["1.0.0"])
    request_id: str = Field(..., description="The Composite's ID for this query (in its logs and audit events).")
    subject: Subject
    consent_id: Optional[str] = Field(
        None, description="The consent used, as the Exchange's Consent Manager named it (also a looked-up one).",
        examples=["c0a8012e-5b7d-4f0e-9a43-2f1d6e7b9a10"],
    )
    sources: Optional[Dict[str, SourceStatus]] = Field(
        None, description="One status per source, when the use case has `source_status` on.",
    )
    data: Dict[str, Any] = Field(
        ...,
        description="The use case's output fields. A field built from a failed source is null; "
        "an empty list or 0 means the registry has nothing.",
    )


class QueryErrorMessage(BaseModel):
    use_case: str = Field(..., examples=["loan-profile@1"])
    request_id: str
    subject: Optional[Subject] = None
    consent_id: Optional[str] = Field(
        None, description="The consent the Exchange's Consent Manager decided on, when it got that far.")
    sources: Optional[Dict[str, SourceStatus]] = Field(
        None, description="Per-source statuses, when the error is a failing source.",
    )
    error: ApiErrorBody


class QueryResponse(BaseModel):
    """The signed response envelope."""

    signature: str = Field(
        ...,
        description="Detached JWS over the canonical JSON of `{header, message}`, made with the Composite's key. "
        "Verify it against the Composite's keys in Partner Management.",
    )
    header: ResponseHeader
    message: QueryResultMessage


class QueryErrorResponse(BaseModel):
    """The signed error envelope (`header.status` is `rjct`)."""

    signature: str = Field(..., description="Detached JWS made with the Composite's key (empty only for `signing_unavailable`).")
    header: ResponseHeader
    message: QueryErrorMessage


# ── examples ─────────────────────────────────────────────────────────────────

_REQ_HEADER = {
    "version": "1.0.0",
    "message_id": "7f1c2a9e-3a51-4c0e-9d5b-0f6f3f2b1c11",
    "message_ts": "2026-10-01T10:00:00Z",
    "action": "query",
    "sender_id": "bank-a",
    "receiver_id": "agri-composite",
}
_SIG = "eyJhbGciOiJFUzI1NiIsImtpZCI6ImJhbmstYS1rZXktMSJ9..<signature>"

QUERY_REQUEST_EXAMPLES = {
    "consent_lookup": {
        "summary": "No consent: the Exchange looks it up (e.g. collected in the partner portal)",
        "value": {
            "signature": _SIG,
            "header": _REQ_HEADER,
            "message": {
                "subject": {"type": "FAYDA_FAN", "value": "123456789012"},
                "parameters": {"crop_year": 2019, "season": "SEASON_MEHER"},
            },
        },
    },
    "consent_id": {
        "summary": "Consent held by the Exchange (consent_id)",
        "value": {
            "signature": _SIG,
            "header": _REQ_HEADER,
            "message": {
                "subject": {"type": "FAYDA_FAN", "value": "123456789012"},
                "parameters": {"crop_year": 2019, "season": "SEASON_MEHER"},
                "consent_id": "c0a8012e-5b7d-4f0e-9a43-2f1d6e7b9a10",
            },
        },
    },
    "consent_jws": {
        "summary": "Consent the partner signed (consent_jws), by farmer ID",
        "value": {
            "signature": _SIG,
            "header": _REQ_HEADER,
            "message": {
                "subject": {"type": "FARMER_ID", "value": "FR-0007"},
                "parameters": {"crop_year": 2019},
                "consent_jws": "<compact JWS signed by the partner, with grants per registry>",
            },
        },
    },
}

_RESP_HEADER = {
    "version": "1.0.0",
    "message_id": "0b6d1d0e-9e55-4d7a-8b3c-1f2e3d4c5b6a",
    "message_ts": "2026-10-01T10:00:01.234Z",
    "action": "on-query",
    "status": "succ",
    "sender_id": "agri-composite",
    "receiver_id": "bank-a",
    "meta": {"in_reply_to": "7f1c2a9e-3a51-4c0e-9d5b-0f6f3f2b1c11"},
}
_COMPOSITE_SIG = "eyJhbGciOiJFUzI1NiIsImtpZCI6ImFncmktY29tcG9zaXRlIn0..<signature>"

QUERY_RESPONSE_EXAMPLE = {
    "signature": _COMPOSITE_SIG,
    "header": _RESP_HEADER,
    "message": {
        "use_case": "loan-profile@1",
        "version": "1.0.0",
        "request_id": "5e0c7b8a-2f41-4d3e-a1b2-c3d4e5f6a7b8",
        "subject": {"type": "FAYDA_FAN", "value": "123456789012"},
        "consent_id": "c0a8012e-5b7d-4f0e-9a43-2f1d6e7b9a10",
        "sources": {
            "farmer": {"status": "ok"},
            "season_summaries": {"status": "ok"},
            "crop_seasons": {"status": "unavailable", "detail": "registry unavailable (HTTP 503)"},
        },
        "data": {
            "farmer": {"name": {"given_name": "Almaz", "surname": "Bekele"}, "sex": "female",
                       "birth_date": "1985-03-02", "main_crops": ["CROP_BREAD_WHEAT", "CROP_TEFF"]},
            "land": {"parcel_count": 2, "total_size": 1.75,
                     "parcels": [{"land_size": 1.25, "measurement": "Hectare", "land_tenure": "Owned"},
                                 {"land_size": 0.5, "measurement": "Hectare", "land_tenure": "Crop share"}]},
            "crops": {"seasons": None, "total_area_sown_ha": None,
                      "season_summaries": [{"crop_season": {"crop_year": 2019, "season": "SEASON_MEHER"},
                                            "measures": {"area_sown_ha": 1.15}}]},
        },
    },
}


def error_example(http_code: str, message: str, sources: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    msg: Dict[str, Any] = {"use_case": "loan-profile@1", "request_id": "5e0c7b8a-2f41-4d3e-a1b2-c3d4e5f6a7b8"}
    if sources:
        msg["sources"] = sources
    msg["error"] = {"code": http_code, "message": message}
    return {
        "signature": _COMPOSITE_SIG,
        "header": {**_RESP_HEADER, "status": "rjct", "status_reason_code": http_code, "status_reason_message": message},
        "message": msg,
    }


def _error_response(description: str, examples: Dict[str, Dict[str, Any]]) -> Dict[str, Any]:
    return {
        "model": QueryErrorResponse,
        "description": description,
        "content": {"application/json": {"examples": {
            code: {"summary": code, "value": value} for code, value in examples.items()
        }}},
    }


QUERY_RESPONSES: Dict[int | str, Dict[str, Any]] = {
    200: {
        "model": QueryResponse,
        "description": "The signed result. Optional sources that failed show in `sources`; their fields are null.",
        "content": {"application/json": {"example": QUERY_RESPONSE_EXAMPLE}},
    },
    400: _error_response(
        "The envelope or input is not valid: `invalid_envelope`, `wrong_receiver`, `stale_request`, "
        "`invalid_use_case`, `invalid_input`, `consent_id_needs_exchange`.",
        {
            "invalid_input": error_example("invalid_input", "subject type must be one of ['FAYDA_FAN', 'FARMER_ID']"),
            "stale_request": error_example("stale_request", "header.message_ts is more than 300s from now"),
        },
    ),
    401: _error_response(
        "`signature_invalid`: the signature does not verify against the sender's keys in Partner Management "
        "(unknown partner, unknown key or bad signature).",
        {"signature_invalid": error_example(
            "signature_invalid", "the request signature does not verify against the sender's Partner Management key")},
    ),
    403: _error_response(
        "Not allowed: `partner_not_allowed` (the partner has no policy for this use case, `name@major`, in "
        "Partner Management), `consent_required`, `consent_malformed`, "
        "`consent_signature_invalid`, `consent_subject_mismatch`, `consent_not_yet_valid`, `consent_expired`, `consent_grant_missing`, "
        "`consent_scope_missing`, `consent_denied` (the reason in the message, e.g. `consent_not_found` when no "
        "consent was sent and none is held), `consent_receipt_missing` (no consent token for a mandatory "
        "registry), `source_denied` (a mandatory source was denied).",
        {
            "partner_not_allowed": error_example(
                "partner_not_allowed",
                "partner 'bank-b' is not granted the use case loan-profile@1 in Partner Management"),
            "consent_denied": error_example(
                "consent_denied", "the exchange Consent Manager denied the consent (consent_expired)"),
            "consent_not_found": error_example(
                "consent_denied", "the exchange Consent Manager denied the consent (consent_not_found)"),
        },
    ),
    404: _error_response(
        "`unknown_use_case`: no published use case with that name (and major version).",
        {"unknown_use_case": error_example("unknown_use_case", "no published use case 'loan-profile@2'")},
    ),
    429: _error_response(
        "`rate_limited`: the partner went over the use case's rate.",
        {"rate_limited": error_example("rate_limited", "more than 60/min for loan-profile@1")},
    ),
    500: _error_response(
        "`signing_unavailable`: the Composite cannot sign; the envelope then has an empty signature.",
        {"signing_unavailable": error_example(
            "signing_unavailable", "the composite cannot sign responses; check its signing key")},
    ),
    502: _error_response(
        "`source_error` (a mandatory source failed) or `consent_exchange_error` (the Exchange's Consent Manager "
        "gave an answer the Composite cannot use).",
        {"source_error": error_example(
            "source_error", "mandatory source failed: farmer (error)",
            {"farmer": {"status": "error", "detail": "registry rejected the request (HTTP 400)"}})},
    ),
    503: _error_response(
        "`source_unavailable` (a mandatory source is down), `consent_exchange_unavailable`, or "
        "`partner_policy_unavailable` (Partner Management cannot be read to check the partner's use-case grant "
        "and no recent answer is cached).",
        {
            "source_unavailable": error_example(
                "source_unavailable", "mandatory source failed: farmer (unavailable)",
                {"farmer": {"status": "unavailable", "detail": "registry unavailable (HTTP 503)"}}),
            "partner_policy_unavailable": error_example(
                "partner_policy_unavailable",
                "Partner Management cannot be read to check the partner's use-case grant; try again later"),
        },
    ),
    504: _error_response(
        "`source_unavailable`: a mandatory source timed out (or the use case's overall timeout was reached).",
        {"source_unavailable": error_example(
            "source_unavailable", "mandatory source failed: farmer (unavailable)",
            {"farmer": {"status": "unavailable", "detail": "registry timed out"}})},
    ),
}


def _inline(schema: Dict[str, Any]) -> Dict[str, Any]:
    """A model's JSON schema with its $defs inlined (for a request body given in openapi_extra)."""
    defs = schema.pop("$defs", {})

    def resolve(node):
        if isinstance(node, dict):
            ref = node.get("$ref")
            if isinstance(ref, str) and ref.startswith("#/$defs/"):
                return resolve(dict(defs[ref.split("/")[-1]]))
            return {k: resolve(v) for k, v in node.items()}
        if isinstance(node, list):
            return [resolve(v) for v in node]
        return node

    return resolve(schema)


QUERY_REQUEST_BODY = {
    "requestBody": {
        "required": True,
        "content": {"application/json": {
            "schema": _inline(QueryRequest.model_json_schema()),
            "examples": QUERY_REQUEST_EXAMPLES,
        }},
    }
}

USE_CASE_PATH = "The use case: `name` (e.g. `loan-profile`) or `name@major` (e.g. `loan-profile@1`)."

USE_CASE_MAJOR_HEADER = (
    "Major version of the use case, when the path names it without `@major`. "
    "Without either, the highest published major is used."
)

# ── console ──────────────────────────────────────────────────────────────────

CONSOLE_RESPONSES: Dict[int | str, Dict[str, Any]] = {
    401: {"description": "No staff session (or it expired)."},
    403: {"description": "The staff user lacks the `composite:view` permission."},
}
