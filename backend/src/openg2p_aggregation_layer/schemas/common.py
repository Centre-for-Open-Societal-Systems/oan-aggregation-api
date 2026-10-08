from datetime import datetime
from enum import Enum
from typing import Generic, List, Optional, TypeVar

from pydantic import BaseModel, Field


class ReasonCode(str, Enum):
    ok = "ok"
    malformed_object = "malformed_object"
    unknown_partner = "unknown_partner"
    signature_invalid = "signature_invalid"
    audience_mismatch = "audience_mismatch"
    subject_not_allowed = "subject_not_allowed"
    purpose_not_allowed = "purpose_not_allowed"
    scope_exceeds_policy = "scope_exceeds_policy"
    validity_exceeds_policy = "validity_exceeds_policy"
    expired = "expired"
    revoked = "revoked"
    replay = "replay"
    no_subject_consent = "no_subject_consent"


class SubjectId(BaseModel):
    type: str = Field(..., examples=["national_id"])
    value: str = Field(..., examples=["FARMER_1234"])


class Problem(BaseModel):
    """Standard error body for non-decision endpoints."""

    error: str
    detail: Optional[str] = None
    trace_id: Optional[str] = None


DataT = TypeVar("DataT")


class Paginated(BaseModel, Generic[DataT]):
    items: List[DataT]
    total: int
    page: int
    size: int
    pages: int


class StatusResponse(BaseModel):
    consent_id: str
    status: str
    valid_until: Optional[datetime] = None
    checked_at: datetime
