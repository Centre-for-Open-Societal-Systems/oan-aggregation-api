"""Orchestration for the async, OTP-gated, cross-registry fetch.

The partner makes ONE call - a Beneficiary-360 request for one beneficiary -
covering any number of registries in the registry catalog. It gets an ack
straight back; the bene-360 response arrives later at its callback, and only
after the subject has authorised the release.

Where each check happens, and why:

``seek``
    The partner's consent object is validated by the Consent Manager's
    ``/consent/v1/validate`` — the same PDP the registries call.
    That buys signature verification against Partner Management, the policy
    ceiling, the replay window and the B8 subject-grant narrowing for free, and
    means the aggregator has not invented a second, weaker way to be trusted.

    The partner's CM binding with this service carries **scope ids** -
    ``<registryCode>.<scope>``, one per top-level block of a registry's record
    (``registry_catalog.py``). The CM treats scopes as opaque strings, so this
    needs no CM change, and ``effective_data_scopes`` means exactly "the
    registry blocks this partner may receive". The query's ``foundationalId``
    must be the consent's subject: a consent for one person never fetches
    another.

``verify_otp``
    The subject's real-time authorisation. Consent says the partner *may* hold
    this data; the OTP says the subject agrees to release it *now*.

``fan-out``
    One call per registry, to its unchanged ``/dci/registry/sync/search``,
    searching by the foundational ID with the catalog's ``id_type``. The
    registry validates each hop with the CM, which caps it at the aggregator's
    binding (lawful basis ``legitimate_interest``: the CM seeks no subject
    grant on the hop). The subject's side of the hop is enforced HERE, before
    the call: a hop goes out only on an active ``AggregationGrant`` and only
    while the CM still reports the partner's consent as active. Registry
    failures are recorded per registry rather than failing the whole request —
    a partner asking across three registries should not lose two because one
    is down.

``sync_with_cm``
    The CM pushes nothing. Approval, denial and withdrawal are read back from
    its generic APIs by a poll (in the API process, the worker, and every
    reaper run), and once more right before the fan-out and before the
    callback, so a decision taken in the CM takes effect here.

``deliver``
    Each registry's record is projected to the catalog's allowed fields and
    mapped onto a bene-360 response (``bene360.py``), which is signed and
    POSTed to the callback as ``reg_records[0]`` of a standard DCI
    ``on-search``. A registry that failed, was not consented or is unknown is
    explained in ``meta.warnings``.
"""
import asyncio
import logging
import os
import socket
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional

import httpx
from openg2p_fastapi_common.service import BaseService
from sqlalchemy import func, or_, select, update

from .. import bene360
from ..config import Settings
from ..db import async_session
from ..models import AggregationGrant, AggregationRequest, AggregationStatus, GrantStatus
from ..kafka_bus import TYPE_FANOUT_REQUESTED, bus, envelope
from ..registry_catalog import SCOPE_SEPARATOR, get_catalog
from .cm_client import CMClient, CMError
from .otp_provider import OtpError
from .otp_service import OtpService
from .registry_client import RegistryClient, RegistryError

_config = Settings.get_config()
_logger = logging.getLogger(_config.logging_default_logger_name)

#: Namespace for the deterministic on-search ``message_id``. Fixed for the
#: lifetime of the service: changing it would make every redelivery look like a
#: new message to partners that de-duplicate on it.
_MESSAGE_NS = uuid.UUID("6f0f5b1e-6a4e-5a0b-9d2a-0c1f8a3e7b40")

#: Who holds a claim. Host plus pid is enough to find the process that stopped,
#: and short enough for the column.
WORKER_ID = ("%s:%d" % (socket.gethostname(), os.getpid()))[:64]


#: Rows whose consent can still be withdrawn from under them. ``pending_consent``
#: waits on a request the CM has not decided; ``delivering`` has already sent.
_WITHDRAWABLE = tuple(s.value for s in (
    AggregationStatus.received, AggregationStatus.pending_otp,
    AggregationStatus.verified, AggregationStatus.queued,
    AggregationStatus.fetching, AggregationStatus.fetched))


def _aware(dt: Optional[datetime]) -> Optional[datetime]:
    if dt is None:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


class AggregationError(Exception):
    def __init__(self, status: int, reason: str, detail: str = ""):
        self.status, self.reason, self.detail = status, reason, detail
        super().__init__(detail or reason)


