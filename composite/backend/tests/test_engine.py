"""End-to-end query handling with stubbed PM, registries and audit manager."""

import copy
import json
import uuid
from datetime import datetime, timedelta, timezone

import httpx
import pytest
import yaml
from conftest import (
    AUDIT_URL, CROP_SEASONS, CSR_URL, FAN, FARMER_ID, FARMER_RECORD, FR_URL, PM_URL, SCOPES, PolicyStub, canonical,
    dci_response, use_case_policy,
)
from jwt import PyJWS
from openg2p_agri_composite.core.audit import AuditEmitter
from openg2p_agri_composite.core.crypto import build_composite_crypto
from openg2p_agri_composite.core.engine import CompositeEngine, EngineSettings
from openg2p_agri_composite.core.loader import UseCaseRegistry
from openg2p_agri_composite.core.policies import PartnerPolicyStore

REGISTRIES = {
    "farmer-registry": {"url": FR_URL, "partner_id": "", "receiver_id": ""},
    "crop-sown-registry": {"url": CSR_URL, "partner_id": "", "receiver_id": ""},
}


class Harness:
    def __init__(self, use_cases_dir, composite_p12, pm_transport, registries, partner_key):
        self.audit_events = []
        self.audit_fail = False
        self.registries = registries
        self.partner_key = partner_key
        self.composite_p12 = composite_p12
        self.pm = PolicyStub()  # PM policies: PARTNER_BANK_A is granted loan-profile@1

        def mux(request: httpx.Request):
            if self.pm.handles(request):
                return self.pm(request)
            if str(request.url).startswith(AUDIT_URL):
                if self.audit_fail:
                    raise httpx.ConnectError("audit down")
                self.audit_events.append(json.loads(request.content))
                return httpx.Response(202)
            return registries(request)

        self.client = httpx.AsyncClient(transport=httpx.MockTransport(mux))
        self.registry = UseCaseRegistry(str(use_cases_dir), known_controllers=REGISTRIES.keys())
        self.registry.load()
        assert not self.registry.errors(), self.registry.errors()
        self.crypto = build_composite_crypto(
            partner_mgmt_api_url=PM_URL,
            signing_p12_path=composite_p12["path"],
            signing_p12_password=composite_p12["password"],
            signing_kid="composite-test",
            signing_algorithm="auto",
            allowed_algorithms="EdDSA,ES256,RS256",
            pm_transport=pm_transport,
        )
        self.policies = PartnerPolicyStore(PM_URL, lambda: self.client)
        self.engine = CompositeEngine(
            EngineSettings(composite_partner_id="agri-composite", registries=REGISTRIES, consent_mode="passthrough",
                           default_overall_timeout_ms=3000),
            self.registry,
            self.crypto,
            lambda: self.client,
            audit=AuditEmitter(AUDIT_URL, lambda: self.client),
            policies=self.policies,
        )

    def consent(self, subject=None, controllers=("farmer-registry", "crop-sown-registry"), key=None, **extra):
        now = datetime.now(timezone.utc)
        claims = {
            "jti": str(uuid.uuid4()),
            "aud": "bank-a",
            "subject_id": subject or {"type": "FAYDA_FAN", "value": FAN},
            "purpose": {"code": "credit-assessment"},
            "grants": [{"data_controller": c, "data_scopes": list(SCOPES[c])} for c in controllers],
            "fetch_type": "oneshot",
            "validity": {"valid_from": (now - timedelta(hours=1)).isoformat(),
                         "valid_until": (now + timedelta(days=30)).isoformat()},
            "issued_at": now.isoformat(),
        }
        claims.update(extra)
        return (key or self.partner_key).sign_compact(claims)

    def envelope(self, subject=None, parameters=None, consent="default", sender="bank-a", key=None, ts=None,
                 consent_id=None):
        header = {
            "version": "1.0.0", "message_id": str(uuid.uuid4()),
            "message_ts": ts or datetime.now(timezone.utc).isoformat(),
            "action": "query", "sender_id": sender, "receiver_id": "agri-composite",
        }
        message = {"subject": subject or {"type": "FAYDA_FAN", "value": FAN}, "parameters": parameters or {}}
        if consent == "default":
            consent = self.consent(subject=message["subject"])
        if consent is not None:
            message["consent_jws"] = consent
        if consent_id is not None:
            message["consent_id"] = consent_id
        sig = (key or self.partner_key).sign_detached({"header": header, "message": message})
        return {"signature": sig, "header": header, "message": message}

    async def query(self, env, ref="loan-profile"):
        status, body = await self.engine.handle_query(ref, env)
        await self.engine.audit.drain()
        return status, body

    def verify_composite_signature(self, body):
        a, _b, c = body["signature"].split(".")
        payload = canonical({"header": body["header"], "message": body["message"]})
        import base64
        b = base64.urlsafe_b64encode(payload).decode().rstrip("=")
        PyJWS().decode(f"{a}.{b}.{c}", self.composite_p12["public_key"], algorithms=["ES256"])
        return True


