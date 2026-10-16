"""Routes for the aggregated fetch.

    POST /dci/registry/sync/search     partner: the on-search is the response (default)
    POST /dci/registry/async/search    partner: 202 ack, on-search POSTed to sender_uri

The partner authorises a search with either its signed consent object
(``authorize.consent_jws``) or a consent the CM holds for it
(``authorize.consent_id``, e.g. granted by the subject's one-time code) plus
its partner-realm bearer token.
    GET  /aggregation/v1/registries    the registry catalog

The partner routes sit under ``/dci/registry`` to mirror the registries' own
partner surface, so a partner's base URL, envelope and signing carry over; the
query is a Beneficiary-360 request. The subject's consent is obtained before
either is called (consent request + one-time code, in the Consent Manager).
"""
import logging
from typing import Optional

from fastapi import Header
from fastapi.responses import JSONResponse
from openg2p_fastapi_common.controller import BaseController

from ..auth import partner_from_token
from ..config import Settings
from ..registry_catalog import get_catalog
from ..schemas.aggregation import SeekAck, SeekEnvelope
from ..services.aggregator_service import AggregationError, AggregatorService

_config = Settings.get_config()
_logger = logging.getLogger(_config.logging_default_logger_name)

_PREFIX = "/aggregation/v1"


def _err(exc: AggregationError) -> JSONResponse:
    return JSONResponse(status_code=exc.status,
                        content={"error": exc.reason, "detail": exc.detail})


def _call(envelope: SeekEnvelope, authorization: Optional[str]) -> dict:
    item = envelope.message.search_request[0]
    criteria = item.search_criteria
    consent_id = criteria.authorize.consent_id
    partner_id = None
    if consent_id:
        # A consent id is only a reference: the token names the partner, and
        # the CM permits only if the consent was given to that partner.
        partner_id = partner_from_token(authorization)
        sender = envelope.header.sender_id
        if sender and sender != partner_id:
            raise AggregationError(
                403, "sender_mismatch",
                "header.sender_id is not the partner named by the bearer token")
    return {
        "consent_jws": criteria.authorize.consent_jws,
        "consent_id": consent_id,
        "partner_id": partner_id,
        "query": criteria.query.wire(),
        "transaction_id": envelope.message.transaction_id,
        "reference_id": item.reference_id,
        "purpose": criteria.purpose,
    }


class AggregatorController(BaseController):
    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self.aggregator = AggregatorService.get_component()
        self.router.tags += ["Aggregation Layer (Beneficiary-360, consent-gated)"]

        self.router.add_api_route(
            "/dci/registry/sync/search", self.search_sync, methods=["POST"],
        )
        self.router.add_api_route(
            "/dci/registry/async/search", self.search_async,
            responses={202: {"model": SeekAck}}, methods=["POST"], status_code=202,
        )
        self.router.add_api_route(
            _PREFIX + "/registries", self.registries, methods=["GET"],
        )

    # ── partner ─────────────────────────────────────────────────────────────

    async def search_sync(
        self, envelope: SeekEnvelope,
        authorization: Optional[str] = Header(default=None, alias="Authorization"),
    ):
        """Answer a Beneficiary-360 request in the response.

        The body is the signed DCI on-search whose ``reg_records[0]`` is the
        bene-360 response. The subject must already have consented to the
        partner in the Consent Manager; otherwise 403 ``no_subject_consent``.
        """
        try:
            return await self.aggregator.search_sync(**_call(envelope, authorization))
        except AggregationError as exc:
            return _err(exc)

    async def search_async(
        self, envelope: SeekEnvelope,
        authorization: Optional[str] = Header(default=None, alias="Authorization"),
    ):
        """Accept a Beneficiary-360 request and answer it on ``header.sender_uri``.

        Every check (callback URL, query, consent) runs before the 202; the
        signed on-search is POSTed to the callback as soon as the registries
        have answered. One attempt, no retry.
        """
        try:
            aggregation = await self.aggregator.search_async(
                callback_url=envelope.header.sender_uri, **_call(envelope, authorization))
        except AggregationError as exc:
            return _err(exc)
        return SeekAck(
            correlation_id=aggregation.correlation_id,
            transaction_id=aggregation.transaction_id,
            status="pdng",
            accepted_scopes=aggregation.requested_scopes,
            registries=sorted(get_catalog().split_scope_ids(aggregation.requested_scopes)),
            callback_url=aggregation.callback_url,
            message="Accepted. The Beneficiary-360 response will be POSTed to "
                    "sender_uri as on-search when the registries have answered.",
        )

    # ── discovery ───────────────────────────────────────────────────────────

    async def registries(self):
        """Which registries, scopes and fields a partner may ask for.

        Open by design: it is the same for every caller and describes nobody's
        data. It carries no URL, binding or secret.
        """
        return {"registries": get_catalog().describe()}
