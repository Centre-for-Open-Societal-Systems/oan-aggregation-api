"""Consent exchange mode: the exchange Consent Manager (stubbed) turns the
partner's consent into one consent receipt per registry."""

import json

import httpx
import pytest
from conftest import CSR_URL, FR_URL, SCOPES
from test_engine import Harness

CM_URL = "http://exchange-cm.test"
VALIDATE_URL = f"{CM_URL}/consent/v1/validate"
RECEIPTS = {"farmer-registry": "fr.receipt.jws", "crop-sown-registry": "csr.receipt.jws"}


class ExchangeCm:
    def __init__(self):
        self.calls = []
        self.response = lambda body: httpx.Response(
            200, json={"decision": "permit", "reason_code": "ok", "receipts": dict(RECEIPTS)}
        )

    def __call__(self, request: httpx.Request):
        body = json.loads(request.content)
        self.calls.append((str(request.url), body))
        return self.response(body)


@pytest.fixture
def x(use_cases_dir, composite_p12, pm_transport, registries, partner_key):
    h = Harness(use_cases_dir, composite_p12, pm_transport, registries, partner_key)
    h.cm = ExchangeCm()
    inner = h.client._transport

    async def mux(request: httpx.Request):
        if str(request.url).startswith(CM_URL):
            return h.cm(request)
        return await inner.handle_async_request(request)

    h.client = httpx.AsyncClient(transport=httpx.MockTransport(mux))
    h.engine.settings.consent_mode = "exchange"
    h.engine.settings.exchange_cm_url = CM_URL + "/"
    return h


def authorize(call):
    return call["message"]["search_request"][0]["search_criteria"].get("authorize")


async def test_exchange_is_default_and_passthrough_never_calls_cm(x):
    from openg2p_agri_composite.core.engine import EngineSettings

    assert EngineSettings().consent_mode == "exchange"
    x.engine.settings.consent_mode = "passthrough"  # CM URL still set: ignored
    env = x.envelope()
    status, body = await x.query(env)
    assert status == 200, body
    assert x.cm.calls == []
    assert len(x.registries.calls) == 3
    assert all(authorize(c)["consent_jws"] == env["message"]["consent_jws"] for _u, c in x.registries.calls)


async def test_exchange_sends_each_registry_its_receipt(x):
    env = x.envelope(parameters={"crop_year": 2019})
    status, body = await x.query(env)
    assert status == 200, body
    assert body["message"]["sources"] == {
        "farmer": {"status": "ok"}, "season_summaries": {"status": "ok"}, "crop_seasons": {"status": "ok"}}
    assert len(x.cm.calls) == 1
    url, req = x.cm.calls[0]
    assert url == VALIDATE_URL
    assert req["consent_jws"] == env["message"]["consent_jws"]
    assert req["issue_receipts"] is True
    assert req["partner_id"] == "agri-composite"
    assert req["request_context"]["subject_id"] == env["message"]["subject"]
    # caller terms: the use case's purpose, the policy's limits, the use case as the template label;
    # the CM reads no use case and no use-case policy
    assert "use_case" not in req["request_context"]
    assert req["request_context"]["purpose"] == "credit-assessment"
    assert req["request_context"]["template"] == "loan-profile@1"
    assert req["request_context"]["limits"] == {"allowed_signing_algs": ["EdDSA", "ES256"],
                                                "allow_partner_consents": False,
                                                "allow_assisted_confirmation": False}
    # receipts carry only the use case's scopes (required and optional), nothing else the consent grants
    assert req["request_context"]["requested_scopes"] == sorted(s for scopes in SCOPES.values() for s in scopes)
    assert [authorize(c)["consent_jws"] for c in x.registries.calls_to(FR_URL)] == ["fr.receipt.jws"]
    assert [authorize(c)["consent_jws"] for c in x.registries.calls_to(CSR_URL)] == ["csr.receipt.jws"] * 2


async def test_exchange_never_asks_for_scopes_beyond_the_use_case(x):
    grants = [{"data_controller": c, "data_scopes": list(s) + [f"{c}.extra"]} for c, s in SCOPES.items()]
    status, body = await x.query(x.envelope(consent=x.consent(grants=grants)))
    assert status == 200, body
    requested = x.cm.calls[0][1]["request_context"]["requested_scopes"]
    assert not [s for s in requested if s.endswith(".extra")]


async def test_exchange_partner_checks_still_run_first(x, partner_key):
    from conftest import Key

    status, body = await x.query(x.envelope(consent=x.consent(key=Key("other"))))
    assert status == 403 and body["message"]["error"]["code"] == "consent_signature_invalid"
    assert x.cm.calls == [] and x.registries.calls == []


