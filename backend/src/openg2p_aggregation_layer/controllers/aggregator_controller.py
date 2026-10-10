"""Routes for the aggregated async fetch.

    POST /dci/registry/async/search                  seek - partner, consent-signed
    GET  /aggregation/v1/registries                  the registry catalog
    GET  /aggregation/v1/queue                       queue health
    GET  /aggregation/v1/requests/{id}               status - the subject
    POST /aggregation/v1/requests/{id}/verify-otp    release - the subject
    GET  /aggregation/v1/requests/{id}/otp           DEV ONLY, config-gated

The seek route sits under ``/dci/registry`` to mirror the registries' own
partner surface, so a partner's base URL, envelope and signing carry over; its
query is a Beneficiary-360 request. Everything else is this service's own API
under ``/aggregation/v1``.

The existing ``/dci/registry/sync/search`` on each registry is untouched.
"""
import logging
from typing import Dict

from fastapi import Depends
from fastapi.responses import JSONResponse
from openg2p_fastapi_common.controller import BaseController

from ..auth import current_identity, get_current_subject
from ..config import Settings
from ..models import AggregationStatus
from ..registry_catalog import get_catalog
from ..schemas.aggregation import (
    AggregationStatusResponse,
    SeekAck,
    SeekEnvelope,
    VerifyOtpRequest,
    VerifyOtpResponse,
)
from ..schemas.common import SubjectId
from ..services.aggregator_service import AggregationError, AggregatorService

_config = Settings.get_config()
_logger = logging.getLogger(_config.logging_default_logger_name)

_PREFIX = "/aggregation/v1"


def _err(exc: AggregationError) -> JSONResponse:
    return JSONResponse(status_code=exc.status,
                        content={"error": exc.reason, "detail": exc.detail})


