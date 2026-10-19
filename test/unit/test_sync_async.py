"""The partner path with no store: sync answers in the response, async on the
callback, and every consent decision is the CM's.

No CM or registry is called: the CM is a fake, the registries' outcomes are
fixed, and the callback is an httpx mock transport."""
import asyncio
import base64
import json
import logging

import httpx
import pytest

from openg2p_aggregation_layer import bene360
from openg2p_aggregation_layer.services import aggregator_service
from openg2p_aggregation_layer.services.aggregator_service import (
    AggregationError,
    AggregatorService,
)
from openg2p_aggregation_layer.services.cm_client import CMClient, CMError

FID = "7615076397"
QUERY = {"@context": bene360.CONTEXT_URL, "foundationalId": FID,
         "timeframe": "Timeframe-Short", "sections": ["REGISTRIES"]}
CALLBACK = "http://partner.test/on-search"


def _jws(claims):
    body = base64.urlsafe_b64encode(json.dumps(claims).encode()).rstrip(b"=").decode()
    return "eyJhbGciOiJFZERTQSJ9." + body + ".sig"


CONSENT = _jws({"aud": "partner-x", "subject_id": {"type": "national_id", "value": FID}})


class FakeCM:
    decode_claims = staticmethod(CMClient.decode_claims)

    def __init__(self, decision=None, status="active"):
        self.decision = decision
        self.status = status
        self.validated = []

    async def validate(self, consent_jws, requested_scopes, purpose=None):
        self.validated.append(requested_scopes)
        if self.decision is not None:
            return self.decision
        return {"decision": "permit", "consent_id": "c-1", "lawful_basis": "consent",
                "subject_id": {"type": "national_id", "value": FID},
                "effective_data_scopes": requested_scopes}

    async def consent_status(self, consent_id):
        if isinstance(self.status, Exception):
            raise self.status
        return self.status


@pytest.fixture
def service(four, monkeypatch):
    from openg2p_aggregation_layer.services.crypto_service import CryptoService
    from openg2p_aggregation_layer.services.registry_client import RegistryClient

    CryptoService()                 # ephemeral key: nothing verifies it here
    monkeypatch.setattr(aggregator_service, "get_catalog", lambda: four)
    svc = object.__new__(AggregatorService)
    svc.registries = RegistryClient()
    svc.cm = FakeCM()
    svc._tasks = set()
    svc.fanned_out = []

    async def query_registries(aggregation):
        svc.fanned_out.append(aggregation.correlation_id)
        return {code: {"status": "ok", "records": 1, "discarded": 0, "membership": None,
                       "scopes": []} for code in four.codes()}

    svc._query_registries = query_registries

    svc.posted = []

    def partner(request: httpx.Request) -> httpx.Response:
        svc.posted.append((str(request.url), json.loads(request.content)))
        return httpx.Response(200, json={"ok": True})

    real_client = httpx.AsyncClient
    monkeypatch.setattr(httpx, "AsyncClient", lambda **kw: real_client(
        transport=httpx.MockTransport(partner), **kw))
    return svc


def _sync(svc, **kw):
    return asyncio.run(svc.search_sync(
        consent_jws=CONSENT, query=QUERY, transaction_id="t-1",
        reference_id="r-1", purpose=None, **kw))


def _async(svc, callback=CALLBACK):
    async def run():
        aggregation = await svc.search_async(
            consent_jws=CONSENT, query=QUERY, callback_url=callback,
            transaction_id="t-1", reference_id="r-1", purpose=None)
        posted_before_ack = list(svc.posted)
        await asyncio.gather(*svc._tasks)
        return aggregation, posted_before_ack
    return asyncio.run(run())


def test_sync_answers_in_the_response(service, four, caplog):
    with caplog.at_level(logging.INFO, logger=aggregator_service._audit.name):
        envelope = _sync(service)
    assert envelope["signature"] and ".." in envelope["signature"]
    header, message = envelope["header"], envelope["message"]
    assert (header["action"], header["status"], header["receiver_id"]) == (
        "on-search", "succ", "partner-x")
    assert message["transaction_id"] == "t-1"
    [item] = message["search_response"]
    assert item["reference_id"] == "r-1"
    [record] = item["data"]["reg_records"]
    assert record["meta"]["sourceSystemsQueried"] == four.codes()
    # The CM is asked only about the registries in play.
    assert service.cm.validated == [four.scope_ids(four.codes())]
    assert service.posted == []

    [line] = [json.loads(r.getMessage()) for r in caplog.records
              if r.name == aggregator_service._audit.name]
    assert (line["mode"], line["outcome"], line["partner"]) == ("sync", "answered", "partner-x")
    assert FID not in json.dumps(line) and line["subject"].endswith(FID[-4:])