class AggregatorService(BaseService):
    def __init__(self, name="", **kwargs):
        super().__init__(name if name else "AggregatorService", **kwargs)
        self.cm = CMClient.get_component()
        self.registries = RegistryClient.get_component()
        self.otp = OtpService.get_component()

    async def _otp_required_for(self, partner_id: str) -> bool:
        """Does this partner's active policy demand a one-time code?

        The same ``required_auth_method`` the consent screen obeys, read here so
        one setting covers both gates - a request only ever reaches one of them.
        An unknown partner keeps the factor: failing open on a missing policy is
        the one outcome this must never have.
        """
        if not partner_id:
            return True
        policy = await self.cm.get_policy(partner_id)
        if policy is None:
            return True
        return (policy.get("required_auth_method") or "") == "otp"

    # ── 1. seek ─────────────────────────────────────────────────────────────

    async def seek(self, *, consent_jws: str, query: Dict[str, Any], callback_url: str,
                   transaction_id: Optional[str], reference_id: Optional[str],
                   purpose: Optional[Dict[str, Any]]) -> AggregationRequest:
        """Validate, record, issue the OTP. Returns the row the ack is built from.

        ``query`` is the partner's bene-360 request (already schema-checked).
        Deliberately does NOT fetch anything: the whole point of the flow is
        that no registry is touched until the subject has answered.
        """
        if not callback_url:
            raise AggregationError(
                400, "no_callback",
                "header.sender_uri must carry the callback URL for on-search")
        if not callback_url.lower().startswith(("http://", "https://")):
            raise AggregationError(400, "bad_callback", "sender_uri must be an http(s) URL")

        catalog = get_catalog()
        if "REGISTRIES" not in bene360.requested_sections(query):
            raise AggregationError(
                422, "section_not_supported",
                "only REGISTRIES can be answered; PROGRAMS, DISBURSEMENTS and "
                "BRIDGE_PROCESSING have no source connected")
        registries = bene360.planned_registries(catalog, query)
        if not registries:
            raise AggregationError(
                422, "no_registry",
                "registryFilter names no registry in this service's catalog "
                "(GET /aggregation/v1/registries)")
        # Ask the CM only about the blocks of the registries in play. The CM
        # answers consented ∩ policy ∩ requested, so a scope the partner's
        # object carries for a registry it filtered out is never released.
        requestable = catalog.scope_ids(registries)

        # The CM's PDP decides whether this partner may hold these blocks.
        try:
            decision = await self.cm.validate(consent_jws, requestable)
        except CMError as exc:
            raise AggregationError(exc.status, exc.reason, exc.detail) from exc
        if decision.get("decision") != "permit":
            reason = str(decision.get("reason_code") or "deny")
            # The subject has never been asked. Rather than making the partner
            # go and raise a consent request itself - which is what forced the
            # subject to consent twice, once on the consent screen and again
            # with an OTP here - raise it for them and park this aggregation
            # until it is approved.
            if reason == "no_subject_consent" and _config.aggregator_raise_consent:
                return await self._raise_consent_request(
                    consent_jws=consent_jws, query=query, requestable=requestable,
                    callback_url=callback_url, transaction_id=transaction_id,
                    reference_id=reference_id, purpose=purpose,
                )
            raise AggregationError(
                403, reason,
                decision.get("detail") or "consent did not permit this request")

        granted = self._granted_scopes(requestable, decision.get("effective_data_scopes"))

        subject = decision.get("subject_id") or {}
        self._check_subject(query, subject.get("value"), get_catalog().same_identifier)

        # Who the partner is: the CM looked the binding up by the object's
        # ``aud`` and verified the signature against that partner's key, so
        # after a permit ``aud`` IS the partner - never anything else in the
        # partner's own body. Its CM id comes from config or the CM's list.
        partner_audience = self.cm.decode_claims(consent_jws).get("aud")
        try:
            partner_id = await self.cm.partner_id_for(partner_audience) or ""
        except CMError as exc:
            raise AggregationError(exc.status, exc.reason, exc.detail) from exc
        if not partner_id:
            # Not fatal: the OTP check below keeps the factor for an unknown
            # partner, and nothing else needs the id before release.
            _logger.warning("No CM partner id for audience '%s'; the OTP is kept",
                            partner_audience)

        request = AggregationRequest(
            partner_id=partner_id,
            partner_audience=partner_audience,
            subject_id_type=subject.get("type", ""),
            subject_id_value=subject.get("value", ""),
            requested_scopes=granted,
            query=query,
            purpose=purpose or {},
            transaction_id=transaction_id,
            correlation_id=uuid.uuid4().hex,
            reference_id=reference_id,
            callback_url=callback_url,
            status=AggregationStatus.received.value,
            lawful_basis=decision.get("lawful_basis") or "consent",
            # The CM record this permit was minted as. The CM revokes it with
            # the subject's consent, so its status is how a withdrawal is seen.
            cm_consent_id=decision.get("consent_id"),
            registry_results={},
        )
        in_play = sorted(catalog.split_scope_ids(granted))

        # An internal partner is never sent to find a subject who can enter a
        # code: under legitimate_interest there is no consent screen and nobody
        # to put in front of one, so the OTP gate does not apply at all.
        if request.lawful_basis != "consent":
            request.otp_required = False
            request.status = AggregationStatus.verified.value
            async with async_session()() as session:
                session.add(request)
                await session.commit()
                await session.refresh(request)
            _logger.info("Aggregation %s: lawful_basis=%s, no consent sought - "
                         "%d scope(s) across %s on the policy ceiling alone",
                         request.id, request.lawful_basis, len(granted), in_play)
            await self._mint_grants(request)
            await self.enqueue_fan_out(request.id)
            return request

        # The subject already consented; whether they must also prove possession
        # of a code before the data moves is the partner's policy, not a
        # property of the flow.
        request.otp_required = await self._otp_required_for(partner_id)
        if request.otp_required:
            await self.otp.issue(request, request.subject_id_value)
            request.status = AggregationStatus.pending_otp.value
        else:
            request.status = AggregationStatus.verified.value

        async with async_session()() as session:
            session.add(request)
            await session.commit()
            await session.refresh(request)

        if not request.otp_required:
            # Everything verify_otp would have done on a correct code. The grant
            # is not optional: no registry is called without one.
            _logger.info("Aggregation %s: no OTP required by policy, %d scope(s) "
                         "across %s - fanning out now",
                         request.id, len(granted), in_play)
            await self._mint_grants(request)
            await self.enqueue_fan_out(request.id)
            return request

        _logger.info("Aggregation %s: pending_otp, %d scope(s) across %s",
                     request.id, len(granted), in_play)
        return request

    @staticmethod
    def _granted_scopes(requestable: List[str], effective: Optional[List[str]]) -> List[str]:
        """The requested scope ids the CM's permit covers; never empty.

        The CM answers a consent object once: its first /validate records the
        permit, and every later /validate of the same object returns that
        record, whatever scopes are asked for. So a permit naming only scopes
        outside ``requestable`` (the CM otherwise answers consented ∩ policy ∩
        requested) means the object already carried a seek for other
        registries - say so, rather than claim the consent lacks the scopes.
        """
        permitted = set(effective or [])
        granted = [scope for scope in requestable if scope in permitted]
        if granted:
            return granted
        asked = sorted({scope.split(SCOPE_SEPARATOR, 1)[0] for scope in requestable})
        if permitted:
            used_for = sorted({scope.split(SCOPE_SEPARATOR, 1)[0] for scope in permitted})
            raise AggregationError(
                409, "consent_object_reused",
                "this consent object already carried a seek for %s; the Consent "
                "Manager answers a consent object once, so it cannot be used for %s. "
                "Sign a new consent object for this seek"
                % (", ".join(used_for), ", ".join(asked)))
        raise AggregationError(
            403, "no_scope_permitted",
            "the consent permits none of the scopes of %s" % ", ".join(asked))

    @staticmethod
    def _check_subject(query: Dict[str, Any], subject_value: Optional[str],
                       same=None) -> None:
        """The beneficiary asked about must be the one who consented.

        The registries are searched by ``foundationalId``, and on the hop the
        CM checks the aggregator's binding, not the subject. So this is the
        only place that stops a partner holding one person's consent from
        fetching another person's record. ``same`` compares two spellings of
        one ID (``RegistryCatalog.same_identifier``: "FAN-1234" is "1234");
        without it the values must be equal.
        """
        asked = query.get("foundationalId")
        same = same or (lambda a, b: bool(a) and a == b)
        if not subject_value or not same(asked, subject_value):
            raise AggregationError(
                403, "subject_mismatch",
                "foundationalId must be the consent object's subject_id.value")

    # ── 1b. raise the consent the subject was never asked for ───────────────

    async def _raise_consent_request(self, *, consent_jws: str, query: Dict[str, Any],
                                     requestable: List[str], callback_url: str,
                                     transaction_id: Optional[str],
                                     reference_id: Optional[str],
                                     purpose: Optional[Dict[str, Any]]
                                     ) -> AggregationRequest:
        """Park the aggregation and ask the subject, once, on the consent screen.

        Reading the claims unverified is safe *here specifically*: the subject
        grant is the last check ``validate`` performs, so a ``no_subject_consent``
        deny means the signature, the partner, the policy ceiling and the replay
        window have all already passed. Nothing is released on these claims —
        they only decide who to ask, and for what.

        The partner's object is kept on the row until the request is decided:
        on approval it is validated once more, which is how the granted scopes
        are learnt (see ``_release``).
        """
        claims = self.cm.decode_claims(consent_jws)
        subject = claims.get("subject_id") or {}
        audience = claims.get("aud")
        self._check_subject(query, subject.get("value"), get_catalog().same_identifier)

        # Ask for exactly what the partner's object names within the
        # registries in play; the subject decides how much of it to grant.
        wanted = set(claims.get("data_scopes") or [])
        scopes = [scope for scope in requestable if scope in wanted]
        if not scopes:
            raise AggregationError(
                403, "no_scope_requested",
                "the consent object names no scope of the requested registries")

        try:
            partner_id = await self.cm.partner_id_for(audience)
        except CMError as exc:
            raise AggregationError(exc.status, exc.reason, exc.detail) from exc
        if not partner_id:
            raise AggregationError(403, "unknown_partner",
                                   "no CM binding for audience '%s'" % audience)

        try:
            consent_request = await self.cm.create_consent_request(
                subject_id={"type": subject.get("type", ""),
                            "value": subject.get("value", "")},
                partner_id=partner_id,
                purpose=purpose or claims.get("purpose") or {
                    "code": _config.aggregator_purpose_code},
                requested_scopes=scopes,
            )
        except CMError as exc:
            raise AggregationError(exc.status, "consent_request_failed",
                                   exc.detail) from exc

        request = AggregationRequest(
            partner_id=partner_id,
            partner_audience=audience,
            subject_id_type=subject.get("type", ""),
            subject_id_value=subject.get("value", ""),
            requested_scopes=scopes,
            query=query,
            purpose=purpose or {},
            transaction_id=transaction_id,
            correlation_id=uuid.uuid4().hex,
            reference_id=reference_id,
            callback_url=callback_url,
            consent_request_id=consent_request["id"],
            consent_jws=consent_jws,
            status=AggregationStatus.pending_consent.value,
            registry_results={},
        )
        async with async_session()() as session:
            session.add(request)
            await session.commit()
            await session.refresh(request)

        _logger.info(
            "Aggregation %s: no consent for %s yet - raised consent request %s "
            "(%d scope(s)); waiting on the consent screen",
            request.id, request.subject_id_value, consent_request["id"], len(scopes))
        return request

    # ── 1c. read consent state back from the CM ─────────────────────────────
    #
    # The CM tells nobody when a consent request is decided or a consent is
    # withdrawn; both are read from its generic APIs. sync_with_cm is one pass
    # over everything that can change because of such a decision. It is run
    # by the poll loop (API process and/or worker, cm_poll_interval_sec) and
    # once per reaper run. Every step is idempotent and a parked row is claimed
    # before it is worked on, so any number of pollers may run at once.

    async def sync_with_cm(self) -> Dict[str, int]:
        """One pass: release / reject parked rows, cancel withdrawn ones."""
        resolved = await self._sync_parked()
        withdrawn = await self._sync_in_flight()
        return {"parked_resolved": resolved, "withdrawn": withdrawn}

    async def _claim_for_sync(self, request_id: str, status: str) -> bool:
        """Claim one row for this poller, unless another holds a live claim."""
        cutoff = datetime.now(timezone.utc) - timedelta(
            seconds=_config.cm_poll_claim_timeout_sec)
        async with async_session()() as session:
            result = await session.execute(
                update(AggregationRequest)
                .where(AggregationRequest.id == request_id,
                       AggregationRequest.status == status,
                       or_(AggregationRequest.claimed_at.is_(None),
                           AggregationRequest.claimed_at < cutoff))
                .values(claimed_by=WORKER_ID, claimed_at=datetime.now(timezone.utc)))
            await session.commit()
            return (result.rowcount or 0) > 0

    async def _unclaim(self, request_id: str) -> None:
        async with async_session()() as session:
            await session.execute(
                update(AggregationRequest)
                .where(AggregationRequest.id == request_id,
                       AggregationRequest.status == AggregationStatus.pending_consent.value,
                       AggregationRequest.claimed_by == WORKER_ID)
                .values(claimed_by=None, claimed_at=None))
            await session.commit()

    async def _sync_parked(self) -> int:
        """Rows waiting on a raised consent request: has the CM decided it?"""
        async with async_session()() as session:
            ids = list((await session.execute(
                select(AggregationRequest.id).where(
                    AggregationRequest.status == AggregationStatus.pending_consent.value)
            )).scalars().all())
        resolved = 0
        for request_id in ids:
            if not await self._claim_for_sync(
                    request_id, AggregationStatus.pending_consent.value):
                continue
            try:
                if await self._resolve_parked(request_id):
                    resolved += 1
            except CMError as exc:
                # Unreachable CM: the row stays parked and the next pass asks
                # again. Never guessed at.
                _logger.warning("Aggregation %s: consent request not checked - %s %s",
                                request_id, exc.reason, exc.detail)
            finally:
                await self._unclaim(request_id)
        return resolved

    async def _resolve_parked(self, request_id: str) -> bool:
        """Release or reject one parked row from its consent request's status.

        True when the row left ``pending_consent``.
        """
        async with async_session()() as session:
            row = await session.get(AggregationRequest, request_id)
        if row is None or row.status != AggregationStatus.pending_consent.value:
            return False

        consent_request = await self.cm.get_consent_request(row.consent_request_id)
        if consent_request is None:
            await self._reject(row.id, "consent_request_missing")
            return True
        status = consent_request.get("status")
        if status in ("denied", "expired"):
            await self._reject(row.id, "consent_%s" % status)
            _logger.info("Aggregation %s: consent request %s was %s",
                         row.id, row.consent_request_id, status)
            return True
        if status != "approved":
            return False
        await self._release(row, consent_request)
        return True

    async def _release(self, row, consent_request: Dict[str, Any]) -> None:
        """A consent request was approved — release the row it was raised for.

        What the subject granted is not on the consent request. It is learnt
        the way any partner learns it: by asking the CM's /validate again with
        the partner's own consent object, which the CM now narrows to the
        subject's grant (B8). That also yields the CM consent record whose
        status says, later, whether the subject has withdrawn.

        The CM refuses an object issued more than its replay window ago
        (300s by default). An approval that lands after that cannot be turned
        into a granted scope set from here, so the row is rejected and the
        partner re-seeks with a fresh object - which the CM then permits
        directly on the grant the subject just gave.
        """
        decision = await self.cm.validate(row.consent_jws or "",
                                          list(row.requested_scopes or []))
        if decision.get("decision") != "permit":
            reason = str(decision.get("reason_code") or "deny")
            if reason == "replay":
                reason = "consent_approved_after_replay_window"
            await self._reject(row.id, reason)
            _logger.info("Aggregation %s: approved, but the partner's object no "
                         "longer validates (%s); the partner must seek again",
                         row.id, reason)
            return

        granted = set(decision.get("effective_data_scopes") or [])
        kept = [s for s in (row.requested_scopes or []) if s in granted]
        if not kept:
            await self._reject(row.id, "no_scope_granted")
            _logger.info("Aggregation %s: subject granted none of the requested "
                         "scopes", row.id)
            return

        # Whatever the subject answered on the consent screen IS the
        # authentication behind this release. A screen that took an id_token
        # instead leaves otp_verified_at empty, and the grants are then
        # recorded as consent-backed rather than OTP-backed.
        verified_at = consent_request.get("otp_verified_at")
        async with async_session()() as session:
            request = await session.get(AggregationRequest, row.id)
            if request.status != AggregationStatus.pending_consent.value:
                return
            request.requested_scopes = kept
            request.cm_consent_id = decision.get("consent_id")
            request.lawful_basis = decision.get("lawful_basis") or "consent"
            request.otp_verified_at = (
                datetime.fromisoformat(verified_at) if verified_at else None)
            request.otp_required = bool(verified_at)
            request.otp_channel = consent_request.get("otp_channel")
            request.consent_jws = None   # spent; not kept past the decision
            request.status = AggregationStatus.verified.value
            request.claimed_by = None
            request.claimed_at = None
            await session.commit()
            await session.refresh(request)

        await self._mint_grants(request)
        await self.enqueue_fan_out(request.id)
        _logger.info("Aggregation %s: released by consent request %s (%d scope(s))",
                     request.id, request.consent_request_id, len(kept))

    async def _sync_in_flight(self) -> int:
        """Rows not yet delivered: is the consent they stand on still active?

        One CM call per distinct consent, however many rows share it.
        """
        async with async_session()() as session:
            rows = (await session.execute(
                select(AggregationRequest.id, AggregationRequest.cm_consent_id).where(
                    AggregationRequest.status.in_(_WITHDRAWABLE),
                    AggregationRequest.lawful_basis == "consent",
                    AggregationRequest.cm_consent_id.isnot(None))
            )).all()
        consent_ids = sorted({consent_id for _id, consent_id in rows})

        cancelled = 0
        for consent_id in consent_ids:
            try:
                status = await self.cm.consent_status(consent_id)
            except CMError as exc:
                _logger.warning("Consent %s not checked - %s %s",
                                consent_id, exc.reason, exc.detail)
                continue
            if status == "active":
                continue
            reason = ("consent_withdrawn" if status == "revoked"
                      else "consent_%s" % (status or "unknown"))
            cancelled += await self.cancel_for_withdrawal(consent_id, reason=reason)
        return cancelled

    async def consent_still_active(self, request) -> bool:
        """The check made right before the fan-out and before the callback.

        A row on a non-consent basis stands on no consent. A consent-basis row
        with no CM record to check is treated as withdrawn: failing open here
        would release data on nothing. Raises CMError when the CM cannot say.
        """
        if (request.lawful_basis or "consent") != "consent":
            return True
        if not request.cm_consent_id:
            return False
        return (await self.cm.consent_status(request.cm_consent_id)) == "active"

    async def _reject(self, request_id: str, reason: str) -> None:
        async with async_session()() as session:
            row = await session.get(AggregationRequest, request_id)
            if row is not None and row.status not in (
                    AggregationStatus.delivered.value, AggregationStatus.delivering.value):
                row.status = AggregationStatus.rejected.value
                row.failure_reason = reason
                row.consent_jws = None
                row.claimed_by = None
                row.claimed_at = None
                row.next_retry_at = None
                await session.commit()

    # ── 2. verify ───────────────────────────────────────────────────────────

    async def _verify_otp(self, request_id: str, code: str) -> AggregationRequest:
        """Check the code and, on success, start the fan-out in the background.

        Only ever reached through ``verify_for_subject``, which has already
        established that the caller is the subject. The caller gets an
        immediate answer; delivery happens on the callback.
        """
        async with async_session()() as session:
            request = await session.get(AggregationRequest, request_id)
            if request is None:
                raise AggregationError(404, "not_found", "no such aggregation request")
            if request.status not in (AggregationStatus.pending_otp.value,):
                if not request.otp_required:
                    raise AggregationError(
                        409, "otp_not_required",
                        "this partner's policy requires no one-time code; the "
                        "fetch proceeded on the subject's consent alone")
                raise AggregationError(409, "wrong_state",
                                       "request is '%s'" % request.status)
            try:
                await self.otp.verify(request, code)
            except OtpError as exc:
                # A terminal failure must close the request, or the attempt cap
                # could be reset by simply asking again.
                if exc.reason in ("otp_expired",) or self.otp.attempts_exhausted(request):
                    request.status = AggregationStatus.rejected.value
                    request.failure_reason = exc.reason
                await session.commit()
                raise AggregationError(exc.status, exc.reason, exc.detail) from exc

            request.status = AggregationStatus.verified.value
            await session.commit()
            await session.refresh(request)

        # The OTP the subject just entered IS their grant for this fetch. Mint
        # it before fanning out: no registry is called without one.
        await self._mint_grants(request)

        await self.enqueue_fan_out(request.id)
        return request

    async def _mint_grants(self, request) -> None:
        """Record the subject's authorisation as a grant per registry hop.

        The registries validate each hop with the CM, where the aggregator's
        bindings run on ``legitimate_interest``: the CM caps the hop at the
        binding's policy ceiling and looks for no subject grant. So the
        subject's side of the hop is held here, and no registry is called
        without an active grant (``_query_registries``).

        ``auth_method`` follows ``request.otp_required``: "otp" when a code was
        answered, "consent" when the partner's policy asked for none and the
        subject's standing consent is the whole of the authority, "none" under
        legitimate_interest. Recording "otp" in the second case would put an
        act in the audit trail that never happened.

        A live grant is reused rather than written again only when partner,
        subject, registry, scopes, purpose, method, lawful basis and the CM
        consent it stands on all match exactly, and at least
        ``aggregator_grant_reuse_min_remaining_sec`` of it is left.
        """
        basis = getattr(request, "lawful_basis", "consent") or "consent"
        method = ("otp" if request.otp_required
                  else "consent" if basis == "consent" else "none")
        purpose = request.purpose or {"code": _config.aggregator_purpose_code}
        catalog = get_catalog()
        by_registry = catalog.split_scope_ids(request.requested_scopes)
        now = datetime.now(timezone.utc)
        valid_until = now + timedelta(seconds=_config.aggregator_consent_validity_sec)
        reuse_floor = now + timedelta(
            seconds=_config.aggregator_grant_reuse_min_remaining_sec)

        grant_ids: Dict[str, str] = {}
        minted, reused = [], []
        async with async_session()() as session:
            for registry, scopes in sorted(by_registry.items()):
                entry = catalog.get(registry)
                candidates = (await session.execute(
                    select(AggregationGrant).where(
                        AggregationGrant.partner_id == request.partner_id,
                        AggregationGrant.subject_id_type == request.subject_id_type,
                        AggregationGrant.subject_id_value == request.subject_id_value,
                        AggregationGrant.registry == registry,
                        AggregationGrant.status == GrantStatus.active.value,
                        AggregationGrant.auth_method == method,
                        AggregationGrant.lawful_basis == basis,
                        AggregationGrant.valid_until >= reuse_floor)
                    .order_by(AggregationGrant.created_at.desc())
                )).scalars().all()
                match = next((g for g in candidates
                              if sorted(g.scopes or []) == scopes
                              and (g.purpose or {}) == purpose
                              and g.cm_consent_id == request.cm_consent_id), None)
                if match is not None:
                    grant_ids[registry] = match.id
                    reused.append(registry)
                    continue
                grant = AggregationGrant(
                    partner_id=request.partner_id,
                    partner_audience=request.partner_audience,
                    subject_id_type=request.subject_id_type,
                    subject_id_value=request.subject_id_value,
                    registry=registry,
                    registry_audience=entry.binding.audience,
                    scopes=scopes,
                    purpose=purpose,
                    auth_method=method,
                    auth_timestamp=_aware(request.otp_verified_at) or now,
                    otp_channel=request.otp_channel,
                    lawful_basis=basis,
                    cm_consent_id=request.cm_consent_id,
                    consent_request_id=request.consent_request_id,
                    aggregation_id=request.id,
                    valid_until=valid_until,
                    status=GrantStatus.active.value,
                )
                session.add(grant)
                grant_ids[registry] = grant.id
                minted.append(registry)

            row = await session.get(AggregationRequest, request.id)
            if row is not None:
                row.grant_ids = grant_ids
            await session.commit()
        request.grant_ids = grant_ids
        _logger.info("Aggregation %s: %s-backed grants minted for %s (valid %ss), "
                     "reused for %s", request.id, method, minted,
                     _config.aggregator_consent_validity_sec, reused)

    async def _active_grant(self, request, registry: str) -> Optional[AggregationGrant]:
        """The grant this fetch spends at ``registry``, if it is still good."""
        grant_id = (request.grant_ids or {}).get(registry)
        if not grant_id:
            return None
        async with async_session()() as session:
            grant = await session.get(AggregationGrant, grant_id)
            if grant is None or grant.status != GrantStatus.active.value:
                return None
            if _aware(grant.valid_until) <= datetime.now(timezone.utc):
                grant.status = GrantStatus.expired.value
                await session.commit()
                return None
            return grant

    async def cancel_for_withdrawal(self, consent_id: str,
                                    reason: str = "consent_withdrawn") -> int:
        """The CM no longer reports this consent as active: stop what stands on it.

        Every aggregation not yet delivered that stands on ``consent_id`` is
        rejected, and every grant minted from it is revoked, so a queued or
        retrying fan-out finds nothing to spend. ``pending_consent`` stands on
        no consent yet and ``delivering`` has already sent, so neither is
        touched. Each fan-out stage claims its row by status, so a rejected
        row is skipped by fetch, delivery and every retry.
        """
        now = datetime.now(timezone.utc)
        async with async_session()() as session:
            result = await session.execute(
                update(AggregationRequest)
                .where(AggregationRequest.cm_consent_id == consent_id,
                       AggregationRequest.status.in_(_WITHDRAWABLE))
                .values(status=AggregationStatus.rejected.value,
                        failure_reason=reason,
                        claimed_by=None, claimed_at=None, next_retry_at=None))
            revoked = await session.execute(
                update(AggregationGrant)
                .where(AggregationGrant.cm_consent_id == consent_id,
                       AggregationGrant.status == GrantStatus.active.value)
                .values(status=GrantStatus.revoked.value, revoked_at=now,
                        revoke_reason=reason))
            await session.commit()
        cancelled = result.rowcount or 0
        _logger.info("Consent %s is no longer active (%s): %d in-flight "
                     "aggregation(s) cancelled, %d grant(s) revoked",
                     consent_id, reason, cancelled, revoked.rowcount or 0)
        return cancelled

    async def verify_for_subject(self, *, aggregation_id: str, code: str,
                                 caller: Optional[Dict[str, str]],
                                 subject_id=None) -> AggregationRequest:
        """The subject's own release: find the request, check it is theirs, verify.

        The subject check is the point. Without it, anyone holding an
        aggregation id and a valid OTP could release a record belonging to
        someone else - and with a provider whose OTP is a constant, that is not
        hypothetical. So the caller's token is required and authoritative; a
        ``subject_id`` in the body is an extra assertion that must agree with
        it, never a substitute for it.
        """
        if not caller or not caller.get("subject_id_value"):
            raise AggregationError(401, "unauthenticated",
                                   "the caller must be an authenticated subject")
        request = await self.get(aggregation_id)
        if request is None:
            raise AggregationError(404, "not_found", "no such aggregation request")

        claims = [(caller.get("subject_id_type") or request.subject_id_type,
                   caller["subject_id_value"], "token")]
        if subject_id is not None and getattr(subject_id, "value", None):
            claims.append((getattr(subject_id, "type", None) or request.subject_id_type,
                           subject_id.value, "body"))
        for wanted_type, wanted_value, where in claims:
            if (wanted_value != request.subject_id_value
                    or wanted_type != request.subject_id_type):
                _logger.warning("Aggregation %s: %s subject mismatch", request.id, where)
                # Deliberately the same 404 as an unknown id: confirming that
                # an id exists but belongs to someone else is a disclosure.
                raise AggregationError(404, "not_found", "no such aggregation request")

        return await self._verify_otp(request.id, code)

    # ── 3. fan out and deliver ──────────────────────────────────────────────
    #
    # Two stages, deliberately separated, because they fail for unrelated
    # reasons and at unrelated speeds.
    #
    #   fetch    queries every registry the partner named. Slow, expensive,
    #            and load the registries actually feel. Its result must not be
    #            thrown away because of something that happens afterwards.
    #   deliver  POSTs the finished envelope to the partner's callback. Fails
    #            when the *partner* is down, which has nothing to do with the
    #            registries and must never cause them to be queried again.
    #
    # With Kafka each stage is a topic and the stages run in different workers.
    # Without it both run in one in-process task, but through the same methods
    # and the same statuses, so behaviour is identical bar the queueing.

    async def enqueue_fan_out(self, request_id: str) -> None:
        """Hand the aggregation to a fan-out worker.

        Called from the request that verified the OTP, so it must be quick and
        must not raise: the subject's code is already spent. If the broker will
        not take it, the work falls back to an in-process task — slower and
        unbounded, but the authorisation is not lost.
        """
        if bus.enabled:
            claimed = await self._set_status(
                request_id,
                expect=(AggregationStatus.verified.value,),
                to=AggregationStatus.queued.value)
            if not claimed:
                _logger.info("Aggregation %s: already queued or past it; not "
                             "publishing again", request_id)
                return
            event = envelope(TYPE_FANOUT_REQUESTED, request_id,
                             {"aggregation_id": request_id})
            if await bus.publish(_config.topic_fanout, request_id, event):
                return
            # The broker took the row out of 'verified' but not the message.
            # Put it back so the in-process path can claim it normally, and so
            # the reaper is not left a row that nothing owns.
            await self._set_status(
                request_id,
                expect=(AggregationStatus.queued.value,),
                to=AggregationStatus.verified.value)
            _logger.warning("Aggregation %s: Kafka unavailable, running the "
                            "fan-out in-process", request_id)
        asyncio.create_task(self._fan_out_and_deliver(request_id))

    async def _fan_out_and_deliver(self, request_id: str) -> None:
        """The whole job in one task — the no-broker path, and the fallback."""
        try:
            prepared = await self.fetch_stage(request_id)
            if prepared is None:
                return
            delivered = await self.deliver_stage(
                request_id, prepared["callback_url"], prepared["body"], attempt=1)
            if not delivered:
                # deliver_stage leaves a failed attempt at 'fetched', meaning
                # "a retry is owed". Under Kafka the retry topic owes it. Here
                # there is no queue and nobody will come back for it, so the
                # row has to be closed — otherwise it sits at 'fetched'
                # forever, which reads as still in progress. This is the same
                # single-attempt outcome the service had before the queues.
                await self.give_up(
                    request_id,
                    "callback_failed (no broker: deliveries are not retried)")
        except Exception:  # noqa: BLE001 - a background task must never vanish silently
            _logger.exception("Aggregation %s: fan-out failed", request_id)
            await self._mark_failed(request_id, "internal_error")

    # ── claiming ────────────────────────────────────────────────────────────

    async def _set_status(self, request_id: str, *, expect: tuple, to: str,
                          claim: bool = False, count_fetch: bool = False) -> bool:
        """Conditional status change. False means somebody else got there first.

        This is the whole of the idempotency story. Kafka delivers at least
        once, so the same fan-out message can legitimately arrive twice — on a
        rebalance, or after a worker died between doing the work and committing
        its offset. Re-querying three registries and POSTing the subject's data
        to the partner a second time is not an acceptable response to that, so
        every stage begins by trying to move the row out of the status it
        expects. Exactly one caller can win, and the losers drop their copy.
        """
        async with async_session()() as session:
            sets = {"status": to}
            if claim:
                sets["claimed_by"] = WORKER_ID
                sets["claimed_at"] = datetime.now(timezone.utc)
            stmt = (
                update(AggregationRequest)
                .where(AggregationRequest.id == request_id,
                       AggregationRequest.status.in_(expect))
                .values(**sets)
            )
            if count_fetch:
                stmt = stmt.values(
                    fetch_attempts=AggregationRequest.fetch_attempts + 1)
            result = await session.execute(stmt)
            await session.commit()
            return (result.rowcount or 0) > 0

    # ── stage 1: the registries ─────────────────────────────────────────────

    async def fetch_stage(self, request_id: str) -> Optional[Dict[str, Any]]:
        """Query every registry and build the signed envelope. Does not deliver.

        Returns the prepared callback, or None when there was nothing to do —
        either the row is gone or another worker holds it.
        """
        claimed = await self._set_status(
            request_id,
            expect=(AggregationStatus.queued.value,
                    AggregationStatus.verified.value),
            to=AggregationStatus.fetching.value,
            claim=True, count_fetch=True)
        if not claimed:
            _logger.info("Aggregation %s: fetch skipped, not claimable "
                         "(already running, done, or cancelled)", request_id)
            return None

        async with async_session()() as session:
            request = await session.get(AggregationRequest, request_id)
        if request is None:
            return None

        # Right before any registry is touched: is the consent this fetch
        # stands on still active in the CM? The poll may not have run since
        # the subject withdrew. An unreachable CM fails closed.
        try:
            still_active = await self.consent_still_active(request)
        except CMError as exc:
            _logger.warning("Aggregation %s: consent not checkable before the "
                            "fan-out - %s %s", request_id, exc.reason, exc.detail)
            await self._mark_failed(request_id, "consent_check_failed")
            return None
        if not still_active:
            if request.cm_consent_id:
                await self.cancel_for_withdrawal(request.cm_consent_id)
            else:
                await self._reject(request_id, "no_consent_reference")
            _logger.info("Aggregation %s: consent no longer active - nothing "
                         "fetched", request_id)
            return None

        outcomes = await self._query_registries(request)
        body = self._build_envelope(request, outcomes)
        results = self._summarise(outcomes)

        # Conditional on still being 'fetching': the subject may have withdrawn
        # while the registries were answering, which rejects the row. An
        # unconditional write here would resurrect it as 'fetched' and deliver
        # data the subject had already taken back.
        async with async_session()() as session:
            result = await session.execute(
                update(AggregationRequest)
                .where(AggregationRequest.id == request_id,
                       AggregationRequest.status == AggregationStatus.fetching.value)
                .values(registry_results=results,
                        status=AggregationStatus.fetched.value,
                        claimed_by=None, claimed_at=None)
            )
            await session.commit()
        if not result.rowcount:
            _logger.info("Aggregation %s: fetched, but no longer 'fetching' "
                         "(consent withdrawn?) - nothing will be delivered",
                         request_id)
            return None

        return {"callback_url": request.callback_url, "body": body,
                "results": results}

    @staticmethod
    def _query_of(request) -> Dict[str, Any]:
        """The row's bene-360 request. A row written before the query was
        stored has none; it is answered for its subject over the short window."""
        query = dict(request.query or {})
        query.setdefault("foundationalId", request.subject_id_value)
        query.setdefault("timeframe", "Timeframe-Short")
        return query

    async def _query_registries(self, request) -> Dict[str, Dict[str, Any]]:
        """One call per registry, the outcome recorded per registry.

        A partner asking across three registries should not lose two because
        one is down, so a RegistryError becomes that registry's outcome and
        the loop continues. A registry the query covers but the consent does
        not is never called; it is reported as such in the response.
        """
        catalog = get_catalog()
        query = self._query_of(request)
        granted = catalog.split_scope_ids(request.requested_scopes)
        foundational_id = query["foundationalId"]
        outcomes: Dict[str, Dict[str, Any]] = {}

        for registry in bene360.planned_registries(catalog, query):
            scopes = granted.get(registry)
            if not scopes:
                outcomes[registry] = {"status": "not_consented"}
                continue
            entry = catalog.get(registry)
            # The subject's side of the hop. The CM caps the hop at the
            # binding's ceiling and asks for no grant (legitimate_interest),
            # so this is the only place the subject's authorisation is spent.
            if await self._active_grant(request, registry) is None:
                outcomes[registry] = {"status": "no_active_grant"}
                continue
            try:
                records = await self.registries.search(
                    registry, entry, request.subject_id_type, request.subject_id_value,
                    entry.search_value(foundational_id), entry.hop_scopes(scopes),
                    request.purpose)
            except RegistryError as exc:
                _logger.warning("Aggregation %s: %s failed - %s %s",
                                request.id, registry, exc.reason, exc.detail)
                outcomes[registry] = {"status": "error", "reason": exc.reason,
                                      "detail": exc.detail}
                continue

            # The registry's search is a substring match, so a record that
            # only mentions the ID (a phone number, another person's ID) can
            # come back. Only an exact identifier match is this beneficiary.
            matched = [r for r in records if entry.identifies(r, foundational_id)]
            if len(matched) < len(records):
                _logger.warning("Aggregation %s: %s returned %d record(s) not identified "
                                "by the foundational ID; discarded", request.id,
                                registry, len(records) - len(matched))

            # The registry clamped to whole blocks; this is where each block
            # is cut down to the catalog's allowed fields and placed on its
            # register or table. Only granted scopes are read, so the block
            # fetched for the identifier check alone never leaves.
            outcomes[registry] = {
                "status": "ok", "records": len(matched), "scopes": scopes,
                "discarded": len(records) - len(matched),
                "membership": bene360.map_registry(
                    registry, entry, matched, scopes, foundational_id)}
        return outcomes

    @staticmethod
    def _summarise(outcomes: Dict[str, Dict[str, Any]]) -> Dict[str, Any]:
        """What each registry did, for the row's audit trail - never the data."""
        summary: Dict[str, Any] = {}
        for registry, outcome in outcomes.items():
            row = {k: v for k, v in outcome.items() if k != "membership"}
            if outcome.get("status") == "ok":
                row["matched"] = bool(outcome.get("membership"))
            summary[registry] = row
        return summary

    # ── stage 2: the partner's callback ─────────────────────────────────────

    async def deliver_stage(self, request_id: str, callback_url: str,
                            body: Dict[str, Any], *, attempt: int) -> bool:
        """POST the envelope once and record what happened.

        Returns True when the partner accepted it. False means the caller
        should schedule a retry — the *caller* decides that, because in Kafka
        mode the wait is held on a topic and in-process there is no wait at all.
        """
        claimed = await self._set_status(
            request_id,
            expect=(AggregationStatus.fetched.value,),
            to=AggregationStatus.delivering.value,
            claim=True)
        if not claimed:
            _logger.info("Aggregation %s: delivery skipped, not claimable",
                         request_id)
            return True  # somebody else owns it; this copy must not retry

        # Last chance to honour a withdrawal: the envelope is built but not
        # sent. A withdrawn consent closes the row (no retry); a CM that
        # cannot answer puts it back to 'fetched' so the normal callback retry
        # asks again - the data is never released on an unanswered check.
        async with async_session()() as session:
            row = await session.get(AggregationRequest, request_id)
        try:
            still_active = await self.consent_still_active(row)
        except CMError as exc:
            _logger.warning("Aggregation %s: consent not checkable before the "
                            "callback - %s %s", request_id, exc.reason, exc.detail)
            await self._set_status(request_id,
                                   expect=(AggregationStatus.delivering.value,),
                                   to=AggregationStatus.fetched.value)
            return False
        if not still_active:
            # 'delivering' is outside what a withdrawal cancels, so close the
            # row here explicitly before revoking what else stands on it.
            await self._set_status(request_id,
                                   expect=(AggregationStatus.delivering.value,),
                                   to=AggregationStatus.fetched.value)
            if row.cm_consent_id:
                await self.cancel_for_withdrawal(row.cm_consent_id)
            else:
                await self._reject(request_id, "no_consent_reference")
            _logger.info("Aggregation %s: consent no longer active - the "
                         "callback was not sent", request_id)
            return True

        status_code = await self._post_callback(request_id, callback_url, body)
        accepted = bool(status_code and 200 <= status_code < 300)

        async with async_session()() as session:
            row = await session.get(AggregationRequest, request_id)
            row.callback_attempts = (row.callback_attempts or 0) + 1
            row.callback_status = status_code
            row.claimed_by = None
            row.claimed_at = None
            if accepted:
                row.status = AggregationStatus.delivered.value
                row.delivered_at = datetime.now(timezone.utc)
                row.next_retry_at = None
                row.failure_reason = None
            else:
                # Back to 'fetched' rather than 'failed': the envelope is still
                # good and a retry is owed. Only give_up() writes 'failed'.
                row.status = AggregationStatus.fetched.value
                row.failure_reason = "callback_failed"
            await session.commit()
        if not accepted:
            _logger.warning("Aggregation %s: callback attempt %d returned %s",
                            request_id, attempt, status_code)
        return accepted

    async def schedule_retry(self, request_id: str, when) -> None:
        """Record when the next callback attempt is due, for the status API."""
        async with async_session()() as session:
            row = await session.get(AggregationRequest, request_id)
            if row is not None:
                row.next_retry_at = when
                await session.commit()

    async def give_up(self, request_id: str, reason: str) -> None:
        """Terminal: the callback will not be attempted again."""
        async with async_session()() as session:
            row = await session.get(AggregationRequest, request_id)
            # A row already closed (withdrawn, or delivered by a racing copy)
            # keeps the reason it was closed for.
            if row is not None and row.status not in (
                    AggregationStatus.rejected.value, AggregationStatus.delivered.value):
                row.status = AggregationStatus.failed.value
                row.failure_reason = reason
                row.next_retry_at = None
                row.claimed_by = None
                row.claimed_at = None
                await session.commit()
        _logger.error("Aggregation %s: giving up - %s", request_id, reason)

    # ── the envelope ────────────────────────────────────────────────────────

    def _build_envelope(self, request, outcomes: Dict[str, Dict[str, Any]]) -> Dict[str, Any]:
        """The standard DCI on-search body carrying the bene-360 response, signed.

        Built once and then carried on the delivery topic, so a retry re-sends
        the identical bytes. That matters twice over: the JWS stays valid
        (re-signing would change the signature for the same facts), and
        ``message_id`` is stable, which is what lets a partner recognise a
        redelivery of something it has already seen rather than treating it as
        a second result.
        """
        now = datetime.now(timezone.utc)
        any_ok = any(o.get("status") == "ok" for o in outcomes.values())
        record = bene360.build_response(
            catalog=get_catalog(), query=self._query_of(request), outcomes=outcomes,
            response_id="urn:openg2p:aggregation:%s" % request.id, generated_at=now)

        header = {
            "version": "1.0.0",
            # Derived from the correlation id rather than random, so the id is
            # the same on every attempt at this delivery.
            "message_id": uuid.uuid5(_MESSAGE_NS, request.correlation_id).hex,
            "message_ts": now.isoformat(),
            "action": "on-search",
            "status": "succ" if any_ok else "rjct",
            "total_count": 1,
            "completed_count": 1 if any_ok else 0,
            "sender_id": _config.aggregator_sender_id,
            "receiver_id": request.partner_audience or request.partner_id,
            "is_msg_encrypted": False,
            # This service's own facts about the release. They stay out of the
            # bene-360 response, whose schema admits no extra properties.
            "meta": {
                "consent_enforcement": (
                    "enabled" if request.lawful_basis == "consent" else "not_applicable"),
                # Why the data moved, and what the subject actually did. Telling
                # a partner "otp" when the policy asked for none overstates the
                # assurance behind the record; saying "consent" when no subject
                # was ever asked misstates the basis for holding it at all.
                "lawful_basis": request.lawful_basis,
                "subject_authentication": (
                    "otp" if request.otp_required
                    else "consent" if request.lawful_basis == "consent" else "none"),
            },
        }
        if not any_ok:
            header["status_reason_code"] = "AGG-VAL-001"
            header["status_reason_message"] = (
                "no registry could be queried; see meta.warnings in the record")

        message = {
            "transaction_id": request.transaction_id or request.correlation_id,
            "correlation_id": request.correlation_id,
            "search_response": [{
                "reference_id": request.reference_id or request.correlation_id,
                "timestamp": now.isoformat(),
                "status": "succ" if any_ok else "rjct",
                "data": {
                    "version": "1.0.0",
                    "reg_type": bene360.DCI_REG_TYPE,
                    "reg_record_type": bene360.DCI_REG_RECORD_TYPE,
                    # One record: the bene-360 response. It carries its own
                    # warnings, so even a rejected search explains itself.
                    "reg_records": [record],
                },
                "pagination": {"page_size": 1, "page_number": 1, "total_count": 1},
                "locale": "en",
            }],
        }
        _jws, signature = self.registries._jws({"header": header, "message": message})
        return {"signature": signature, "header": header, "message": message}

    async def _post_callback(self, request_id: str, callback_url: str,
                             body: Dict[str, Any]) -> Optional[int]:
        """One HTTP POST. None means it did not complete at all."""
        try:
            async with httpx.AsyncClient(timeout=_config.aggregator_callback_timeout) as client:
                response = await client.post(callback_url, json=body)
            _logger.info("Aggregation %s: callback %s -> HTTP %s",
                         request_id, callback_url, response.status_code)
            return response.status_code
        except Exception as exc:  # noqa: BLE001
            _logger.warning("Aggregation %s: callback to %s failed: %s",
                            request_id, callback_url, exc)
            return None

    async def _mark_failed(self, request_id: str, reason: str) -> None:
        async with async_session()() as session:
            row = await session.get(AggregationRequest, request_id)
            if row is not None and row.status not in (
                    AggregationStatus.rejected.value, AggregationStatus.delivered.value):
                row.status = AggregationStatus.failed.value
                row.failure_reason = reason
                row.claimed_by = None
                row.claimed_at = None
                await session.commit()

    # ── recovery ────────────────────────────────────────────────────────────

    async def reap_stale_claims(self) -> List[str]:
        """Release rows whose worker died mid-stage, and say which they were.

        A claim is a status, not a lease, so nothing expires on its own. If a
        worker is killed between claiming a row and finishing it, two things
        are true at once: the row sits in 'fetching' or 'delivering' forever,
        and its Kafka message — redelivered to another worker on the rebalance
        — is refused as unclaimable. Neither side recovers without this.

        Both stages go back to 'queued', not to the stage they were in. A
        half-delivered row cannot be resumed, because the signed envelope lived
        in the Kafka message rather than in the database: rebuilding it means
        querying the registries again. That is real load and a second read of
        the subject's data, which is exactly why it is reserved for a worker
        that has actually died rather than one that is merely slow — hence the
        ``kafka_claim_timeout_sec`` floor.

        The caller republishes the returned ids; see
        ``python -m openg2p_aggregation_layer.reap``.
        """
        now = datetime.now(timezone.utc)
        cutoff = now - timedelta(seconds=_config.kafka_claim_timeout_sec)
        stale = (AggregationStatus.fetching.value,
                 AggregationStatus.delivering.value)
        async with async_session()() as session:
            rows = list((await session.execute(
                select(AggregationRequest.id)
                .where(AggregationRequest.status.in_(stale),
                       AggregationRequest.claimed_at < cutoff))).scalars().all())

            # A second kind of abandonment, with a different cause. A row at
            # 'fetched' with a next_retry_at in the past is a delivery that is
            # owed but whose message is gone — the retry topic aged it out, a
            # tier was renamed, or the publish that should have queued it
            # failed. Nothing holds it and nothing will come back for it, so it
            # sits there looking like it is waiting. The grace period is
            # generous because a tier that is merely busy is not this.
            overdue = list((await session.execute(
                select(AggregationRequest.id)
                .where(AggregationRequest.status == AggregationStatus.fetched.value,
                       AggregationRequest.next_retry_at.isnot(None),
                       AggregationRequest.next_retry_at < cutoff))).scalars().all())
            if overdue:
                _logger.warning(
                    "%d delivery(ies) were owed a retry that never arrived: %s",
                    len(overdue), ", ".join(overdue))
                rows += overdue

            if not rows:
                return []
            await session.execute(
                update(AggregationRequest)
                .where(AggregationRequest.id.in_(rows))
                .values(status=AggregationStatus.queued.value,
                        claimed_by=None, claimed_at=None,
                        # The old schedule is void — this row is going back
                        # through the fan-out, and leaving a stale timestamp
                        # would keep it counted as awaiting a retry forever.
                        next_retry_at=None))
            await session.commit()
        _logger.warning("Released %d aggregation(s) from a dead worker's claim: %s",
                        len(rows), ", ".join(rows))
        return list(rows)

    async def republish(self, request_id: str) -> None:
        """Put a released row back on the fan-out topic.

        The row is already 'queued', so ``enqueue_fan_out`` would refuse it —
        its whole job is to move a row *out* of 'verified'. This is the one
        place that publishes for a row that is already queued.
        """
        event = envelope(TYPE_FANOUT_REQUESTED, request_id,
                         {"aggregation_id": request_id, "requeued": True})
        if not await bus.publish(_config.topic_fanout, request_id, event):
            _logger.error("Aggregation %s: could not requeue after reaping; it "
                          "stays 'queued' until the next sweep", request_id)

    # ── status ──────────────────────────────────────────────────────────────

    async def queue_status(self) -> Dict[str, Any]:
        """What the queue is doing right now, without a Kafka client.

        Exists because "is the queue working?" was otherwise only answerable by
        reading the service log or shelling into the broker, neither of which a
        partner integrator or a Postman collection can do. Everything here
        comes from the aggregation rows, so it answers identically with the
        broker on or off — which is the point: it says which mode is serving
        requests rather than assuming one.
        """
        counts: Dict[str, int] = {}
        async with async_session()() as session:
            rows = await session.execute(
                select(AggregationRequest.status,
                       func.count(AggregationRequest.id))
                .group_by(AggregationRequest.status))
            for status, count in rows.all():
                counts[status] = int(count)

            # Work that has been accepted but has not reached the partner. This
            # is the number to watch during a burst: it should rise and fall,
            # not sit still.
            in_flight = sum(counts.get(s, 0) for s in (
                AggregationStatus.queued.value,
                AggregationStatus.fetching.value,
                AggregationStatus.fetched.value,
                AggregationStatus.delivering.value))

            stuck = (await session.execute(
                select(func.count(AggregationRequest.id))
                .where(AggregationRequest.status.in_(
                    (AggregationStatus.fetching.value,
                     AggregationStatus.delivering.value)),
                       AggregationRequest.claimed_at <
                       datetime.now(timezone.utc) - timedelta(
                           seconds=_config.kafka_claim_timeout_sec)))).scalar() or 0

            retrying = (await session.execute(
                select(func.count(AggregationRequest.id))
                .where(AggregationRequest.next_retry_at.isnot(None),
                       AggregationRequest.status ==
                       AggregationStatus.fetched.value))).scalar() or 0

            requeried = (await session.execute(
                select(func.count(AggregationRequest.id))
                .where(AggregationRequest.fetch_attempts > 1))).scalar() or 0

        return {
            "mode": "kafka" if _config.kafka_enabled else "in-process",
            "kafka": {
                "enabled": _config.kafka_enabled,
                "bootstrap_servers": (_config.kafka_bootstrap_servers
                                      if _config.kafka_enabled else None),
                "consumers_in_app": (_config.kafka_consumers_in_app
                                     if _config.kafka_enabled else None),
                "connected": bool(_config.kafka_enabled and bus.connected),
                "topics": {
                    "fanout": _config.topic_fanout,
                    "delivery": _config.topic_delivery,
                    # One per backoff step. Listed individually because
                    # "the retry topic" no longer exists as a thing to look at.
                    "retry": [t["topic"] for t in _config.retry_tiers],
                    "dlq": _config.topic_dlq,
                } if _config.kafka_enabled else {},
                "fanout_concurrency": _config.kafka_fanout_concurrency,
                "delivery_max_attempts": _config.kafka_delivery_max_attempts,
                "retry_backoff_seconds": _config.retry_backoff,
            },
            "aggregations": counts,
            "in_flight": in_flight,
            "awaiting_callback_retry": int(retrying),
            # Above zero means a worker died mid-stage and the rows need
            # `python -m openg2p_aggregation_layer.reap`.
            "stale_claims": int(stuck),
            # Above zero means some registry was queried more than once for the
            # same request — always worth an explanation.
            "refetched": int(requeried),
        }

    async def get(self, request_id: str) -> Optional[AggregationRequest]:
        async with async_session()() as session:
            return await session.get(AggregationRequest, request_id)