@pytest.fixture
def h(use_cases_dir, composite_p12, pm_transport, registries, partner_key):
    return Harness(use_cases_dir, composite_p12, pm_transport, registries, partner_key)


def set_use_case(h, use_cases_dir, **changes):
    path = use_cases_dir / "loan-profile.yaml"
    raw = yaml.safe_load(path.read_text())
    for dotted, value in changes.items():
        node = raw
        parts = dotted.split(".")
        for p in parts[:-1]:
            node = node[p]
        node[parts[-1]] = value
    path.write_text(yaml.safe_dump(raw, sort_keys=False))
    h.registry.load()
    assert not h.registry.errors(), h.registry.errors()


async def test_happy_path_by_fan(h):
    env = h.envelope(parameters={"crop_year": 2019, "season": "SEASON_MEHER"})
    status, body = await h.query(env)
    assert status == 200, body
    msg = body["message"]
    assert msg["use_case"] == "loan-profile@1"
    assert msg["subject"] == {"type": "FAYDA_FAN", "value": FAN}
    assert msg["sources"] == {"farmer": {"status": "ok"}, "season_summaries": {"status": "ok"}, "crop_seasons": {"status": "ok"}}
    data = msg["data"]
    assert data["farmer"]["name"]["surname"] == "Bekele"
    assert data["farmer"]["sex"] == "female"
    assert {i["identifier_type"] for i in data["farmer"]["identifiers"]} == {"UIN", "FARMER_ID"}
    assert data["farmer"]["main_crops"] == ["CROP_BREAD_WHEAT", "CROP_TEFF"]
    assert data["farmer"]["location"]["name"] == "Kebele 04"
    assert data["land"]["total_size"] == 1.75 and data["land"]["parcel_count"] == 2
    assert len(data["crops"]["seasons"]) == 2
    assert data["crops"]["total_area_sown_ha"] == 1.15
    assert data["crops"]["season_summaries"][0]["measures"]["area_sown_ha"] == 1.15
    assert body["header"]["sender_id"] == "agri-composite" and body["header"]["receiver_id"] == "bank-a"
    assert body["header"]["meta"]["in_reply_to"] == env["header"]["message_id"]
    assert h.verify_composite_signature(body)

    # Farmer Registry first, by foundational_id; then the Crop Sown Registry by farmer ID.
    fr = h.registries.calls_to(FR_URL)
    assert len(fr) == 1
    crit = fr[0]["message"]["search_request"][0]["search_criteria"]
    assert crit["reg_type"] == "Farmer"
    assert crit["query"]["value"]["expression"]["query"] == {"foundational_id": {"$eq": FAN}}
    csr = h.registries.calls_to(CSR_URL)
    assert len(csr) == 2
    for call in csr:
        crit = call["message"]["search_request"][0]["search_criteria"]
        q = crit["query"]["value"]["expression"]["query"]
        assert q["subject_id"] == FARMER_ID and q["crop_year"] == 2019 and q["season"] == "SEASON_MEHER"
    agg = next(c for c in csr if c["message"]["search_request"][0]["search_criteria"]["reg_record_type"].endswith("ActivityAggregate"))
    assert agg["message"]["search_request"][0]["search_criteria"]["query"]["value"]["expression"]["query"]["aggregate_type"] == "FARMER_SEASON_SUMMARY"

    # Every call: composite as sender, partner on whose behalf, consent forwarded unchanged, signed.
    for _url, call in h.registries.calls:
        assert call["header"]["sender_id"] == "agri-composite"
        assert call["header"]["meta"]["on_behalf_of"] == "bank-a"
        assert call["message"]["search_request"][0]["search_criteria"]["authorize"]["consent_jws"] == env["message"]["consent_jws"]
        assert call["message"]["transaction_id"] == msg["request_id"]
        h.verify_composite_signature(call)

    kinds = [e["type"].rsplit(".", 1)[-1] for e in h.audit_events]
    assert kinds.count("request") == 1 and kinds.count("source_call") == 3 and kinds.count("response") == 1
    blob = json.dumps(h.audit_events)
    assert FAN not in blob and FARMER_ID not in blob and "Bekele" not in blob  # no data in audit


