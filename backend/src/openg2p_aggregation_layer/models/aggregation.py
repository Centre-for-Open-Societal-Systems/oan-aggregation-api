"""The aggregation request — one partner ask, spanning several registries.

This is the state machine behind the async ``seek`` flow. The partner's call
returns an ack immediately, so everything about the request has to survive the
response: what was asked for, where to call back, whether the subject
has proved possession of the OTP yet, and what happened when the callback was
attempted.

    received ──OTP sent──► pending_otp ──code ok──► verified
       │                       │                          │
       │ policy requires       │ wrong code x N           │ published to
       │ no OTP                │ or expired               ▼ aggregation.fanout
       └───────────────────────┼──────────────────►   queued
                               ▼                          │ a worker claims it
                            rejected                      ▼
                                                      fetching
                                                          │ registries answered,
                                                          ▼ envelope published
                                                      fetched
                                                          │ a worker claims it
                                                          ▼
                                                     delivering
                                                    ┌─────┴─────┐
                                             2xx    │           │ retries exhausted
                                                    ▼           ▼
                                              delivered      failed

``queued``/``fetching``/``delivering`` exist only so that at-least-once
delivery cannot do the work twice. A worker claims a row with a conditional
UPDATE on ``status``; if it changes no rows, another worker already has it and
this copy of the message is dropped. Without Kafka the same statuses are still
walked, just by an in-process task — so a row's history reads the same either
way.

Whether the OTP leg is walked at all is the partner's policy: ``seek`` reads
``required_auth_method`` from the CM once and records the answer on
``otp_required``. NULL skips straight to ``verified``.

A row parked at ``pending_consent`` waits on a consent request raised in the
CM. Nothing pushes the answer here: the poller (``AggregatorService.sync_with_cm``)
reads the request's status from the CM's generic API and releases or rejects
the row. The same poll, and a check right before the fan-out and before the
callback, read the status of ``cm_consent_id`` so a withdrawal in the CM stops
the fetch.
"""
from datetime import datetime
from enum import Enum
from typing import Optional

from sqlalchemy import Boolean, DateTime, Integer, String, Text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from .base import BaseORMModelWithId


class AggregationStatus(str, Enum):
    received = "received"        # ack sent, nothing else has happened yet
    # The subject had no consent for this partner, so one was raised on their
    # behalf and is waiting on the consent screen. The OTP belongs to THAT
    # request, not to this one — the CM reports it as otp_verified_at.
    pending_consent = "pending_consent"
    pending_otp = "pending_otp"  # OTP issued to the subject, awaiting the code
    verified = "verified"        # subject proved the OTP; fan-out may proceed
    # ── the queued stages. Each is a claim held by exactly one worker. ──────
    queued = "queued"            # on aggregation.fanout, no worker yet
    fetching = "fetching"        # a worker is querying the registries
    fetched = "fetched"          # registries answered; envelope is on the
                                 # delivery topic, awaiting its callback
    delivering = "delivering"    # a worker is POSTing to the partner
    delivered = "delivered"      # callback accepted the aggregated payload
    failed = "failed"            # fan-out or callback failed; see failure_reason
    rejected = "rejected"        # OTP failed, consent denied or withdrawn