async def test_exchange_cm_deny(x):
    x.cm.response = lambda body: httpx.Response(
        200, json={"decision": "deny", "reason_code": "unknown_partner", "detail": "not onboarded"})
    status, body = await x.query(x.envelope())
    assert status == 403
    err = body["message"]["error"]
    assert err["code"] == "consent_denied" and "unknown_partner" in err["message"]
    assert x.registries.calls == []


async def test_exchange_permit_without_receipt_for_mandatory_source(x):
    x.cm.response = lambda body: httpx.Response(
        200, json={"decision": "permit", "receipts": {"crop-sown-registry": "csr.receipt.jws"}})
    status, body = await x.query(x.envelope())
    assert status == 403
    err = body["message"]["error"]
    assert err["code"] == "consent_receipt_missing" and "farmer-registry" in err["message"]
    assert x.registries.calls == []


async def test_exchange_no_receipts_field_fails_mandatory(x):
    x.cm.response = lambda body: httpx.Response(200, json={"decision": "permit"})
    status, body = await x.query(x.envelope())
    assert status == 403 and body["message"]["error"]["code"] == "consent_receipt_missing"


async def test_exchange_missing_receipt_for_optional_source(x):
    x.cm.response = lambda body: httpx.Response(
        200, json={"decision": "permit", "receipts": {"farmer-registry": "fr.receipt.jws"}})
    status, body = await x.query(x.envelope())
    assert status == 200, body
    srcs = body["message"]["sources"]
    assert srcs["farmer"]["status"] == "ok"
    for sid in ("season_summaries", "crop_seasons"):
        assert srcs[sid]["status"] == "unavailable"
        assert "no consent receipt for crop-sown-registry" in srcs[sid]["detail"]
    assert x.registries.calls_to(CSR_URL) == []


async def test_exchange_ungranted_optional_source_stays_denied(x):
    status, body = await x.query(x.envelope(consent=x.consent(controllers=("farmer-registry",))))
    assert status == 200, body
    assert body["message"]["sources"]["crop_seasons"]["status"] == "denied"


@pytest.mark.parametrize(
    "fail, http, code",
    [
        (lambda r: (_ for _ in ()).throw(httpx.ConnectError("down")), 503, "consent_exchange_unavailable"),
        (lambda r: (_ for _ in ()).throw(httpx.ReadTimeout("slow")), 503, "consent_exchange_unavailable"),
        (lambda r: httpx.Response(502), 503, "consent_exchange_unavailable"),
        (lambda r: httpx.Response(404), 502, "consent_exchange_error"),
        (lambda r: httpx.Response(200, content=b"nope"), 502, "consent_exchange_error"),
    ],
)
async def test_exchange_cm_unreachable_or_broken(x, fail, http, code):
    x.cm.response = fail
    status, body = await x.query(x.envelope())
    assert status == http and body["message"]["error"]["code"] == code
    assert x.registries.calls == []


async def test_exchange_without_cm_url_fails_closed(x):
    x.engine.settings.exchange_cm_url = ""
    status, body = await x.query(x.envelope())
    assert status == 503 and body["message"]["error"]["code"] == "consent_exchange_unavailable"
    assert x.registries.calls == []


def test_settings_default_exchange_and_reject_unknown_mode(monkeypatch):
    from openg2p_agri_composite.config import Settings
    from pydantic import ValidationError

    s = Settings()
    assert s.consent_mode == "exchange"
    assert s.consent_exchange_cm_url == "http://commons-services-cm-partner-api"
    monkeypatch.setenv("AGRI_COMPOSITE_CONSENT_MODE", "passthrough")
    monkeypatch.setenv("AGRI_COMPOSITE_CONSENT_EXCHANGE_CM_URL", CM_URL)
    s = Settings()
    assert s.consent_mode == "passthrough" and s.consent_exchange_cm_url == CM_URL
    with pytest.raises(ValidationError):
        Settings(consent_mode="bogus")



# ── a stored consent (message.consent_id) ───────────────────────────────────

def by_id_response(grants=None, receipts=None, decision="permit"):
    grants = {c: list(v) for c, v in SCOPES.items()} if grants is None else grants
    receipts = {c: RECEIPTS[c] for c in grants} if receipts is None else receipts
    return lambda body: httpx.Response(200, json={"decision": decision, "reason_code": "ok",
                                                  "grants": grants, "receipts": receipts})


