"""Orchestration for the async, OTP-gated, cross-registry fetch.

The partner makes ONE call naming fields from any number of registries. It gets
an ack straight back; the data arrives later at its callback, and only after the
subject has entered an OTP.

Where each check happens, and why:

``seek``
    The partner's consent object is validated by the Consent Manager's
    ``/consent/v1/validate`` — the same PDP the registries call.
    That buys signature verification against Partner Management, the policy
    ceiling, the replay window and the B8 subject-grant narrowing for free, and
    means the aggregator has not invented a second, weaker way to be trusted.

    The aggregator's CM binding carries **field aliases** as its data scopes
    (``farmer.firstname`` …) rather than registry block names. CM treats scopes
    as opaque strings, so this needs no CM change and makes
    ``effective_data_scopes`` mean exactly "the fields this partner may ask
    for". Requested fields are intersected with it.

``verify_otp``
    The subject's real-time authorisation. Consent says the partner *may* hold
    this data; the OTP says the subject agrees to release it *now*.

``fan-out``
    One call per registry, to its unchanged ``/dci/registry/sync/search``, with
    consent enforcement still on. Registry failures are recorded per registry
    rather than failing the whole request — a partner asking across three
    registries should not lose two because one is down.

``deliver``
    The aggregated record is signed and POSTed to the callback in the standard
    DCI ``on-search`` envelope, so the partner parses it with whatever already
    handles a sync response. Nothing about the envelope shape is new.
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
from sqlalchemy import func, select, update

from ..config import Settings
from ..db import async_session
from ..models import AggregationRequest, AggregationStatus
from ..kafka_bus import TYPE_FANOUT_REQUESTED, bus, envelope
from . import field_catalog
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

    async def seek(self, *, consent_jws: str, fields: List[str], callback_url: str,
                   query_value: str, transaction_id: Optional[str],
                   reference_id: Optional[str],
                   purpose: Optional[Dict[str, Any]],
                   registry_queries: Optional[Dict[str, str]] = None) -> AggregationRequest:
        """Validate, record, issue the OTP. Returns the row the ack is built from.

        Deliberately does NOT fetch anything: the whole point of the flow is
        that no registry is touched until the subject has answered.
        """
        if not fields:
            raise AggregationError(400, "no_fields", "requested_fields is empty")
        if not callback_url:
            raise AggregationError(
                400, "no_callback",
                "header.sender_uri must carry the callback URL for on-search")
        if not callback_url.lower().startswith(("http://", "https://")):
            raise AggregationError(400, "bad_callback", "sender_uri must be an http(s) URL")

        # Reject unknown aliases loudly and all at once. Dropping them silently
        # would be indistinguishable from "the farmer has no such data".
        try:
            by_registry = field_catalog.resolve(fields)
        except field_catalog.UnknownFieldError as exc:
            raise AggregationError(400, "unknown_field", str(exc)) from exc

        # The CM's PDP decides whether this partner may hold these fields.
        try:
            decision = await self.cm.validate(consent_jws, fields)
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
                    consent_jws=consent_jws, fields=fields, callback_url=callback_url,
                    query_value=query_value, transaction_id=transaction_id,
                    reference_id=reference_id, purpose=purpose,
                    registry_queries=registry_queries, by_registry=by_registry,
                )
            raise AggregationError(
                403, reason,
                decision.get("detail") or "consent did not permit this request")

        permitted = set(decision.get("effective_data_scopes") or [])
        granted_fields = [f for f in fields if f in permitted]
        if not granted_fields:
            raise AggregationError(
                403, "no_field_permitted",
                "none of the requested fields are within the consent")

        # Re-resolve against what was actually permitted, so a partially
        # permitted request only ever fans out to the registries it still needs.
        by_registry = field_catalog.resolve(granted_fields)

        # Who the partner is comes from the CM's decision (CM API #1), never
        # from anything in the partner's own body.
        partner_id = decision.get("partner_id") or ""
        partner_audience = decision.get("partner_audience")

        subject = decision.get("subject_id") or {}
        request = AggregationRequest(
            partner_id=partner_id,
            partner_audience=partner_audience,
            subject_id_type=subject.get("type", ""),
            subject_id_value=subject.get("value", ""),
            requested_fields=granted_fields,
            purpose=purpose or {},
            transaction_id=transaction_id,
            correlation_id=uuid.uuid4().hex,
            reference_id=reference_id,
            callback_url=callback_url,
            status=AggregationStatus.received.value,
            lawful_basis=decision.get("lawful_basis") or "consent",
            registry_results={"query_value": query_value,
                              "registry_queries": registry_queries or {},
                              "registries": sorted(by_registry),
                              # The CM record this permit was minted as. Sent
                              # back with the grants so My consents can group
                              # them under the consent they came from.
                              "validated_consent_id": decision.get("consent_id")},
        )

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
                         "%d field(s) across %s on the policy ceiling alone",
                         request.id, request.lawful_basis,
                         len(granted_fields), sorted(by_registry))
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
            # is not optional: without it the aggregator holds a policy ceiling
            # and no subject grant, and B8 denies every registry.
            _logger.info("Aggregation %s: no OTP required by policy, %d field(s) "
                         "across %s - fanning out now",
                         request.id, len(granted_fields), sorted(by_registry))
            await self._mint_grants(request)
            await self.enqueue_fan_out(request.id)
            return request

        _logger.info("Aggregation %s: pending_otp, %d field(s) across %s",
                     request.id, len(granted_fields), sorted(by_registry))
        return request

    # ── 1b. raise the consent the subject was never asked for ───────────────

    async def _raise_consent_request(self, *, consent_jws: str, fields: List[str],
                                     callback_url: str, query_value: str,
                                     transaction_id: Optional[str],
                                     reference_id: Optional[str],
                                     purpose: Optional[Dict[str, Any]],
                                     registry_queries: Optional[Dict[str, str]],
                                     by_registry) -> AggregationRequest:
        """Park the aggregation and ask the subject, once, on the consent screen.

        Reading the claims unverified is safe *here specifically*: the subject
        grant is the last check ``validate`` performs, so a ``no_subject_consent``
        deny means the signature, the partner, the policy ceiling and the replay
        window have all already passed. Nothing is released on these claims —
        they only decide who to ask.
        """
        claims = self.cm.decode_claims(consent_jws)
        subject = claims.get("subject_id") or {}
        audience = claims.get("aud")

        try:
            partner = await self.cm.partner_by_audience(audience)
        except CMError as exc:
            raise AggregationError(exc.status, exc.reason, exc.detail) from exc
        if partner is None:
            raise AggregationError(403, "unknown_partner",
                                   "no CM binding for audience '%s'" % audience)
        partner_id = partner["id"]
        partner_audience = partner.get("audience") or audience

        # Ask for exactly what the partner asked for; the subject decides how
        # much of it to grant on the screen.
        try:
            consent_request = await self.cm.create_consent_request(
                subject_id={"type": subject.get("type", ""),
                            "value": subject.get("value", "")},
                partner_id=partner_id,
                purpose=purpose or claims.get("purpose") or {
                    "code": _config.aggregator_purpose_code},
                requested_scopes=fields,
            )
        except CMError as exc:
            raise AggregationError(exc.status, "consent_request_failed",
                                   exc.detail) from exc

        request = AggregationRequest(
            partner_id=partner_id,
            partner_audience=partner_audience,
            subject_id_type=subject.get("type", ""),
            subject_id_value=subject.get("value", ""),
            requested_fields=fields,
            purpose=purpose or {},
            transaction_id=transaction_id,
            correlation_id=uuid.uuid4().hex,
            reference_id=reference_id,
            callback_url=callback_url,
            consent_request_id=consent_request["id"],
            status=AggregationStatus.pending_consent.value,
            registry_results={"query_value": query_value,
                              "registry_queries": registry_queries or {},
                              "registries": sorted(by_registry)},
        )
        async with async_session()() as session:
            session.add(request)
            await session.commit()
            await session.refresh(request)

        _logger.info(
            "Aggregation %s: no consent for %s yet - raised consent request %s "
            "(%d field(s)); waiting on the consent screen",
            request.id, request.subject_id_value, consent_request["id"], len(fields))
        return request

    async def release_for_consent_request(self, consent_request_id: str) -> None:
        """A consent request was just approved — release whatever was waiting.

        Called on the CM's ``consent_request.approved`` event (CM API #5).
        Silent when nothing is waiting, which is the normal case for a consent
        raised by hand. Safe to receive twice: only ``pending_consent`` rows
        are picked up, and the first delivery moves them out of it.
        """
        async with async_session()() as session:
            rows = await session.execute(
                select(AggregationRequest).where(
                    AggregationRequest.consent_request_id == consent_request_id,
                    AggregationRequest.status == AggregationStatus.pending_consent.value,
                )
            )
            waiting = rows.scalars().all()
        if not waiting:
            return

        # What the subject actually granted is the ceiling now, exactly as it
        # would have been had the consent existed when the partner called
        # (CM API #4). If the CM cannot be reached this raises and the rows
        # stay parked; the event is retried rather than guessed at.
        consent_request = await self.cm.granted_scopes(consent_request_id) or {}
        granted = set(consent_request.get("granted_scopes") or [])

        for row in waiting:
            kept = [f for f in (row.requested_fields or []) if f in granted]
            async with async_session()() as session:
                request = await session.get(AggregationRequest, row.id)
                if not kept:
                    request.status = AggregationStatus.rejected.value
                    request.failure_reason = "no_field_granted"
                    await session.commit()
                    _logger.info("Aggregation %s: subject granted none of the "
                                 "requested fields", row.id)
                    continue
                request.requested_fields = kept
                request.status = AggregationStatus.verified.value
                # Whatever the subject answered on the consent screen IS the
                # authentication behind this release; carry its timestamp so the
                # grants minted below point at when it happened. A screen that
                # took an id_token instead leaves otp_verified_at NULL, and the
                # grant is then recorded as consent-backed rather than OTP-backed.
                verified_at = consent_request.get("otp_verified_at")
                request.otp_verified_at = (
                    datetime.fromisoformat(verified_at) if verified_at else None)
                request.otp_required = bool(verified_at)
                request.otp_channel = consent_request.get("otp_channel")
                request.otp_provider = consent_request.get("otp_provider")
                await session.commit()
                await session.refresh(request)

            await self._mint_grants(request)
            await self.enqueue_fan_out(request.id)
            _logger.info("Aggregation %s: released by consent request %s (%d field(s))",
                         request.id, consent_request_id, len(kept))

    # ── 2. verify ───────────────────────────────────────────────────────────

    async def verify_otp(self, request_id: str, code: str) -> AggregationRequest:
        """Check the code and, on success, start the fan-out in the background.

        The caller gets an immediate answer; delivery happens on the callback.
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
        # it before fanning out, or every registry will deny the aggregator
        # with no_subject_consent.
        await self._mint_grants(request)

        await self.enqueue_fan_out(request.id)
        return request

    async def _mint_grants(self, request) -> None:
        """Record the subject's authentication as an originated grant per binding.

        Without this the aggregator is an unknown quantity to the PDP: it holds
        a policy ceiling but no subject grant, and B8 denies it at every
        registry. Rather than exempt the aggregator - which would leave a path
        to registry data that the subject never touched - what the subject
        actually did is written down, by the CM, as what it actually is.

        ``auth_method`` follows ``request.otp_required``: "otp" when a code was
        answered, "consent" when the partner's policy asked for none and the
        subject's standing grant is the whole of the authority. Recording
        "otp" in the second case would put an act in the audit trail that never
        happened.

        The CM writes one AuthContext for the request and one grant per
        registry binding (CM API #3). Reuse is the CM's call too: the newest
        live grant is reused only when scopes, purpose, method and lawful basis
        all match exactly and at least ``reuse_min_remaining_sec`` is left.
        """
        basis = getattr(request, "lawful_basis", "consent") or "consent"
        method = ("otp" if request.otp_required
                  else "consent" if basis == "consent" else "none")
        by_registry = field_catalog.resolve(request.requested_fields)
        now = datetime.now(timezone.utc)
        valid_until = now + timedelta(seconds=_config.aggregator_consent_validity_sec)

        bindings = []
        for registry, specs in sorted(by_registry.items()):
            cfg = _config.aggregator_registry_map.get(registry)
            if not cfg:
                continue
            bindings.append({"registry": registry,
                             "audience": cfg.get("audience"),
                             "scopes": field_catalog.scopes_for(specs)})

        result = await self.cm.record_grants({
            "aggregation_id": request.id,
            "subject_id": {"type": request.subject_id_type,
                           "value": request.subject_id_value},
            "issuer": _config.aggregator_issuer,
            "auth_method": method,
            "auth_timestamp": (request.otp_verified_at or now).isoformat(),
            "lawful_basis": basis,
            "otp_channel": request.otp_channel,
            "purpose": request.purpose or {"code": _config.aggregator_purpose_code},
            "valid_until": valid_until.isoformat(),
            "reuse_min_remaining_sec": _config.aggregator_grant_reuse_min_remaining_sec,
            "bindings": bindings,
            "consent_request_id": request.consent_request_id,
            "root_consent_id": (request.registry_results or {}).get("validated_consent_id"),
        }) or {}
        for skipped in result.get("skipped") or []:
            _logger.warning("Aggregation %s: no CM binding for %s (%s); it will "
                            "deny with unknown_partner", request.id,
                            skipped.get("registry"), skipped.get("reason"))
        _logger.info("Aggregation %s: %s-backed grants minted for %s (valid %ss), "
                     "reused for %s",
                     request.id, method, result.get("minted"),
                     _config.aggregator_consent_validity_sec, result.get("reused"))

    async def cancel_for_withdrawal(self, *, partner_id: str, subject_id_type: str,
                                    subject_id_value: str) -> int:
        """The subject withdrew consent to this partner: stop what is in flight.

        Called on the CM's ``consent.withdrawn`` event (CM API #5), which the
        CM sends only when no other live consent to the same partner remains.
        ``pending_consent`` waits on a different, not-yet-approved request and
        ``delivering`` has already sent, so neither is touched. A fetch that
        starts between the withdraw and this event is still denied at the
        registry, because the CM has already revoked the grant.
        """
        cancellable = tuple(s.value for s in (
            AggregationStatus.received, AggregationStatus.pending_otp,
            AggregationStatus.verified, AggregationStatus.queued,
            AggregationStatus.fetching, AggregationStatus.fetched))
        async with async_session()() as session:
            result = await session.execute(
                update(AggregationRequest)
                .where(AggregationRequest.partner_id == partner_id,
                       AggregationRequest.subject_id_type == subject_id_type,
                       AggregationRequest.subject_id_value == subject_id_value,
                       AggregationRequest.status.in_(cancellable))
                .values(status=AggregationStatus.rejected.value,
                        failure_reason="consent_withdrawn",
                        claimed_by=None, claimed_at=None, next_retry_at=None))
            await session.commit()
        cancelled = result.rowcount or 0
        _logger.info("Consent withdrawn for %s/%s: %d in-flight aggregation(s) "
                     "cancelled", partner_id, subject_id_value, cancelled)
        return cancelled

    async def verify_for_subject(self, *, aggregation_id: Optional[str],
                                 correlation_id: Optional[str],
                                 subject_id, code: str,
                                 caller: Optional[Dict[str, str]] = None
                                 ) -> AggregationRequest:
        """The farmer's own release: find the request, check it is theirs, verify.

        The subject check is the point. Without it, anyone holding an
        aggregation id and a valid OTP could release a record belonging to
        someone else - and with a provider whose OTP is a constant, that is not
        hypothetical.
        """
        if not aggregation_id and not correlation_id:
            raise AggregationError(400, "no_identifier",
                                   "supply aggregation_id or correlation_id")

        async with async_session()() as session:
            request = None
            if aggregation_id:
                request = await session.get(AggregationRequest, aggregation_id)
            if request is None and correlation_id:
                result = await session.execute(
                    select(AggregationRequest).where(
                        AggregationRequest.correlation_id == correlation_id))
                request = result.scalars().first()
        if request is None:
            raise AggregationError(404, "not_found", "no such aggregation request")

        # The authenticated caller is authoritative. A body subject_id is an
        # extra assertion to agree with, never the thing that establishes who is
        # asking - the probe showed that omitting it used to skip the check
        # entirely and release the record.
        claims = []
        if caller and caller.get("subject_id_value"):
            claims.append((caller.get("subject_id_type") or request.subject_id_type,
                           caller["subject_id_value"], "token"))
        if subject_id is not None and getattr(subject_id, "value", None):
            claims.append((getattr(subject_id, "type", None) or request.subject_id_type,
                           subject_id.value, "body"))
        if not claims:
            raise AggregationError(401, "unauthenticated",
                                   "the caller must be an authenticated subject")

        for wanted_type, wanted_value, where in claims:
            if (wanted_value != request.subject_id_value
                    or wanted_type != request.subject_id_type):
                _logger.warning("Aggregation %s: %s subject mismatch", request.id, where)
                # Deliberately the same 404 as an unknown id: confirming that
                # an id exists but belongs to someone else is a disclosure.
                raise AggregationError(404, "not_found", "no such aggregation request")

        return await self.verify_otp(request.id, code)

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

        aggregated, results = await self._query_registries(request)
        body = self._build_envelope(request, aggregated, results)

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

    async def _query_registries(self, request) -> tuple:
        """One call per registry, failures recorded per registry.

        A partner asking across three registries should not lose two because
        one is down, so a RegistryError is written into ``results`` and the
        loop continues.
        """
        by_registry = field_catalog.resolve(request.requested_fields)
        results_seed = request.registry_results or {}
        default_query = results_seed.get("query_value") or request.subject_id_value
        per_registry = results_seed.get("registry_queries") or {}
        aggregated: Dict[str, Any] = {}
        results: Dict[str, Any] = dict(request.registry_results or {})

        for registry, specs in sorted(by_registry.items()):
            cfg = _config.aggregator_registry_map.get(registry)
            if not cfg:
                results[registry] = {"status": "error", "reason": "not_configured"}
                continue
            scopes = field_catalog.scopes_for(specs)
            query_value = per_registry.get(registry) or default_query
            try:
                records = await self.registries.search(
                    registry, cfg, request.subject_id_type, request.subject_id_value,
                    query_value, scopes, request.purpose)
            except RegistryError as exc:
                _logger.warning("Aggregation %s: %s failed - %s %s",
                                request.id, registry, exc.reason, exc.detail)
                results[registry] = {"status": "error", "reason": exc.reason,
                                     "detail": exc.detail}
                continue

            # Keep only the requested leaves. The registry clamped to whole
            # blocks; this is where the partner's field list is honoured.
            projected: Dict[str, Any] = {}
            for record in records:
                projected.update(field_catalog.project(record, specs))
            aggregated.update(projected)
            results[registry] = {"status": "ok", "records": len(records),
                                 "queried": query_value,
                                 "fields": sorted(projected)}
        return aggregated, results

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

    def _build_envelope(self, request, aggregated: Dict[str, Any],
                        results: Dict[str, Any]) -> Dict[str, Any]:
        """The standard DCI on-search body, signed, ready to POST.

        Built once and then carried on the delivery topic, so a retry re-sends
        the identical bytes. That matters twice over: the JWS stays valid
        (re-signing would change the signature for the same facts), and
        ``message_id`` is stable, which is what lets a partner recognise a
        redelivery of something it has already seen rather than treating it as
        a second result.
        """
        now = datetime.now(timezone.utc)
        any_ok = any(isinstance(v, dict) and v.get("status") == "ok"
                     for v in results.values())

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
            "meta": {
                "aggregated": True,
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
                "registries": {k: v for k, v in results.items() if isinstance(v, dict)},
            },
        }
        if not any_ok:
            header["status_reason_code"] = "AGG-VAL-001"
            header["status_reason_message"] = "no registry returned data"

        message = {
            "transaction_id": request.transaction_id or request.correlation_id,
            "correlation_id": request.correlation_id,
            "search_response": [{
                "reference_id": request.reference_id or request.correlation_id,
                "timestamp": now.isoformat(),
                "status": "succ" if any_ok else "rjct",
                "data": {
                    "reg_type": _config.aggregator_reg_type,
                    "reg_record_type": _config.aggregator_reg_record_type,
                    # One aggregated record. The partner's own field aliases are
                    # the keys, so it never has to know which registry answered.
                    "reg_records": [aggregated] if aggregated else [],
                },
                "pagination": {"page_size": 1, "page_number": 1,
                               "total_count": 1 if aggregated else 0},
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