async def test_by_farmer_id_and_optional_parameters_omitted(h):
    subject = {"type": "FARMER_ID", "value": FARMER_ID}
    status, body = await h.query(h.envelope(subject=subject))
    assert status == 200, body
    fr = h.registries.calls_to(FR_URL)[0]["message"]["search_request"][0]["search_criteria"]
    assert fr["query"]["value"]["expression"]["query"] == {"functional_record_id": {"$eq": FARMER_ID}}
    for call in h.registries.calls_to(CSR_URL):
        q = call["message"]["search_request"][0]["search_criteria"]["query"]["value"]["expression"]["query"]
        assert q["subject_id"] == FARMER_ID and "crop_year" not in q and "season" not in q


async def test_bad_signature_rejected_without_calling_registries(h, partner_key):
    env = h.envelope()
    env["message"]["parameters"] = {"crop_year": 2020}  # tampered after signing
    status, body = await h.query(env)
    assert status == 401 and body["message"]["error"]["code"] == "signature_invalid"
    assert body["header"]["status"] == "rjct"
    assert h.registries.calls == []
    assert h.verify_composite_signature(body)


async def test_unknown_partner_fails_closed(h):
    from conftest import Key
    stranger = Key("x")
    status, body = await h.query(h.envelope(sender="bank-z", key=stranger, consent=None))
    assert status == 401 and h.registries.calls == []


async def test_partner_not_allowed(h, use_cases_dir, pm_keys, partner_key):
    # bank-b's key is in PM, but PM has no policy for it and this use case (404): fail closed.
    pm_keys["PARTNER_BANK_B"] = pm_keys["PARTNER_BANK_A"]
    status, body = await h.query(h.envelope(sender="bank-b"))
    assert status == 403 and body["message"]["error"]["code"] == "partner_not_allowed"
    assert "Partner Management" in body["message"]["error"]["message"]
    assert h.pm.calls[-1] == f"{PM_URL}/policies/PARTNER_BANK_B?use_case=loan-profile%401"
    assert h.registries.calls == []
    assert h.verify_composite_signature(body)


async def test_partner_policy_without_the_use_case(h):
    # Policies for other use cases (or another major) do not grant this one.
    h.pm.policies["PARTNER_BANK_A"] = [use_case_policy(use_case="loan-profile@2"), use_case_policy(use_case="other@1")]
    status, body = await h.query(h.envelope())
    assert status == 403 and body["message"]["error"]["code"] == "partner_not_allowed"
    assert "loan-profile@1" in body["message"]["error"]["message"]


async def test_partner_scope_policy_is_not_a_use_case_grant(h):
    # Only a use-case policy counts; a registry scope policy does not.
    h.pm.policies["PARTNER_BANK_A"] = [use_case_policy(data_controller="farmer-registry", use_case=None,
                                                       allowed_data_scopes=["farmer-registry.land"])]
    status, body = await h.query(h.envelope())
    assert status == 403 and body["message"]["error"]["code"] == "partner_not_allowed"


async def test_partner_grant_is_cached(h):
    for _ in range(3):
        status, _ = await h.query(h.envelope())
        assert status == 200
    assert len(h.pm.calls) == 1


async def test_pm_outage_with_nothing_cached_is_503(h):
    h.pm.down = True
    status, body = await h.query(h.envelope())
    assert status == 503 and body["message"]["error"]["code"] == "partner_policy_unavailable"
    assert h.registries.calls == []
    assert h.verify_composite_signature(body)