class AggregationRequest(BaseORMModelWithId):
    """One ``POST /dci/registry/async/search``.

    ``query`` is the partner's Beneficiary-360 request verbatim and
    ``requested_scopes`` the scope ids (``<registryCode>.<scope>``) it stands
    on - at seek what was permitted or asked for, after a raised consent is
    approved what the subject granted. Both are kept as sent rather than
    resolved against the registry catalog, so an audit shows what was *asked*
    even if the catalog changes later.
    """

    __tablename__ = "aggregation_requests"

    # ── who is asking, and for what ─────────────────────────────────────────
    partner_id: Mapped[str] = mapped_column(String, index=True)
    partner_audience: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)
    subject_id_type: Mapped[str] = mapped_column(String(50), index=True)
    subject_id_value: Mapped[str] = mapped_column(String(255), index=True)
    requested_scopes: Mapped[list] = mapped_column(JSONB, default=list)
    query: Mapped[dict] = mapped_column(JSONB, default=dict)
    purpose: Mapped[dict] = mapped_column(JSONB, default=dict)
    # Set only when the seek had to raise a consent request itself. Approving
    # that request is what releases this aggregation.
    consent_request_id: Mapped[Optional[str]] = mapped_column(
        String, nullable=True, index=True)
    # The partner's consent object, kept ONLY while the row is parked on a
    # raised consent request. On approval the poller asks the CM's /validate
    # again with it, which is how the subject's granted scopes are learnt (the
    # CM narrows to them). Cleared as soon as the row leaves pending_consent.
    consent_jws: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    # The CM consent record this fetch stands on: the consent_id /validate
    # returned for the partner's object. Its status (GET
    # /consent/v1/consents/{id}/status) is what tells this service that the
    # subject withdrew - the CM revokes it with the consent it hangs off.
    cm_consent_id: Mapped[Optional[str]] = mapped_column(
        String, nullable=True, index=True)
    # Per registry, the AggregationGrant this fetch spends: {"<registryCode>": "<id>"}.
    grant_ids: Mapped[dict] = mapped_column(JSONB, default=dict)

    # ── DCI correlation. transaction_id is the partner's; correlation_id is
    # ours and is what comes back on the callback. ───────────────────────────
    transaction_id: Mapped[Optional[str]] = mapped_column(String(99), nullable=True, index=True)
    correlation_id: Mapped[str] = mapped_column(String(99), index=True)
    reference_id: Mapped[Optional[str]] = mapped_column(String(99), nullable=True)
    callback_url: Mapped[str] = mapped_column(Text)

    # Whether this partner's policy demanded a one-time code for THIS fetch,
    # decided once in ``seek`` and written down rather than inferred later: an
    # absent otp_hash cannot tell "no code was required" from "no code was ever
    # sent". Everything downstream — the auth_method recorded on the grant, and
    # the subject_authentication the partner is told — reads this.
    otp_required: Mapped[bool] = mapped_column(Boolean, default=True)

    # Why this fetch was allowed: "consent" (the subject granted one) or
    # "legitimate_interest" (an internal partner; no grant was sought). Written
    # per request, because a partner's policy can change afterwards and the
    # audit has to say what was true when the data moved.
    lawful_basis: Mapped[str] = mapped_column(String(40), default="consent")

    # ── OTP. The code itself is NEVER stored — only its hash, exactly as the
    # ID token is only ever stored hashed in AuthContext. ───────────────────
    otp_hash: Mapped[Optional[str]] = mapped_column(String(128), nullable=True)
    otp_expires_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True)
    otp_attempts: Mapped[int] = mapped_column(Integer, default=0)
    otp_verified_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True)
    otp_channel: Mapped[Optional[str]] = mapped_column(String(50), nullable=True)
    otp_destination: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)
    # Which backend issued it, and its own handle on the exchange (the Mock
    # Identity System's transactionId, plus the kycToken it returns on success).
    # Recording this is what lets an audit say WHAT authorised a fetch rather
    # than only that something did.
    otp_provider: Mapped[Optional[str]] = mapped_column(String(50), nullable=True)
    otp_reference: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)
    # DEV ONLY, written only when otp_debug_enabled. Holding the plaintext is
    # exactly what the rest of this design avoids; it exists so the flow can be
    # exercised from a tool that cannot read the service log. Never enable this
    # where real subjects exist.
    otp_debug_code: Mapped[Optional[str]] = mapped_column(String(16), nullable=True)

    # ── outcome ─────────────────────────────────────────────────────────────
    status: Mapped[str] = mapped_column(
        String(20), default=AggregationStatus.received.value, index=True)
    # Which registries answered, and with what — kept for the audit trail and
    # for explaining a partial result without re-running the fan-out.
    registry_results: Mapped[dict] = mapped_column(JSONB, default=dict)
    delivered_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True)
    callback_status: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    callback_attempts: Mapped[int] = mapped_column(Integer, default=0)
    failure_reason: Mapped[Optional[str]] = mapped_column(Text, nullable=True)

    # ── queue bookkeeping ───────────────────────────────────────────────────
    # How many times a worker has started the registry fan-out for this row.
    # More than one means a worker died mid-fetch and the reaper released it;
    # it is the number worth alerting on, because each attempt is real load on
    # every registry involved.
    fetch_attempts: Mapped[int] = mapped_column(Integer, default=0)
    # Which worker currently holds the claim, and since when. Together these
    # are what lets `python -m openg2p_aggregation_layer.reap` tell a fetch that
    # is merely slow from one whose process is gone.
    claimed_by: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    claimed_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True)
    # When the next callback attempt is due. Informational — the wait itself is
    # held on the retry topic, not polled from here — but it is what makes a
    # backing-off delivery legible in the status endpoint instead of looking
    # stuck.
    next_retry_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True)
