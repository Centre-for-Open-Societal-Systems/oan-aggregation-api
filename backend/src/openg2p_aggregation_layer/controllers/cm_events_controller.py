"""Consent events from the Consent Manager (CM API #5).

    POST /aggregation/v1/cm-events

Inside consent-management two CM services called the aggregator in-process:
approving a consent request released the aggregations parked on it, and
withdrawing a consent cancelled the subject's in-flight fetches for that
partner. Across a process boundary those calls become events the CM POSTs here.

    consent_request.approved   {"consent_request_id": "..."}
    consent.withdrawn          {"consent_id": "...", "partner_id": "...",
                                "subject_id": {"type": "...", "value": "..."}}

Both handlers are idempotent, so the CM may retry until it gets a 2xx.

Authenticated with an HMAC-SHA256 over ``"<timestamp>.<raw body>"`` in
``X-CM-Signature: t=<unix seconds>,v1=<hex>`` - the same scheme the CM already
verifies on inbound AWE webhooks, pointed the other way.
"""
import hashlib
import hmac
import json
import logging
import time

from fastapi import Request
from fastapi.responses import JSONResponse
from openg2p_fastapi_common.controller import BaseController

from ..config import Settings
from ..services.aggregator_service import AggregatorService
from ..services.cm_client import CMError

_config = Settings.get_config()
_logger = logging.getLogger(_config.logging_default_logger_name)

EVENT_APPROVED = "consent_request.approved"
EVENT_WITHDRAWN = "consent.withdrawn"


def _signature_ok(header: str, body: bytes) -> bool:
    secret = _config.cm_events_hmac_secret
    if not secret:
        # Dev only. Logged on every event so it cannot go unnoticed.
        _logger.warning("cm_events_hmac_secret is empty - CM event accepted "
                        "WITHOUT a signature check")
        return True
    parts = dict(p.split("=", 1) for p in (header or "").split(",") if "=" in p)
    try:
        ts = int(parts.get("t", ""))
    except ValueError:
        return False
    if abs(time.time() - ts) > _config.cm_events_max_skew_sec:
        return False
    expected = hmac.new(secret.encode("utf-8"),
                        str(ts).encode("ascii") + b"." + body,
                        hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, parts.get("v1", ""))


class CMEventsController(BaseController):
    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self.aggregator = AggregatorService.get_component()
        self.router.tags += ["Consent Manager events"]
        self.router.add_api_route(
            "/aggregation/v1/cm-events", self.receive, methods=["POST"],
        )

    async def receive(self, request: Request):
        body = await request.body()
        if not _signature_ok(request.headers.get("X-CM-Signature", ""), body):
            return JSONResponse(status_code=401, content={"error": "bad_signature"})
        try:
            event = json.loads(body)
        except ValueError:
            return JSONResponse(status_code=400, content={"error": "not_json"})

        kind = event.get("type")
        data = event.get("data") or {}
        try:
            if kind == EVENT_APPROVED:
                await self.aggregator.release_for_consent_request(
                    data["consent_request_id"])
                return {"status": "ok"}
            if kind == EVENT_WITHDRAWN:
                subject = data.get("subject_id") or {}
                cancelled = await self.aggregator.cancel_for_withdrawal(
                    partner_id=data["partner_id"],
                    subject_id_type=subject.get("type", ""),
                    subject_id_value=subject.get("value", ""))
                return {"status": "ok", "cancelled": cancelled}
        except KeyError as exc:
            return JSONResponse(status_code=400,
                                content={"error": "missing_field", "detail": str(exc)})
        except CMError as exc:
            # The CM could not answer our follow-up read. 503 makes it retry.
            return JSONResponse(status_code=503,
                                content={"error": exc.reason, "detail": exc.detail})

        # An event type we do not handle is acknowledged, not refused: the CM
        # may grow new events before this service learns about them.
        _logger.info("CM event %s ignored (type %r)", event.get("event_id"), kind)
        return {"status": "ignored"}
