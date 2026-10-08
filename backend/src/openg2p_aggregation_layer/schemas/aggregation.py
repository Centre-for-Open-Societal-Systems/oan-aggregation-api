"""Wire shapes for the aggregated async flow.

The seek envelope is deliberately the **same shape** as a DCI search request:
``{signature, header, message}``, with ``header.sender_uri`` carrying the
callback and ``search_criteria`` carrying the field list. A partner that already
speaks ``/dci/registry/sync/search`` changes two things — the URL, and
``fields`` instead of a per-registry ``reg_type``. Nothing else in its client
has to move.
"""
from datetime import datetime
from typing import Any, Dict, List, Optional

from pydantic import BaseModel, ConfigDict, Field

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
    # The DCI spec's own field for "where to send the response". Reused rather
    # than inventing a callback_url, so the envelope stays standard.
    sender_uri: Optional[str] = Field(
        default=None, description="Callback URL the on-search is POSTed to")
    total_count: Optional[int] = None
    is_msg_encrypted: bool = False
    meta: Dict[str, Any] = Field(default_factory=dict)


class SeekQueryValue(BaseModel):
    model_config = ConfigDict(extra="allow")
    id_type: str = "functional_id"
    id_value: str


class SeekQuery(BaseModel):
    model_config = ConfigDict(extra="allow")
    type: str = "idtype-value"
    value: SeekQueryValue


class SeekAuthorize(BaseModel):
    model_config = ConfigDict(extra="allow")
    consent_jws: str = Field(
        description="The partner's consent object for the AGGREGATOR binding. "
                    "Its data scopes are field aliases, not registry blocks.")


class SeekCriteria(BaseModel):
    model_config = ConfigDict(extra="allow")

    version: str = "1.0.0"
    query_type: str = "idtype-value"
    query: SeekQuery
    # The whole point of the endpoint: fields from any registry, one list.
    fields: List[str] = Field(
        min_length=1,
        description="Catalog aliases, e.g. farmer.firstname, livestock.UIN")
    authorize: SeekAuthorize
    purpose: Optional[Dict[str, Any]] = None
    # Each registry keys on its own functional id, so one id_value cannot match
    # all of them. Name the exceptions here; query.value.id_value is the default
    # for any registry not listed.
    registry_queries: Optional[Dict[str, str]] = Field(
        default=None,
        description='Per-registry query id, e.g. {"livestock": "LS-000000000001"}')


class SeekRequestItem(BaseModel):
    model_config = ConfigDict(extra="allow")
    reference_id: str = Field(max_length=99)
    timestamp: Optional[str] = None
    search_criteria: SeekCriteria


class SeekMessage(BaseModel):
    model_config = ConfigDict(extra="allow")
    transaction_id: Optional[str] = Field(default=None, max_length=99)
    search_request: List[SeekRequestItem] = Field(min_length=1)


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
    status: str = Field(description="rcvd | pdng")
    otp_required: bool = True
    otp_channel: Optional[str] = None
    otp_expires_at: Optional[datetime] = None
    accepted_fields: List[str] = Field(default_factory=list)
    registries: List[str] = Field(default_factory=list)
    callback_url: Optional[str] = None
    message: Optional[str] = None
    # Set when the subject had no consent for this partner and one was raised
    # for them. The partner sends them to consent_url; the OTP lives there.
    consent_request_id: Optional[str] = None
    consent_url: Optional[str] = None


class VerifyOtpRequest(BaseModel):
    otp: str = Field(min_length=4, max_length=10)


class FarmerConsentValidateRequest(BaseModel):
    """The farmer releasing their own data, in one flat body.

    Either identifier works: ``aggregation_id`` is what the ack returned, and
    ``correlation_id`` is what travels on the DCI envelope, so whichever the
    caller happens to be holding is accepted.
    """

    model_config = ConfigDict(extra="forbid")

    aggregation_id: Optional[str] = Field(
        default=None, description="From the seek ack. Either this or correlation_id.")
    correlation_id: Optional[str] = Field(
        default=None, description="From the DCI envelope. Either this or aggregation_id.")
    subject_id: Optional[SubjectId] = Field(
        default=None,
        description="Optional cross-check. The farmer is taken from the bearer "
                    "token; if this is supplied it must agree with it. It cannot "
                    "establish identity on its own.")
    otp: str = Field(min_length=4, max_length=10)


class FarmerConsentValidateResponse(BaseModel):
    aggregation_id: str
    status: str
    subject_id: SubjectId
    released_fields: List[str]
    released_to: Optional[str] = Field(
        default=None, description="The partner the data is being sent to")
    callback_url: Optional[str] = None
    message: str


class VerifyOtpResponse(BaseModel):
    aggregation_id: str
    status: str
    message: str


class AggregationStatusResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True, extra="allow")

    id: str
    status: str
    subject_id_type: str
    subject_id_value: str
    requested_fields: List[str]
    correlation_id: str
    callback_url: str
    # Whether the partner's policy demanded a code for this fetch. Without it a
    # status of 'verified' with no otp_verified_at reads as a missing record
    # rather than a policy decision.
    otp_required: bool = True
    otp_verified_at: Optional[datetime] = None
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
