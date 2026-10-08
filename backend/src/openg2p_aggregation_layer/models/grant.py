"""The subject's authorisation for one registry hop, kept by this service.

Inside consent-management the aggregator wrote this as an originated
``ConsentArtefact`` per registry binding, so the registry's PDP would find a
subject grant (B8) for the aggregator. The Consent Manager no longer records
anything for the aggregator: its registry bindings run on the
``legitimate_interest`` lawful basis, so the CM caps each hop at the binding's
policy ceiling and asks for no subject grant.

That moves the subject's side of the hop here, and this table is it. A
registry is only called while a grant for (partner, subject, registry) is
``active`` and unexpired, and the grant records what the subject actually did:
which consent it stands on (``cm_consent_id``), how they authenticated
(``auth_method``), when, and under which lawful basis. Withdrawing the consent
in the CM revokes every grant that stands on it, the next time this service
reads the consent's status.

    active ──consent withdrawn / expired in the CM──► revoked
       └──────────────valid_until passed──────────────► expired (read lazily)
"""
from datetime import datetime
from enum import Enum
from typing import Optional

from sqlalchemy import DateTime, String
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from .base import BaseORMModelWithId


class GrantStatus(str, Enum):
    active = "active"
    revoked = "revoked"
    expired = "expired"


class AggregationGrant(BaseORMModelWithId):
    __tablename__ = "aggregation_grants"

    # ── whose data, released to whom, through which registry ────────────────
    partner_id: Mapped[str] = mapped_column(String, index=True)
    partner_audience: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)
    subject_id_type: Mapped[str] = mapped_column(String(50), index=True)
    subject_id_value: Mapped[str] = mapped_column(String(255), index=True)
    registry: Mapped[str] = mapped_column(String(50), index=True)
    # The CM binding the hop is validated against (aggregator_registries[...]
    # .audience) - legitimate_interest, so the CM itself grants nothing to it.
    registry_audience: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)
    # Registry block names, exactly what the hop's consent object asks for.
    scopes: Mapped[list] = mapped_column(JSONB, default=list)
    purpose: Mapped[dict] = mapped_column(JSONB, default=dict)

    # ── what the subject did ────────────────────────────────────────────────
    # "otp" (a code was answered, here or on the CM consent screen), "consent"
    # (the standing consent alone, the partner's policy asked for no code) or
    # "none" (legitimate_interest - nobody was asked).
    auth_method: Mapped[str] = mapped_column(String(20))
    auth_timestamp: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True)
    otp_channel: Mapped[Optional[str]] = mapped_column(String(50), nullable=True)
    lawful_basis: Mapped[str] = mapped_column(String(40), default="consent")
    # The CM records this grant stands on. Withdrawal is read from the first.
    cm_consent_id: Mapped[Optional[str]] = mapped_column(
        String, nullable=True, index=True)
    consent_request_id: Mapped[Optional[str]] = mapped_column(String, nullable=True)
    # The aggregation that minted it. A reused grant keeps its first one; the
    # aggregations spending it point here through AggregationRequest.grant_ids.
    aggregation_id: Mapped[Optional[str]] = mapped_column(String, nullable=True)

    # ── lifetime ────────────────────────────────────────────────────────────
    valid_until: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    status: Mapped[str] = mapped_column(
        String(20), default=GrantStatus.active.value, index=True)
    revoked_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True)
    revoke_reason: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)