class AggregatorController(BaseController):
    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self.aggregator = AggregatorService.get_component()
        self.router.tags += ["Aggregation Layer (Beneficiary-360, OTP-gated)"]

        self.router.add_api_route(
            "/dci/registry/async/search", self.seek,
            responses={202: {"model": SeekAck}}, methods=["POST"], status_code=202,
        )
        self.router.add_api_route(
            _PREFIX + "/registries", self.registries, methods=["GET"],
        )
        self.router.add_api_route(
            _PREFIX + "/queue", self.queue, methods=["GET"],
        )
        self.router.add_api_route(
            _PREFIX + "/requests/{aggregation_id}", self.status,
            responses={200: {"model": AggregationStatusResponse}}, methods=["GET"],
        )
        self.router.add_api_route(
            _PREFIX + "/requests/{aggregation_id}/verify-otp", self.verify_otp,
            responses={200: {"model": VerifyOtpResponse}}, methods=["POST"],
        )
        if _config.otp_debug_enabled:
            # Guarded by config and loud about it: this hands out the subject's
            # OTP, which defeats the second factor. It exists so the flow is
            # demonstrable without an SMS gateway.
            _logger.warning(
                "otp_debug_enabled=true - GET %s/requests/{id}/otp will return the "
                "subject's OTP in plaintext. Never enable outside dev.", _PREFIX)
            self.router.add_api_route(
                _PREFIX + "/requests/{aggregation_id}/otp", self.peek_otp,
                methods=["GET"],
            )

    # ── partner ─────────────────────────────────────────────────────────────

    async def seek(self, envelope: SeekEnvelope):
        """Accept a Beneficiary-360 request, issue the OTP, return an ack.

        No data is fetched here; the bene-360 response is POSTed to
        ``header.sender_uri`` as a DCI on-search once the subject authorises it.
        """
        item = envelope.message.search_request[0]
        criteria = item.search_criteria
        try:
            request = await self.aggregator.seek(
                consent_jws=criteria.authorize.consent_jws,
                query=criteria.query.wire(),
                callback_url=envelope.header.sender_uri,
                transaction_id=envelope.message.transaction_id,
                reference_id=item.reference_id,
                purpose=criteria.purpose,
            )
        except AggregationError as exc:
            return _err(exc)

        waiting_on_consent = request.status == AggregationStatus.pending_consent.value
        skipped_otp = (not waiting_on_consent) and not request.otp_required
        internal = request.lawful_basis != "consent"
        return SeekAck(
            aggregation_id=request.id,
            correlation_id=request.correlation_id,
            transaction_id=request.transaction_id,
            status="pdng",
            # Three ways to get here and only one of them wants a code from the
            # partner: pending_consent puts the OTP on the consent screen, and a
            # policy with no required_auth_method has already fanned out.
            otp_required=not waiting_on_consent and bool(request.otp_required),
            otp_channel=request.otp_channel,
            otp_expires_at=request.otp_expires_at,
            accepted_scopes=request.requested_scopes,
            registries=sorted(get_catalog().split_scope_ids(request.requested_scopes)),
            callback_url=request.callback_url,
            consent_request_id=request.consent_request_id,
            consent_url=(
                _config.consent_ui_base_url.rstrip("/") + "/consent/" + request.consent_request_id
                if waiting_on_consent and _config.consent_ui_base_url else None),
            message=(
                ("The subject has not consented to this partner yet. A consent "
                 "request was raised for them; send them to consent_url. The "
                 "Beneficiary-360 response will be POSTed to sender_uri as "
                 "on-search once they grant it.")
                if waiting_on_consent else
                ("No consent was sought: this partner operates on the "
                 "controller's own lawful basis, and its allowed data scopes "
                 "are the whole of the authority. The Beneficiary-360 response "
                 "will be POSTed to sender_uri as on-search shortly.")
                if internal else
                ("This partner requires no one-time code. The subject's consent "
                 "is the whole of the authority; the Beneficiary-360 response "
                 "will be POSTed to sender_uri as on-search shortly.")
                if skipped_otp else
                ("OTP sent to the subject. The Beneficiary-360 response will be "
                 "POSTed to sender_uri as on-search once it is verified.")),
        )

    # ── subject ─────────────────────────────────────────────────────────────

    async def verify_otp(self, aggregation_id: str, data: VerifyOtpRequest,
                         subject: Dict[str, str] = Depends(get_current_subject)):
        """The subject enters their OTP; on success the data goes to the partner.

        Scoped to the caller, like the rest of the subject-facing API: the
        subject is taken from the TOKEN, and the request must be theirs. An
        aggregation id plus an OTP is not enough on its own.
        """
        try:
            request = await self.aggregator.verify_for_subject(
                aggregation_id=aggregation_id, code=data.otp, caller=subject,
                subject_id=data.subject_id)
        except AggregationError as exc:
            return _err(exc)
        return VerifyOtpResponse(
            aggregation_id=request.id,
            status=request.status,
            subject_id=SubjectId(type=request.subject_id_type,
                                 value=request.subject_id_value),
            released_scopes=request.requested_scopes or [],
            released_to=request.partner_audience or request.partner_id,
            # The handler queues the work; this 200 does not mean the
            # registries have answered.
            message="OTP verified. The fetch is queued; the Beneficiary-360 "
                    "response will be POSTed to the partner's callback when it "
                    "completes. Poll GET %s/requests/{id} for progress." % _PREFIX,
        )

    # ── discovery / audit ───────────────────────────────────────────────────

    async def registries(self):
        """Which registries, scopes and fields a partner may ask for.

        Open by design: it is the same for every caller and describes nobody's
        data. It carries no URL, binding or secret.
        """
        return {"registries": get_catalog().describe()}

    async def queue(self, identity=Depends(current_identity)):
        """Is the fan-out queue healthy, and what is in it?

        Authenticated but not role-gated: it exposes counts and configuration,
        never a subject, a field value or a callback URL. Anyone who may call
        the API at all may ask whether it is keeping up.
        """
        return await self.aggregator.queue_status()

    async def status(self, aggregation_id: str,
                     subject: Dict[str, str] = Depends(get_current_subject)):
        request = await self.aggregator.get(aggregation_id)
        if request is None or not self._belongs_to(request, subject):
            # Same 404 either way: confirming a request exists but belongs to
            # someone else is itself a disclosure.
            return JSONResponse(status_code=404, content={"error": "not_found"})
        return AggregationStatusResponse.model_validate(request)

    @staticmethod
    def _belongs_to(request, subject: Dict[str, str]) -> bool:
        return (request.subject_id_value == subject.get("subject_id_value")
                and request.subject_id_type == subject.get("subject_id_type"))

    async def peek_otp(self, aggregation_id: str,
                       subject: Dict[str, str] = Depends(get_current_subject)):
        request = await self.aggregator.get(aggregation_id)
        if request is None or not self._belongs_to(request, subject):
            return JSONResponse(status_code=404, content={"error": "not_found"})
        # Even in dev the hash is all that is stored, so the code cannot be
        # recovered here. What this endpoint gives is the state, and the log
        # line carries the code itself.
        payload = {
            "aggregation_id": request.id,
            "status": request.status,
            "otp_provider": request.otp_provider,
            "otp_channel": request.otp_channel,
            "otp_destination": request.otp_destination,
            "otp_expires_at": request.otp_expires_at,
            "otp_attempts": request.otp_attempts,
        }
        if request.otp_debug_code:
            payload["otp"] = request.otp_debug_code
            payload["note"] = ("DEV ONLY. otp_debug_enabled is on, so the "
                               "plaintext is kept and returned here.")
        elif (request.otp_provider or "") == "fayda":
            payload["note"] = ("Fayda stores only a salted hash bound to "
                               "(transaction, individualId). Read the code from "
                               "the service log: grep '[OTP]'.")
        else:
            payload["note"] = ("Only the hash is stored. Read the code from the "
                               "service log: grep '[OTP]'.")
        return payload
