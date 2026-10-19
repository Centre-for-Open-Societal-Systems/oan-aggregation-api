"""End-to-end: Aggregation Layer <-> Consent Manager over the CM's generic APIs only.

The CM carries no aggregation code. The aggregation layer keeps nothing and
uses two CM APIs: POST /consent/v1/validate and
GET /consent/v1/consents/{id}/status. The subject's consent is obtained
before the aggregation layer is called (consent request + one-time code, in
the CM). The partner sends a Beneficiary-360 request and receives a
Beneficiary-360 response inside a signed DCI on-search, checked against the
published schemas (test/fixtures/bene360).

  0  the CM has no aggregation API; the AL rejects malformed / foreign queries
  A  sync (default): consent held -> validate -> registry (legitimate_interest
     binding, searched by foundationalId) -> the on-search is the response;
     the CM records no grant for the aggregator
  B  async: consent held -> 202 ack -> the same on-search on the callback
  C  never asked: 403 no_subject_consent, no registry called -> the partner
     raises a consent request in the CM, the subject confirms it with the
     one-time code and grants one of two scopes -> a new consent object ->
     only that scope is released
  D  async, consent withdrawn while the registry is answering -> the callback
     is a rejected on-search (consent_withdrawn), no data

Steps 1-2 of C use the CM's subject routes as they are today (raise, issue
the OTP, verify, approve); the partner-facing "raise sends the OTP" and
"validate OTP = approve" are not in the CM yet (docs/CM-API-CONTRACT.md).

Started by run.sh, which brings up Postgres (for the CM only), the CM, the
Aggregation Layer (no database; catalog test/e2e/registries.yaml) and
fakes.py first.
"""
import json
import os
import pathlib
import sys
import time
import uuid
from datetime import datetime, timedelta, timezone

import httpx
import jwt
from jsonschema import Draft202012Validator, FormatChecker

CM = os.environ.get("E2E_CM", "http://127.0.0.1:18000")
AGG = os.environ.get("E2E_AGG", "http://127.0.0.1:18100")
FAKES = os.environ.get("E2E_FAKES", "http://127.0.0.1:18090")
KEYS = pathlib.Path(os.environ["E2E_KEYS"])
SCHEMAS = pathlib.Path(__file__).resolve().parents[1] / "fixtures" / "bene360"

# auth is off on the CM, so every subject call is the dev subject - and the
# beneficiary is that subject, searched by the same foundational ID.
SUBJECT = {"type": "national_id", "value": "dev"}
PURPOSE = {"code": "loan_origination"}
PERSONAL = "FARMER_REGISTRY.farmer_personal_details"
FAMILY = "FARMER_REGISTRY.family_details"
SCOPES = [PERSONAL, FAMILY]
CONTEXT = "https://schemas.openg2p.org/beneficiary360/v1/context.jsonld"

cm = httpx.Client(base_url=CM, timeout=20)
agg = httpx.Client(base_url=AGG, timeout=30)
response_schema = Draft202012Validator(
    json.loads((SCHEMAS / "response.schema.json").read_text(encoding="utf-8")),
    format_checker=FormatChecker())
results = []


def check(name, cond, extra=""):
    results.append((name, bool(cond)))
    print("  %-62s %s %s" % (name, "PASS" if cond else "FAIL", extra))


def ok(response):
    if response.status_code >= 400:
        raise SystemExit("%s %s -> %s %s" % (response.request.method, response.request.url,
                                             response.status_code, response.text))
    return response.json()


def partner_jws(ref, audience, scopes):
    now = datetime.now(timezone.utc)
    claims = {
        "@context": "https://openg2p.org/contexts/consent_object.jsonld",
        "@type": "ConsentObject", "jti": uuid.uuid4().hex, "iss": audience,
        "aud": audience, "data_controller": "agg",
        "subject_id": SUBJECT, "purpose": PURPOSE, "data_scopes": scopes,
        "fetch_type": "oneshot",
        "validity": {"valid_from": now.isoformat(),
                     "valid_until": (now + timedelta(days=30)).isoformat()},
        "issued_at": now.isoformat(),
    }
    key = (KEYS / ("%s.pem" % ref)).read_text()
    return jwt.encode(claims, key, algorithm="EdDSA", headers={"kid": "k1"})