async def test_no_policy_source_fails_closed(h):
    h.engine.policies = None
    status, body = await h.query(h.envelope())
    assert status == 503 and body["message"]["error"]["code"] == "partner_policy_unavailable"


async def test_input_validation(h):
    status, body = await h.query(h.envelope(parameters={"season": "SUMMER"}))
    assert status == 400 and "season" in body["message"]["error"]["message"]
    status, body = await h.query(h.envelope(parameters={"crop_year": "2019"}))
    assert status == 400
    status, body = await h.query(h.envelope(parameters={"colour": "red"}))
    assert status == 400 and "unknown parameters" in body["message"]["error"]["message"]
    subject = {"type": "NATIONAL_ID", "value": "1"}
    status, body = await h.query(h.envelope(subject=subject, consent=h.consent(subject=subject)))
    assert status == 400 and "subject type" in body["message"]["error"]["message"]
    assert h.registries.calls == []


async def test_stale_request_rejected(h):
    old = (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat()
    status, body = await h.query(h.envelope(ts=old))
    assert status == 400 and body["message"]["error"]["code"] == "stale_request"


async def test_unknown_use_case_and_major(h):
    status, _ = await h.query(h.envelope(), ref="nope")
    assert status == 404
    status, _ = await h.query(h.envelope(), ref="loan-profile@2")
    assert status == 404
    status, body = await h.query(h.envelope(), ref="loan-profile@1")
    assert status == 200, body


async def test_consent_subject_mismatch(h):
    consent = h.consent(subject={"type": "FAYDA_FAN", "value": "999999999999"})
    status, body = await h.query(h.envelope(consent=consent))
    assert status == 403 and body["message"]["error"]["code"] == "consent_subject_mismatch"
    assert h.registries.calls == []


async def test_consent_missing_or_unsigned(h):
    status, body = await h.query(h.envelope(consent=None))
    assert status == 403 and body["message"]["error"]["code"] == "consent_required"
    from conftest import Key
    forged = h.consent(key=Key("bank-a-key-1"))  # same kid, wrong key
    status, body = await h.query(h.envelope(consent=forged))
    assert status == 403 and body["message"]["error"]["code"] == "consent_signature_invalid"
    assert h.registries.calls == []


async def test_consent_without_grant_for_optional_source(h):
    consent = h.consent(controllers=("farmer-registry",))
    status, body = await h.query(h.envelope(consent=consent))
    assert status == 200, body
    srcs = body["message"]["sources"]
    assert srcs["farmer"]["status"] == "ok"
    assert srcs["season_summaries"]["status"] == "denied" and srcs["crop_seasons"]["status"] == "denied"
    assert h.registries.calls_to(CSR_URL) == []
    # fields from a denied source are null, not "nothing found"
    crops = body["message"]["data"]["crops"]
    assert crops == {"seasons": None, "season_summaries": None, "total_area_sown_ha": None}
    assert body["message"]["data"]["land"]["parcel_count"] is not None


async def test_consent_without_grant_for_mandatory_source(h):
    consent = h.consent(controllers=("crop-sown-registry",))
    status, body = await h.query(h.envelope(consent=consent))
    assert status == 403 and body["message"]["error"]["code"] == "consent_grant_missing"
    assert h.registries.calls == []


async def test_legacy_single_controller_consent(h):
    consent = h.consent(controllers=(), grants=None, data_controller="farmer-registry",
                        data_scopes=list(SCOPES["farmer-registry"]))
    status, body = await h.query(h.envelope(consent=consent))
    assert status == 200, body
    assert body["message"]["sources"]["crop_seasons"]["status"] == "denied"


async def test_consent_missing_a_required_scope_of_a_mandatory_source_fails(h):
    grants = [{"data_controller": "farmer-registry", "data_scopes": ["farmer-registry.personal_details"]},
              {"data_controller": "crop-sown-registry", "data_scopes": list(SCOPES["crop-sown-registry"])}]
    status, body = await h.query(h.envelope(consent=h.consent(grants=grants)))
    assert status == 403 and body["message"]["error"]["code"] == "consent_scope_missing"
    assert "farmer-registry.farmer_identifiers" in body["message"]["error"]["message"]
    assert h.registries.calls == []


async def test_consent_missing_a_required_scope_of_an_optional_source_skips_it(h):
    grants = [{"data_controller": "farmer-registry", "data_scopes": list(SCOPES["farmer-registry"])},
              {"data_controller": "crop-sown-registry", "data_scopes": ["crop-sown-registry.location"]}]
    status, body = await h.query(h.envelope(consent=h.consent(grants=grants)))
    assert status == 200, body
    srcs = body["message"]["sources"]
    assert srcs["farmer"]["status"] == "ok"
    for sid in ("season_summaries", "crop_seasons"):
        assert srcs[sid]["status"] == "denied" and "crop-sown-registry.farmer_reference" in srcs[sid]["detail"]
    assert h.registries.calls_to(CSR_URL) == []
    assert body["message"]["data"]["crops"]["seasons"] is None


async def test_optional_scopes_may_be_left_out(h):
    required = {"farmer-registry": ["farmer_identifiers", "personal_details", "land", "main_crops"],
                "crop-sown-registry": ["farmer_reference", "crop_season", "measures"]}
    grants = [{"data_controller": c, "data_scopes": [f"{c}.{n}" for n in names]} for c, names in required.items()]
    status, body = await h.query(h.envelope(consent=h.consent(grants=grants)))
    assert status == 200, body
    assert all(s["status"] == "ok" for s in body["message"]["sources"].values()), body["message"]["sources"]


async def test_optional_source_unavailable_gives_partial_response(h):
    h.registries.handlers[(CSR_URL, "spdci-extensions-agri:CropSeason")] = lambda body: httpx.Response(503)
    status, body = await h.query(h.envelope())
    assert status == 200, body
    srcs = body["message"]["sources"]
    assert srcs["crop_seasons"]["status"] == "unavailable"
    assert srcs["season_summaries"]["status"] == "ok"
    # retries: 0 → one attempt (a slow registry search is not retried)
    seasons_calls = [c for c in h.registries.calls_to(CSR_URL)
                     if c["message"]["search_request"][0]["search_criteria"]["reg_record_type"].endswith("CropSeason")]
    assert len(seasons_calls) == 1
    crops = body["message"]["data"]["crops"]
    # built from the unavailable source: null; season summaries answered, so they stay
    assert crops["seasons"] is None and crops["total_area_sown_ha"] is None
    assert crops["season_summaries"] is not None


async def test_no_record_source_gives_empty_values_not_null(h):
    h.registries.handlers[(CSR_URL, "spdci-extensions-agri:CropSeason")] = (
        lambda body: httpx.Response(200, json=dci_response(body, records=[])))
    status, body = await h.query(h.envelope())
    assert status == 200, body
    assert body["message"]["sources"]["crop_seasons"]["status"] == "no_record"
    crops = body["message"]["data"]["crops"]
    assert crops["seasons"] == [] and crops["total_area_sown_ha"] == 0


async def test_partial_response_denied_fails_request(h, use_cases_dir):
    set_use_case(h, use_cases_dir, **{"execution.partial_response": "denied"})
    h.registries.handlers[(CSR_URL, "spdci-extensions-agri:CropSeason")] = lambda body: httpx.Response(503)
    status, body = await h.query(h.envelope())
    assert status == 503 and body["message"]["error"]["code"] == "source_unavailable"
    assert "data" not in body["message"] and body["message"]["sources"]["crop_seasons"]["status"] == "unavailable"


async def test_mandatory_source_timeout_fails_request(h, use_cases_dir):
    def handler(body):
        raise httpx.ReadTimeout("slow")

    h.registries.handlers[(FR_URL, None)] = handler
    status, body = await h.query(h.envelope())
    assert status == 504 and body["message"]["error"]["code"] == "source_unavailable"
    assert body["message"]["sources"]["farmer"]["status"] == "unavailable"
    assert body["message"]["sources"]["crop_seasons"]["detail"].startswith("not called")
    assert h.registries.calls_to(CSR_URL) == []


async def test_registry_consent_denial_is_denied(h):
    h.registries.handlers[(CSR_URL, "spdci-extensions-agri:ActivityAggregate")] = lambda body: httpx.Response(
        200, json=dci_response(body, status="rjct", reason="Consent denied for reference_id 'x': controller_not_granted"))
    h.registries.handlers[(CSR_URL, "spdci-extensions-agri:CropSeason")] = lambda body: httpx.Response(
        200, json=dci_response(body, status="rjct", reason="unsupported operator"))
    status, body = await h.query(h.envelope())
    assert status == 200
    assert body["message"]["sources"]["season_summaries"]["status"] == "denied"
    assert body["message"]["sources"]["crop_seasons"]["status"] == "error"


async def test_mandatory_denied_by_registry_fails(h):
    h.registries.handlers[(FR_URL, None)] = lambda body: httpx.Response(
        200, json=dci_response(body, status="rjct", reason="The consent's subject is not the person searched"))
    status, body = await h.query(h.envelope())
    assert status == 403 and body["message"]["error"]["code"] == "source_denied"


async def test_no_farmer_record(h):
    subject = {"type": "FAYDA_FAN", "value": "000000000000"}
    status, body = await h.query(h.envelope(subject=subject, consent=h.consent(subject=subject)))
    assert status == 200
    srcs = body["message"]["sources"]
    assert srcs["farmer"]["status"] == "no_record"
    assert srcs["crop_seasons"]["status"] == "no_record" and "not called" in srcs["crop_seasons"]["detail"]
    assert body["message"]["data"]["farmer"]["name"] is None
    assert body["message"]["data"]["land"]["total_size"] == 0


async def test_rate_limit(h, use_cases_dir):
    set_use_case(h, use_cases_dir, **{"limits.rate_per_partner": "2/min"})
    assert (await h.query(h.envelope()))[0] == 200
    assert (await h.query(h.envelope()))[0] == 200
    status, body = await h.query(h.envelope())
    assert status == 429 and body["message"]["error"]["code"] == "rate_limited"


async def test_audit_failure_never_breaks_request(h):
    h.audit_fail = True
    status, body = await h.query(h.envelope())
    assert status == 200 and h.audit_events == []


async def test_registry_response_signature_checked_when_configured(h):
    h.engine.settings.registries = {**REGISTRIES, "crop-sown-registry": {**REGISTRIES["crop-sown-registry"], "partner_id": "csr"}}
    status, body = await h.query(h.envelope())
    assert status == 200
    assert body["message"]["sources"]["crop_seasons"] == {"status": "error", "detail": "registry response signature does not verify"}


async def test_cannot_sign_returns_500(h):
    h.crypto.helper._signing_key_path = "/nonexistent.p12"
    h.crypto.helper._signing_key = None
    status, body = await h.query(h.envelope())
    assert status == 500 and body["message"]["error"]["code"] == "signing_unavailable"


async def test_crop_sources_not_called_without_a_farmer_id(h):
    record = copy.deepcopy(FARMER_RECORD)
    record["farmer_personal_details"]["member_identifier"] = [
        i for i in record["farmer_personal_details"]["member_identifier"] if i.get("identifier_type") != "FARMER_ID"]
    h.registries.handlers[(FR_URL, None)] = lambda body: httpx.Response(200, json=dci_response(body, [record]))
    status, body = await h.query(h.envelope())
    assert status == 200, body
    srcs = body["message"]["sources"]
    for sid in ("season_summaries", "crop_seasons"):
        assert srcs[sid]["status"] == "error" and "no subject_id" in srcs[sid]["detail"]
    assert h.registries.calls_to(CSR_URL) == []


async def test_a_full_page_is_flagged(h, use_cases_dir):
    crop = h.registry.get("loan-profile").spec.sources[2]
    assert crop.id == "crop_seasons"
    h.registries.handlers[(CSR_URL, "spdci-extensions-agri:CropSeason")] = (
        lambda body: httpx.Response(200, json=dci_response(body, [dict(CROP_SEASONS[0], i=n) for n in range(100)])))
    status, body = await h.query(h.envelope())
    assert status == 200
    assert "first 100 records only" in body["message"]["sources"]["crop_seasons"]["detail"]
    assert "detail" not in body["message"]["sources"]["farmer"]  # a one-record lookup is never flagged