async def test_consent_id_is_checked_at_the_exchange_cm_and_registries_get_receipts(x):
    x.cm.response = by_id_response()
    status, body = await x.query(x.envelope(consent=None, consent_id="c-123"))
    assert status == 200, body
    assert all(s["status"] == "ok" for s in body["message"]["sources"].values())
    _url, req = x.cm.calls[0]
    assert req["consent_id"] == "c-123" and req["consent_partner_id"] == "bank-a"
    assert req["partner_id"] == "agri-composite" and req["issue_receipts"] is True
    assert "consent_jws" not in req
    assert "use_case" not in req["request_context"]
    assert req["request_context"]["template"] == "loan-profile@1"
    assert req["request_context"]["purpose"] == "credit-assessment"
    assert req["request_context"]["requested_scopes"] == sorted(s for v in SCOPES.values() for s in v)
    assert [authorize(c)["consent_jws"] for c in x.registries.calls_to(FR_URL)] == ["fr.receipt.jws"]


async def test_consent_id_needs_exchange_mode(x):
    x.engine.settings.consent_mode = "passthrough"
    status, body = await x.query(x.envelope(consent=None, consent_id="c-123"))
    assert status == 400 and body["message"]["error"]["code"] == "consent_id_needs_exchange"


async def test_consent_id_and_consent_jws_together_are_refused(x):
    status, body = await x.query(x.envelope(consent_id="c-123"))
    assert status == 400 and body["message"]["error"]["code"] == "invalid_input"
    assert x.cm.calls == []


async def test_consent_id_without_a_grant_for_a_mandatory_registry(x):
    x.cm.response = by_id_response(grants={"crop-sown-registry": list(SCOPES["crop-sown-registry"])})
    status, body = await x.query(x.envelope(consent=None, consent_id="c-123"))
    assert status == 403 and body["message"]["error"]["code"] == "consent_grant_missing"
    assert x.registries.calls == []


async def test_consent_id_missing_a_required_scope(x):
    grants = {c: list(v) for c, v in SCOPES.items()}
    grants["farmer-registry"].remove("farmer-registry.farmer_identifiers")
    x.cm.response = by_id_response(grants=grants)
    status, body = await x.query(x.envelope(consent=None, consent_id="c-123"))
    assert status == 403 and body["message"]["error"]["code"] == "consent_scope_missing"


async def test_consent_id_partial_grant_for_an_optional_registry(x):
    grants = {c: list(v) for c, v in SCOPES.items()}
    grants["crop-sown-registry"] = ["crop-sown-registry.location"]
    x.cm.response = by_id_response(grants=grants)
    status, body = await x.query(x.envelope(consent=None, consent_id="c-123"))
    assert status == 200, body
    srcs = body["message"]["sources"]
    assert srcs["farmer"]["status"] == "ok"
    assert srcs["crop_seasons"]["status"] == "denied" and x.registries.calls_to(CSR_URL) == []


async def test_consent_id_denied_by_the_cm(x):
    x.cm.response = lambda body: httpx.Response(200, json={"decision": "deny", "reason_code": "revoked"})
    status, body = await x.query(x.envelope(consent=None, consent_id="c-123"))
    assert status == 403 and body["message"]["error"]["code"] == "consent_denied"
    assert "revoked" in body["message"]["error"]["message"]


# ── no consent sent: the exchange CM looks it up ─────────────────────────────

def lookup_response(consent_id="c-found", **kw):
    inner = by_id_response(**kw)

    def respond(body):
        r = inner(body)
        return httpx.Response(200, json={**r.json(), "consent_id": consent_id})
    return respond


async def test_no_consent_is_looked_up_at_the_exchange_cm(x):
    x.cm.response = lookup_response()
    env = x.envelope(consent=None)
    status, body = await x.query(env)
    assert status == 200, body
    _url, req = x.cm.calls[0]
    assert "consent_id" not in req and "consent_jws" not in req
    assert req["consent_partner_id"] == "bank-a" and req["partner_id"] == "agri-composite"
    assert req["issue_receipts"] is True
    ctx = req["request_context"]
    assert ctx["subject_id"] == env["message"]["subject"]
    assert "use_case" not in ctx and ctx["template"] == "loan-profile@1" and ctx["purpose"] == "credit-assessment"
    assert ctx["limits"] == {"allowed_signing_algs": ["EdDSA", "ES256"], "allow_partner_consents": False,
                             "allow_assisted_confirmation": False}
    assert ctx["requested_scopes"] == sorted(s for v in SCOPES.values() for s in v)
    assert [authorize(c)["consent_jws"] for c in x.registries.calls_to(FR_URL)] == ["fr.receipt.jws"]
    # the consent used is named in the (signed) response and the audit events
    assert body["message"]["consent_id"] == "c-found" and x.verify_composite_signature(body)
    by_kind = {e["type"].rsplit(".", 1)[-1]: e["data"]["context"] for e in x.audit_events}
    assert by_kind["request"]["consent_id"] == "c-found" and by_kind["response"]["consent_id"] == "c-found"


