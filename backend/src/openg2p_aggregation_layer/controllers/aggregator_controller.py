"""Routes for the aggregated async fetch.

    POST /dci/registry/async/search          seek  - partner, consent-signed
    POST /consent/v1/aggregation/{id}/verify-otp   - the subject
    GET  /consent/v1/aggregation/{id}              - status / audit
    GET  /consent/v1/aggregation/fields            - the field catalog
    GET  /consent/v1/aggregation/{id}/otp          - DEV ONLY, config-gated

The seek route sits under ``/dci/registry`` to mirror the registries' own
partner surface, so a partner's base URL and envelope handling carry over. The
subject-facing routes sit under ``/consent/v1`` with the rest of CM's API.

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
from ..schemas.aggregation import (
    AggregationStatusResponse,
    FarmerConsentValidateRequest,
    FarmerConsentValidateResponse,
    SeekAck,
    SeekEnvelope,
    VerifyOtpRequest,
    VerifyOtpResponse,
)
from ..schemas.common import SubjectId
from ..services import field_catalog
from ..services.aggregator_service import AggregationError, AggregatorService

_config = Settings.get_config()
_logger = logging.getLogger(_config.logging_default_logger_name)


def _err(exc: AggregationError) -> JSONResponse:
    return JSONResponse(status_code=exc.status,
                        content={"error": exc.reason, "detail": exc.detail})


class AggregatorController(BaseController):
    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self.aggregator = AggregatorService.get_component()
        self.router.tags += ["Aggregator (async, OTP-gated)"]

        self.router.add_api_route(
            "/dci/registry/async/search", self.seek,
            responses={202: {"model": SeekAck}}, methods=["POST"], status_code=202,
        )
        self.router.add_api_route(
            "/consent/v1/aggregation/fields", self.fields, methods=["GET"],
        )
        # Registered BEFORE /aggregation/{aggregation_id}: FastAPI matches in
        # declaration order, so the other way round "queue" is swallowed as an
        # aggregation id and this route is unreachable.
        self.router.add_api_route(
            "/consent/v1/aggregation/queue", self.queue, methods=["GET"],
        )
        self.router.add_api_route(
            "/consent/v1/aggregation/{aggregation_id}", self.status,
            responses={200: {"model": AggregationStatusResponse}}, methods=["GET"],
        )
        self.router.add_api_route(
            "/consent/v1/aggregation/{aggregation_id}/verify-otp", self.verify_otp,
            responses={200: {"model": VerifyOtpResponse}}, methods=["POST"],
        )
        # The farmer-facing name for the same act. Flat body, subject checked.
        self.router.add_api_route(
            "/consent/v1/farmer-consent-validate", self.farmer_consent_validate,
            responses={200: {"model": FarmerConsentValidateResponse}}, methods=["POST"],
        )
        if _config.otp_debug_enabled:
            # Guarded by config and loud about it: this hands out the subject's
            # OTP, which defeats the second factor. It exists so the flow is
            # demonstrable without an SMS gateway.
            _logger.warning(
                "otp_debug_enabled=true - GET /consent/v1/aggregation/{id}/otp will "
                "return the subject's OTP in plaintext. Never enable outside dev.")
            self.router.add_api_route(
                "/consent/v1/aggregation/{aggregation_id}/otp", self.peek_otp,
                methods=["GET"],
            )

    # ── partner ─────────────────────────────────────────────────────────────

    async def seek(self, envelope: SeekEnvelope):
        """Accept the ask, issue the OTP, return an ack. No data is fetched."""
        item = envelope.message.search_request[0]
        criteria = item.search_criteria
        try:
            request = await self.aggregator.seek(
                consent_jws=criteria.authorize.consent_jws,
                fields=criteria.fields,
                callback_url=envelope.header.sender_uri,
                query_value=criteria.query.value.id_value,
                registry_queries=criteria.registry_queries,
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
            accepted_fields=request.requested_fields,
            registries=(request.registry_results or {}).get("registries") or [],
            callback_url=request.callback_url,
            consent_request_id=request.consent_request_id,
            consent_url=(
                _config.consent_ui_base_url.rstrip("/") + "/consent/" + request.consent_request_id
                if waiting_on_consent and _config.consent_ui_base_url else None),
            message=(
                ("The subject has not consented to this partner yet. A consent "
                 "request was raised for them; send them to consent_url. Data "
                 "will be POSTed to sender_uri as on-search once they grant it.")
                if waiting_on_consent else
                ("No consent was sought: this partner operates on the "
                 "controller's own lawful basis, and its allowed data scopes "
                 "are the whole of the authority. Data will be POSTed to "
                 "sender_uri as on-search shortly.")
                if internal else
                ("This partner requires no one-time code. The subject's consent "
                 "is the whole of the authority; data will be POSTed to "
                 "sender_uri as on-search shortly.")
                if skipped_otp else
                ("OTP sent to the subject. Data will be POSTed to sender_uri "
                 "as on-search once it is verified.")),
        )

    # ── subject ─────────────────────────────────────────────────────────────

    async def verify_otp(self, aggregation_id: str, data: VerifyOtpRequest):
        try:
            request = await self.aggregator.verify_otp(aggregation_id, data.otp)
        except AggregationError as exc:
            return _err(exc)
        return VerifyOtpResponse(
            aggregation_id=request.id, status=request.status,
            # "Fetching from the registries" was true when the handler did it
            # inline. It now queues the work, and saying otherwise invites a
            # caller to treat this 200 as meaning the registries have answered.
            message="OTP verified. The fetch is queued; the aggregated record "
                    "will be POSTed to the partner's callback when it "
                    "completes. Poll GET /consent/v1/aggregation/{id} for "
                    "progress.",
        )

    async def farmer_consent_validate(
        self, data: FarmerConsentValidateRequest,
        subject: Dict[str, str] = Depends(get_current_subject),
    ):
        """The farmer validates their OTP; on success the data goes to the partner.

        Scoped to the caller, like the rest of the subject-facing API: the farmer
        is taken from the TOKEN. A subject_id in the body is cross-checked but
        cannot establish identity on its own - otherwise an aggregation id plus
        an OTP would be enough, and the mock provider's OTP is a constant.
        """
        try:
            request = await self.aggregator.verify_for_subject(
                aggregation_id=data.aggregation_id,
                correlation_id=data.correlation_id,
                subject_id=data.subject_id,
                caller=subject,
                code=data.otp,
            )
        except AggregationError as exc:
            return _err(exc)

        return FarmerConsentValidateResponse(
            aggregation_id=request.id,
            status=request.status,
            subject_id=SubjectId(type=request.subject_id_type,
                                 value=request.subject_id_value),
            released_fields=request.requested_fields or [],
            released_to=request.partner_audience or request.partner_id,
            callback_url=request.callback_url,
            message="OTP validated. The consented fields are queued for "
                    "collection and will be POSTed to the partner's callback.",
        )

    # ── discovery / audit ───────────────────────────────────────────────────

    async def fields(self):
        """What a partner may ask for, without reading the source."""
        return {"fields": field_catalog.describe()}

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
