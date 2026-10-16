"""Wire shapes for the aggregated fetch (sync and async).

The partner's call is a **DCI search envelope** carrying a **Beneficiary-360
request**:

``{signature, header, message}``
    The DCI transport, as on a registry's own ``/dci/registry/sync/search``.
    ``header.sender_uri`` is the callback on the async route (DCI's own field
    for "where to send the response"), ``message.transaction_id`` /
    ``reference_id`` are echoed on the on-search.
``search_criteria.query_type = "beneficiary360"`` + ``search_criteria.query``
    The bene-360 request, exactly as ``request.schema.json`` defines it. That
    schema forbids extra properties, so nothing of this service's own goes in
    it.
``search_criteria.authorize.consent_jws`` / ``search_criteria.purpose``
    What this service needs besides the query - the partner's signed consent
    object, and optionally the purpose - in the places DCI already has for
    them.

The answer comes back the same way: a signed DCI ``on-search`` whose
``reg_records[0]`` is a bene-360 response - as the HTTP response on the sync
route, POSTed to ``sender_uri`` on the async one.
"""
from typing import Any, Dict, List, Literal, Optional

from pydantic import BaseModel, ConfigDict, Field, model_validator

from ..bene360 import QUERY_TYPE, Beneficiary360Request


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
        default=None,
        description="Async route only: the callback URL the on-search is POSTed to")
    total_count: Optional[int] = None
    is_msg_encrypted: bool = False
    meta: Dict[str, Any] = Field(default_factory=dict)


class SeekAuthorize(BaseModel):
    """Exactly one of ``consent_jws`` / ``consent_id``."""

    model_config = ConfigDict(extra="allow")
    consent_jws: Optional[str] = Field(
        default=None,
        description="The partner's consent object for its binding with this "
                    "service. Its data_scopes are scope ids "
                    "(<registryCode>.<scope>, see GET /aggregation/v1/registries).")
    consent_id: Optional[str] = Field(
        default=None, max_length=99,
        description="A consent the Consent Manager holds for the calling partner "
                    "(e.g. granted when the subject's one-time code was verified). "
                    "Needs the partner's bearer token (partner realm).")

    @model_validator(mode="after")
    def _one_consent(self):
        if bool(self.consent_jws) == bool(self.consent_id):
            raise ValueError("authorize needs exactly one of consent_jws or consent_id")
        return self


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
    # One beneficiary per call: each one has its own consent.
    search_request: List[SeekRequestItem] = Field(min_length=1, max_length=1)


class SeekEnvelope(BaseModel):
    model_config = ConfigDict(extra="allow")
    signature: Optional[str] = None
    header: SeekHeader
    message: SeekMessage


# ── async ack ───────────────────────────────────────────────────────────────

class SeekAck(BaseModel):
    """Returned by the async route once the call has been accepted. Carries no
    data: the on-search follows on ``callback_url``."""

    model_config = ConfigDict(extra="allow")

    correlation_id: str
    transaction_id: Optional[str] = None
    status: str = Field(description="pdng")
    accepted_scopes: List[str] = Field(default_factory=list)
    registries: List[str] = Field(default_factory=list)
    callback_url: Optional[str] = None
    message: Optional[str] = None