async def test_no_consent_found_is_denied(x):
    x.cm.response = lambda body: httpx.Response(
        200, json={"decision": "deny", "reason_code": "consent_not_found", "detail": "no active consent"})
    status, body = await x.query(x.envelope(consent=None))
    assert status == 403
    err = body["message"]["error"]
    assert err["code"] == "consent_denied" and "consent_not_found" in err["message"]
    assert "consent_id" not in body["message"]
    assert x.registries.calls == []


async def test_a_denied_consent_is_named_in_the_error(x):
    x.cm.response = lambda body: httpx.Response(
        200, json={"decision": "deny", "reason_code": "revoked", "consent_id": "c-old"})
    status, body = await x.query(x.envelope(consent=None))
    assert status == 403 and body["message"]["consent_id"] == "c-old"


async def test_no_consent_in_passthrough_mode_is_still_refused(x):
    x.engine.settings.consent_mode = "passthrough"
    status, body = await x.query(x.envelope(consent=None))
    assert status == 403 and body["message"]["error"]["code"] == "consent_required"
    assert x.cm.calls == []


async def test_consent_jws_names_the_consent_the_cm_used(x):
    x.cm.response = lambda body: httpx.Response(
        200, json={"decision": "permit", "receipts": dict(RECEIPTS), "consent_id": "c-jws"})
    status, body = await x.query(x.envelope())
    assert status == 200 and body["message"]["consent_id"] == "c-jws"


async def test_no_consent_id_in_the_response_when_the_cm_names_none(x):
    status, body = await x.query(x.envelope())
    assert status == 200 and "consent_id" not in body["message"]


# ── caller terms: limits from the partner's use-case policy ──────────────────

async def test_limits_come_from_the_partner_use_case_policy(x):
    from conftest import use_case_policy

    x.pm.policies["PARTNER_BANK_A"] = [use_case_policy(
        allowed_subject_id_types=["FAYDA_FAN"], max_validity_duration="P1Y", allow_partner_consents=True,
        allow_assisted_confirmation=True, allowed_signing_algs=["ES256"], allowed_purposes=["ignored"])]
    x.cm.response = lookup_response()
    status, body = await x.query(x.envelope(consent=None))
    assert status == 200, body
    assert x.cm.calls[0][1]["request_context"]["limits"] == {
        "allowed_subject_id_types": ["FAYDA_FAN"], "max_validity_duration": "P1Y",
        "allow_partner_consents": True, "allow_assisted_confirmation": True, "allowed_signing_algs": ["ES256"]}
    # the policy's purposes are no limit and no check (granting a use case grants its purpose)
    assert x.cm.calls[0][1]["request_context"]["purpose"] == "credit-assessment"


async def test_an_ungranted_partner_never_reaches_the_cm(x):
    x.pm.policies["PARTNER_BANK_A"] = []
    for consent in (None, "jws"):
        env = x.envelope() if consent else x.envelope(consent=None)
        status, body = await x.query(env)
        assert status == 403 and body["message"]["error"]["code"] == "partner_not_allowed"
    assert x.cm.calls == []


def _undeclare(x, source_id):
    compiled = x.registry.get("loan-profile@1")
    src = compiled.sources[source_id]
    src.scopes, src.optional_scopes = [], []


async def test_lookup_needs_every_source_to_name_its_scopes(x):
    # requested_scopes is required by the CM; a consent it holds is not seen here to take it from.
    _undeclare(x, "farmer")
    status, body = await x.query(x.envelope(consent=None))
    assert status == 500 and body["message"]["error"]["code"] == "use_case_misconfigured"
    assert x.cm.calls == []


async def test_consent_jws_a_source_without_scopes_asks_for_its_grant(x):
    _undeclare(x, "farmer")
    status, body = await x.query(x.envelope())
    assert status == 200, body
    requested = x.cm.calls[0][1]["request_context"]["requested_scopes"]
    assert set(SCOPES["farmer-registry"]) <= set(requested)  # what the consent grants farmer-registry


async def test_a_use_case_without_a_purpose_is_not_sent_to_the_cm(x):
    x.registry.get("loan-profile@1").spec.purpose = None
    status, body = await x.query(x.envelope(consent=None))
    assert status == 500 and body["message"]["error"]["code"] == "use_case_misconfigured"
    assert x.cm.calls == []