def test_no_subject_consent_says_where_to_get_it(service):
    service.cm = FakeCM({"decision": "deny", "reason_code": "no_subject_consent",
                         "detail": "the subject has not granted an active consent"})
    with pytest.raises(AggregationError) as caught:
        _sync(service)
    assert (caught.value.status, caught.value.reason) == (403, "no_subject_consent")
    assert "/consent/v1/consent-requests" in caught.value.detail
    assert service.fanned_out == []


def test_any_other_denial_is_passed_on(service):
    service.cm = FakeCM({"decision": "deny", "reason_code": "signature_invalid",
                         "detail": "signature did not verify"})
    with pytest.raises(AggregationError) as caught:
        _sync(service)
    assert (caught.value.status, caught.value.reason, caught.value.detail) == (
        403, "signature_invalid", "signature did not verify")
    assert service.fanned_out == []


def test_another_subject_is_refused(service, four):
    service.cm = FakeCM({"decision": "permit", "consent_id": "c-1",
                         "subject_id": {"type": "national_id", "value": "999"},
                         "effective_data_scopes": four.scope_ids()})
    with pytest.raises(AggregationError) as caught:
        _sync(service)
    assert (caught.value.status, caught.value.reason) == (403, "subject_mismatch")
    assert service.fanned_out == []


@pytest.mark.parametrize("callback, reason", [(None, "no_callback"),
                                               ("ftp://x/cb", "bad_callback")])
def test_async_needs_a_callback(service, callback, reason):
    with pytest.raises(AggregationError) as caught:
        _async(service, callback)
    assert (caught.value.status, caught.value.reason) == (400, reason)
    assert service.cm.validated == [] and service.fanned_out == []


def test_async_acks_then_posts_the_answer(service):
    aggregation, posted_before_ack = _async(service)
    assert posted_before_ack == []
    assert aggregation.callback_url == CALLBACK and aggregation.mode == "async"
    [(url, body)] = service.posted
    assert url == CALLBACK
    assert body["header"]["status"] == "succ"
    assert body["message"]["correlation_id"] == aggregation.correlation_id
    assert body["message"]["search_response"][0]["data"]["reg_records"]
    assert service._tasks == set()


@pytest.mark.parametrize("status, code", [
    ("revoked", "consent_withdrawn"),
    (CMError(503, "cm_unavailable"), "consent_check_failed"),
])
def test_async_sends_no_data_once_the_consent_is_gone(service, status, code):
    service.cm.status = status
    _async(service)
    [(_url, body)] = service.posted
    assert body["header"]["status"] == "rjct"
    assert body["header"]["status_reason_code"] == code
    [item] = body["message"]["search_response"]
    assert item["status"] == "rjct" and "data" not in item


@pytest.mark.parametrize("status, http, code", [
    ("revoked", 403, "consent_withdrawn"),
    ("expired", 403, "consent_withdrawn"),
    (None, 403, "consent_withdrawn"),                       # the CM no longer knows it
    (CMError(503, "cm_unavailable"), 503, "consent_check_failed"),
])
def test_sync_releases_no_data_once_the_consent_is_gone(service, status, http, code):
    """The same last check as async, after the registries answered and
    before the answer leaves (e.g. revoked while the registries were asked)."""
    service.cm.status = status
    with pytest.raises(AggregationError) as exc:
        _sync(service)
    assert (exc.value.status, exc.value.reason) == (http, code)
    assert service.fanned_out  # the registries were asked; their data was not released


def test_sync_checks_the_consent_after_the_fan_out(service):
    order = []
    real_query = service._query_registries

    async def query(aggregation):
        order.append("registries")
        return await real_query(aggregation)

    async def status(consent_id):
        order.append("final check")
        return "active"

    service._query_registries = query
    service.cm.consent_status = status
    assert _sync(service)["header"]["status"] == "succ"
    assert order == ["registries", "final check"]
