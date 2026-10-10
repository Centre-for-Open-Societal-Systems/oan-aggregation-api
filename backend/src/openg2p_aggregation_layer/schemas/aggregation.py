"""Wire shapes for the aggregated async flow.

The partner's call is a **DCI search envelope** carrying a **Beneficiary-360
request**:

``{signature, header, message}``
    The DCI transport, as on a registry's own ``/dci/registry/sync/search``.
    ``header.sender_uri`` is the callback (DCI's own field for "where to send
    the response"), ``message.transaction_id`` / ``reference_id`` are echoed on
    the on-search.
``search_criteria.query_type = "beneficiary360"`` + ``search_criteria.query``
    The bene-360 request, exactly as ``request.schema.json`` defines it. That
    schema forbids extra properties, so nothing of this service's own goes in
    it.
``search_criteria.authorize.consent_jws`` / ``search_criteria.purpose``
    What this service needs besides the query - the partner's signed consent
    object, and optionally the purpose - in the places DCI already has for
    them.

The answer comes back the same way: a signed DCI ``on-search`` whose
``reg_records[0]`` is a bene-360 response.
"""
from datetime import datetime
from typing import Any, Dict, List, Literal, Optional

from pydantic import BaseModel, ConfigDict, Field

from ..bene360 import QUERY_TYPE, Beneficiary360Request
from .common import SubjectId


# ── request ─────────────────────────────────────────────────────────────────

class SeekHeader(BaseModel):
    model_config = ConfigDict(extra="allow")

    version: str = "1.0.0"
    message_id: Optional[str] = None
    message_ts: Optional[str] = None
    action: str = "search"
    sender_id: Optional[str] = None
    receiver_id: Optional[str] = None
    sender_uri: Optional[str] = Field(
        default=None, description="Callback URL the on-search is POSTed to")
    total_count: Optional[int] = None
    is_msg_encrypted: bool = False
    meta: Dict[str, Any] = Field(default_factory=dict)


class SeekAuthorize(BaseModel):
    model_config = ConfigDict(extra="allow")
    consent_jws: str = Field(
        description="The partner's consent object for its binding with this "
                    "service. Its data_scopes are scope ids "
                    "(<registryCode>.<scope>, see GET /aggregation/v1/registries).")


class SeekCriteria(BaseModel):
    model_config = ConfigDict(extra="allow")

    version: str = "1.0.0"
    query_type: Literal["beneficiary360"] = Field(
        description="Always '%s': query is a Beneficiary-360 request" % QUERY_TYPE)
    query: Beneficiary360Request
    authorize: SeekAuthorize
    purpose: Optional[Dict[str, Any]] = Field(
        default=None, description="Defaults to the consent object's purpose")


class SeekRequestItem(BaseModel):
    model_config = ConfigDict(extra="allow")
    reference_id: str = Field(max_length=99)
    timestamp: Optional[str] = None
    search_criteria: SeekCriteria


class SeekMessage(BaseModel):
    model_config = ConfigDict(extra="allow")
    transaction_id: Optional[str] = Field(default=None, max_length=99)
    # One beneficiary per call: each one has its own consent, OTP and callback.
    search_request: List[SeekRequestItem] = Field(min_length=1, max_length=1)


class SeekEnvelope(BaseModel):
    model_config = ConfigDict(extra="allow")
    signature: Optional[str] = None
    header: SeekHeader
    message: SeekMessage


# ── ack ─────────────────────────────────────────────────────────────────────

class SeekAck(BaseModel):
    """Returned immediately. Carries no data by design — the subject has not
    authorised anything yet."""

    model_config = ConfigDict(extra="allow")

    aggregation_id: str
    correlation_id: str
    transaction_id: Optional[str] = None
    status: str = Field(description="pdng")
    otp_required: bool = True
    otp_channel: Optional[str] = None
    otp_expires_at: Optional[datetime] = None
    accepted_scopes: List[str] = Field(default_factory=list)
    registries: List[str] = Field(default_factory=list)
    callback_url: Optional[str] = None
    message: Optional[str] = None
    # Set when the subject had no consent for this partner and one was raised
    # for them. The partner sends them to consent_url; the OTP lives there.
    consent_request_id: Optional[str] = None
    consent_url: Optional[str] = None


# ── subject ─────────────────────────────────────────────────────────────────

class VerifyOtpRequest(BaseModel):
    """The subject releasing their own data.

    The subject is taken from the bearer token. ``subject_id`` is an optional
    cross-check that must agree with it; it cannot establish identity alone.
    """

    model_config = ConfigDict(extra="forbid")

    otp: str = Field(min_length=4, max_length=10)
    subject_id: Optional[SubjectId] = None


class VerifyOtpResponse(BaseModel):
    aggregation_id: str
    status: str
    subject_id: SubjectId
    released_scopes: List[str]
    released_to: Optional[str] = Field(
        default=None, description="The partner the data is being sent to")
    message: str


class AggregationStatusResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True, extra="allow")

    id: str
    status: str
    subject_id_type: str
    subject_id_value: str
    requested_scopes: List[str]
    # The bene-360 request as the partner sent it.
    query: Dict[str, Any] = Field(default_factory=dict)
    correlation_id: str
    callback_url: str
    # Whether the partner's policy demanded a code for this fetch. Without it a
    # status of 'verified' with no otp_verified_at reads as a missing record
    # rather than a policy decision.
    otp_required: bool = True
    otp_verified_at: Optional[datetime] = None
    lawful_basis: Optional[str] = None
    # The consent request raised for the subject (if any), the CM consent
    # record this fetch stands on, and the grant spent at each registry.
    consent_request_id: Optional[str] = None
    cm_consent_id: Optional[str] = None
    grant_ids: Dict[str, str] = Field(default_factory=dict)
    registry_results: Dict[str, Any] = Field(default_factory=dict)
    callback_status: Optional[int] = None
    callback_attempts: int = 0
    delivered_at: Optional[datetime] = None
    failure_reason: Optional[str] = None
    # Queue state. A request sitting in 'fetched' with a next_retry_at is
    # backing off, not stuck, and the difference is the first thing anyone
    # looking at a late callback needs to know. fetch_attempts above 1 means a
    # worker died mid-fetch and the registries were queried more than once.
    fetch_attempts: int = 0
    next_retry_at: Optional[datetime] = None
    created_at: datetime