def binding(name, audience, controller, pm_id, scopes, otp, basis="consent"):
    partner = ok(cm.post("/consent/v1/partners", json={
        "name": name, "audience": audience, "controller_id": controller,
        "partner_mgmt_id": pm_id}))
    ok(cm.put("/consent/v1/partners/%s/policy" % partner["id"], json={
        "allowed_data_scopes": scopes, "allowed_purposes": ["loan_origination"],
        "allowed_signing_algs": ["EdDSA"], "required_auth_method": "otp" if otp else None,
        "lawful_basis": basis}))
    return partner


def consent_in_cm(partner_id, granted):
    """The consent, before the aggregation layer is called.

    The partner raises the request; the subject gets the one-time code and
    confirms it; the scopes are granted. Done with the CM's routes as they are
    today (the code is read back with the CM's otp_debug).
    """
    cr = ok(cm.post("/consent/v1/consent-requests", json={
        "subject_id": SUBJECT, "partner_id": partner_id, "purpose": PURPOSE,
        "requested_scopes": SCOPES}))
    ok(cm.post("/consent/v1/consent-requests/%s/otp" % cr["id"]))
    code = ok(cm.get("/consent/v1/consent-requests/%s/otp" % cr["id"]))["otp"]
    ok(cm.post("/consent/v1/consent-requests/%s/verify-otp" % cr["id"], json={"otp": code}))
    return ok(cm.post("/consent/v1/consent-requests/%s/approve" % cr["id"],
                      json={"granted_scopes": granted}))


def envelope(ref, audience, scopes, callback=True, **query):
    body = {"@context": CONTEXT, "foundationalId": SUBJECT["value"],
            "timeframe": "Timeframe-Medium", "correlationId": "e2e-" + uuid.uuid4().hex[:6]}
    body.update(query)
    header = {"sender_id": audience, "receiver_id": "aggregation-layer"}
    if callback:
        header["sender_uri"] = FAKES + "/callback"
    return {
        "header": header,
        "message": {"transaction_id": uuid.uuid4().hex[:12], "search_request": [{
            "reference_id": "ref-" + uuid.uuid4().hex[:6],
            "search_criteria": {
                "query_type": "beneficiary360", "query": body, "purpose": PURPOSE,
                "authorize": {"consent_jws": partner_jws(ref, audience, scopes)}}}]}}


def search(ref, audience, scopes, **query):
    return agg.post("/dci/registry/sync/search", json=envelope(ref, audience, scopes, **query))


def search_async(ref, audience, scopes, callback=True, **query):
    return agg.post("/dci/registry/async/search",
                    json=envelope(ref, audience, scopes, callback=callback, **query))


def wait_callback(correlation_id, seconds=20):
    end = time.time() + seconds
    while time.time() < end:
        for body in httpx.get(FAKES + "/received").json():
            if body["message"]["correlation_id"] == correlation_id:
                return body
        time.sleep(0.5)
    return None


def record_of(body):
    """The bene-360 response carried as reg_records[0] of the on-search."""
    records = ((body or {}).get("message", {}).get("search_response", [{}])[0]
               .get("data", {}).get("reg_records") or [{}])
    return records[0]


def farmer_register(record):
    for registry in record.get("registries") or []:
        if registry["registryCode"] == "FARMER_REGISTRY":
            return registry["registers"][0]
    return {}


def schema_errors(record):
    return ["%s: %s" % (list(e.path), e.message)
            for e in response_schema.iter_errors(record)]


def registry_searches():
    return len(httpx.get(FAKES + "/searches").json())


def main():
    print("setup: CM bindings, the subject's consent to partner X")
    px = binding("Partner X", "partner-x", "agg", "PARTNER_X", SCOPES, otp=True)
    py = binding("Partner Y", "partner-y", "agg", "PARTNER_Y", SCOPES, otp=True)
    # The aggregator's registry binding: legitimate_interest, so the CM caps
    # the hop at this ceiling and seeks no subject grant on it. Its scopes are
    # the registry's own block names.
    binding("Aggregation Layer -> farmer", "aggregation-layer-farmer", "farmer-registry",
            "PARTNER_AGGREGATION_LAYER", ["farmer_personal_details", "family_details"],
            otp=False, basis="legitimate_interest")
    consent_x = consent_in_cm(px["id"], SCOPES)

    print("\n0  contract and guard rails")
    paths = ok(cm.get("/openapi.json"))["paths"]
    agg_paths = [p for p in paths if "aggregat" in p or "grants" in p
                 or "by-audience" in p or "granted-scopes" in p or "async" in p]
    check("no aggregation / grants / by-audience route on the CM", not agg_paths, agg_paths)
    al_paths = list(ok(agg.get("/openapi.json"))["paths"])
    check("no request / OTP / queue route on the AL",
          not [p for p in al_paths if "/requests" in p or "otp" in p or "/queue" in p],
          al_paths)
    catalog = ok(agg.get("/aggregation/v1/registries"))["registries"]
    check("discovery lists the catalog's scope ids, no URLs",
          [s["scopeId"] for s in catalog[0]["scopes"]] == SCOPES
          and "127.0.0.1" not in json.dumps(catalog))
    # openg2p-fastapi-common answers a request validation error with 400.
    bad = search("PARTNER_X", "partner-x", SCOPES, fields=["x"])
    check("query with a property bene-360 forbids -> 400",
          bad.status_code == 400 and "Extra inputs are not permitted" in bad.text,
          bad.text[:80])
    other = search("PARTNER_X", "partner-x", SCOPES, foundationalId="someone-else")
    check("foundationalId other than the consent subject -> 403",
          other.status_code == 403 and other.json().get("error") == "subject_mismatch",
          other.text[:80])
    none = search("PARTNER_X", "partner-x", SCOPES, sections=["PROGRAMS"])
    check("only unsupported sections -> 422 section_not_supported",
          none.status_code == 422 and none.json().get("error") == "section_not_supported",
          none.text[:80])
    unknown = search("PARTNER_X", "partner-x", SCOPES, registryFilter=["NO_SUCH_REGISTRY"])
    check("registryFilter naming no catalog registry -> 422 no_registry",
          unknown.status_code == 422 and unknown.json().get("error") == "no_registry",
          unknown.text[:80])
    nocb = search_async("PARTNER_X", "partner-x", SCOPES, callback=False)
    check("async without sender_uri -> 400 no_callback",
          nocb.status_code == 400 and nocb.json().get("error") == "no_callback",
          nocb.text[:80])

    print("\nA  sync: consent held -> registry -> on-search in the response")
    response = search("PARTNER_X", "partner-x", SCOPES,
                      registryFilter=["FARMER_REGISTRY", "WORKER_REGISTRY"])
    check("200", response.status_code == 200, response.text[:120])
    body = response.json() if response.status_code == 200 else {}
    record = record_of(body)
    farmer = farmer_register(record)
    check("signed on-search", body.get("header", {}).get("action") == "on-search"
          and bool(body.get("signature")))
    check("on-search carries a bene-360 response",
          body and body["message"]["search_response"][0]["data"]["reg_type"] == "beneficiary360"
          and record.get("@type") == "Beneficiary360Response")
    check("response validates against response.schema.json", not schema_errors(record),
          schema_errors(record)[:3])
    searched = httpx.get(FAKES + "/searches").json()
    check("registry searched by foundationalId with the catalog id_type",
          searched and searched[-1]["query"]["value"] == {"id_type": "UIN",
                                                         "id_value": "dev"})
    check("registry permitted the legitimate_interest hop",
          record.get("meta", {}).get("sourceSystemsQueried") == ["FARMER_REGISTRY"]
          and bool(farmer))
    check("only the allowed fields: Mary Bell, no phone, no second name",
          farmer.get("attributes") == {"farmer_personal_details": {
              "demographic_info": {"name": {"given_name": "Mary", "surname": "Bell"}}}},
          farmer.get("attributes"))
    check("family block mapped as a HOUSEHOLD table, filtered",
          [(t["tableMnemonic"], t["attributes"]) for t in farmer.get("tables", [])]
          == [("HOUSEHOLD", {"group_size": 5})], farmer.get("tables"))
    check("unknown registry in registryFilter reported in meta.warnings",
          {"system": "WORKER_REGISTRY", "code": "REGISTRY_NOT_CONFIGURED"} in [
              {k: w[k] for k in ("system", "code")}
              for w in record.get("meta", {}).get("warnings", [])])
    check("correlationId echoed",
          record.get("meta", {}).get("correlationId", "").startswith("e2e-"))
    check("header.meta: lawful_basis consent, enforcement enabled",
          body and body["header"]["meta"] == {"consent_enforcement": "enabled",
                                              "lawful_basis": "consent"},
          body and body["header"].get("meta"))
    every = ok(cm.get("/consent/v1/my/consents", params={"view": "all", "size": 100}))
    every = every.get("items", every)
    check("CM holds no originated grant for the aggregator",
          not [r for r in every if r.get("source") == "originated"
               and r.get("partner_id") not in (px["id"], py["id"])])

    print("\nB  async: consent held -> 202 -> on-search on the callback")
    ack = search_async("PARTNER_X", "partner-x", SCOPES)
    check("202", ack.status_code == 202, ack.text[:120])
    ack = ack.json() if ack.status_code == 202 else {}
    check("ack: pdng, scopes, registries, callback",
          ack.get("status") == "pdng" and ack.get("accepted_scopes") == SCOPES
          and ack.get("registries") == ["FARMER_REGISTRY"]
          and ack.get("callback_url") == FAKES + "/callback", ack)
    body = wait_callback(ack.get("correlation_id"))
    record = record_of(body)
    check("callback received", body is not None)
    check("callback is a signed on-search, succ",
          body and body["header"]["action"] == "on-search"
          and body["header"]["status"] == "succ" and bool(body.get("signature")))
    check("bene-360 response, schema-valid",
          record.get("@type") == "Beneficiary360Response" and not schema_errors(record),
          schema_errors(record)[:3])

    print("\nC  never asked: 403 -> consent in the CM (one scope) -> search again")
    before = registry_searches()
    refused = search("PARTNER_Y", "partner-y", SCOPES)
    check("403 no_subject_consent",
          refused.status_code == 403 and refused.json().get("error") == "no_subject_consent",
          refused.text[:80])
    check("detail tells the partner to raise a consent request",
          "consent-requests" in refused.json().get("detail", ""))
    check("no registry called", registry_searches() == before)
    consent_in_cm(py["id"], [PERSONAL])   # grants one of two
    response = search("PARTNER_Y", "partner-y", SCOPES)
    body = response.json() if response.status_code == 200 else {}
    farmer = farmer_register(record_of(body))
    check("new consent object -> 200", response.status_code == 200, response.text[:120])
    check("only the granted scope delivered",
          list(farmer.get("attributes") or {}) == ["farmer_personal_details"]
          and farmer.get("tables") == [], farmer)
    check("response validates against response.schema.json",
          not schema_errors(record_of(body)))

    print("\nD  async, consent withdrawn before the callback -> rejected on-search")
    ok(httpx.post(FAKES + "/delay", json={"seconds": 3}))
    ack = search_async("PARTNER_X", "partner-x", SCOPES)
    check("202", ack.status_code == 202, ack.text[:120])
    ok(cm.post("/consent/v1/my/consents/%s/revoke" % consent_x["id"], json={}))
    body = wait_callback(ack.json().get("correlation_id"))
    ok(httpx.post(FAKES + "/delay", json={"seconds": 0}))
    check("callback is a rjct on-search, consent_withdrawn",
          body and body["header"]["status"] == "rjct"
          and body["header"].get("status_reason_code") == "consent_withdrawn",
          body and body["header"])
    check("no data in it",
          body and "data" not in body["message"]["search_response"][0])
    after = search("PARTNER_X", "partner-x", SCOPES)
    check("a new search after the withdrawal -> 403", after.status_code == 403,
          after.text[:80])

    failed = [n for n, good in results if not good]
    print("\n%d/%d passed" % (len(results) - len(failed), len(results)))
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
