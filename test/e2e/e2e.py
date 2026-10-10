"""End-to-end: Aggregation Layer <-> Consent Manager over the CM's generic APIs only.

The CM carries no aggregation code. The aggregation layer uses
POST /consent/v1/validate, GET /consent/v1/partners(/{id}/policy),
POST + GET /consent/v1/consent-requests(/{id}) and
GET /consent/v1/consents/{id}/status, and keeps its own grants. The partner
sends a Beneficiary-360 request and receives a Beneficiary-360 response, both
checked against the published schemas (test/fixtures/bene360).

  0  the CM has no aggregation API; the AL rejects malformed / foreign queries
  A  partner already holds consent, policy demands an OTP
     seek -> validate -> OTP -> own grant -> registry (legitimate_interest
     binding, searched by foundationalId) -> bene-360 callback; the CM records
     no grant for the aggregator
  B  partner never asked -> consent raised -> approved on the CM (one of two
     scopes) -> poller reads the request -> re-validate -> only that scope
  C  consent withdrawn while an aggregation waits for its OTP
     -> poller reads the consent status -> aggregation rejected
  D  consent request denied on the CM -> aggregation rejected
  E  approval after the CM's replay window -> rejected, partner must re-seek

Started by run.sh, which brings up Postgres, the CM (replay window shortened
so E is quick), the Aggregation Layer (poll every second, catalog
test/e2e/registries.yaml) and fakes.py first.
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
# Must match consent_manager_replay_freshness_window_sec in run.sh.
REPLAY_WINDOW = int(os.environ.get("E2E_REPLAY_WINDOW", "8"))
SCHEMAS = pathlib.Path(__file__).resolve().parents[1] / "fixtures" / "bene360"

# auth is off on both services, so every caller is the dev subject - and the
# beneficiary is that subject, searched by the same foundational ID.
SUBJECT = {"type": "national_id", "value": "dev"}
PURPOSE = {"code": "loan_origination"}
PERSONAL = "FARMER_REGISTRY.farmer_personal_details"
FAMILY = "FARMER_REGISTRY.family_details"
SCOPES = [PERSONAL, FAMILY]
CONTEXT = "https://schemas.openg2p.org/beneficiary360/v1/context.jsonld"

cm = httpx.Client(base_url=CM, timeout=20)
agg = httpx.Client(base_url=AGG, timeout=20)
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


def approve_on_cm(request_id, granted):
    """What the subject does on the CM consent screen."""
    ok(cm.post("/consent/v1/consent-requests/%s/otp" % request_id))
    code = ok(cm.get("/consent/v1/consent-requests/%s/otp" % request_id))["otp"]
    ok(cm.post("/consent/v1/consent-requests/%s/verify-otp" % request_id, json={"otp": code}))
    return ok(cm.post("/consent/v1/consent-requests/%s/approve" % request_id,
                      json={"granted_scopes": granted}))


def seek(ref, audience, scopes, **query):
    body = {"@context": CONTEXT, "foundationalId": SUBJECT["value"],
            "timeframe": "Timeframe-Medium", "correlationId": "e2e-" + uuid.uuid4().hex[:6]}
    body.update(query)
    return agg.post("/dci/registry/async/search", json={
        "header": {"sender_id": audience, "receiver_id": "aggregation-layer",
                   "sender_uri": FAKES + "/callback"},
        "message": {"transaction_id": uuid.uuid4().hex[:12], "search_request": [{
            "reference_id": "ref-" + uuid.uuid4().hex[:6],
            "search_criteria": {
                "query_type": "beneficiary360", "query": body, "purpose": PURPOSE,
                "authorize": {"consent_jws": partner_jws(ref, audience, scopes)}}}]}})


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


def wait_status(aggregation_id, want, seconds=15):
    end = time.time() + seconds
    row = {}
    while time.time() < end:
        row = agg.get("/aggregation/v1/requests/%s" % aggregation_id).json()
        if row.get("status") == want:
            return row
        time.sleep(0.5)
    return row


def main():
    print("setup: CM bindings")
    px = binding("Partner X", "partner-x", "agg", "PARTNER_X", SCOPES, otp=True)
    binding("Partner Y", "partner-y", "agg", "PARTNER_Y", SCOPES, otp=True)
    binding("Partner Z", "partner-z", "agg", "PARTNER_Y", SCOPES, otp=True)
    binding("Partner W", "partner-w", "agg", "PARTNER_Y", SCOPES, otp=True)
    # The aggregator's registry binding: legitimate_interest, so the CM caps
    # the hop at this ceiling and seeks no subject grant on it. Its scopes are
    # the registry's own block names.
    binding("Aggregation Layer -> farmer", "aggregation-layer-farmer", "farmer-registry",
            "PARTNER_AGGREGATION_LAYER", ["farmer_personal_details", "family_details"],
            otp=False, basis="legitimate_interest")
    cr = ok(cm.post("/consent/v1/consent-requests", json={
        "subject_id": SUBJECT, "partner_id": px["id"], "purpose": PURPOSE,
        "requested_scopes": SCOPES}))
    consent_x = approve_on_cm(cr["id"], SCOPES)

    print("\n0  contract and guard rails")
    paths = ok(cm.get("/openapi.json"))["paths"]
    agg_paths = [p for p in paths if "aggregat" in p or "grants" in p
                 or "by-audience" in p or "granted-scopes" in p or "async" in p]
    check("no aggregation / grants / by-audience route on the CM", not agg_paths, agg_paths)
    catalog = ok(agg.get("/aggregation/v1/registries"))["registries"]
    check("discovery lists the catalog's scope ids, no URLs",
          [s["scopeId"] for s in catalog[0]["scopes"]] == SCOPES
          and "127.0.0.1" not in json.dumps(catalog))
    # openg2p-fastapi-common answers a request validation error with 400.
    bad = seek("PARTNER_X", "partner-x", SCOPES, fields=["x"])
    check("query with a property bene-360 forbids -> 400",
          bad.status_code == 400 and "Extra inputs are not permitted" in bad.text,
          bad.text[:80])
    other = seek("PARTNER_X", "partner-x", SCOPES, foundationalId="someone-else")
    check("foundationalId other than the consent subject -> 403",
          other.status_code == 403 and other.json().get("error") == "subject_mismatch",
          other.text[:80])
    none = seek("PARTNER_X", "partner-x", SCOPES, sections=["PROGRAMS"])
    check("only unsupported sections -> 422", none.status_code == 422, none.text[:80])

    print("\nA  consent held + OTP: seek -> OTP -> own grant -> registry -> callback")
    ack = ok(seek("PARTNER_X", "partner-x", SCOPES,
                  registryFilter=["FARMER_REGISTRY", "WORKER_REGISTRY"]))
    check("ack asks for OTP", ack["otp_required"] is True)
    check("ack lists the accepted scope ids", ack["accepted_scopes"] == SCOPES)
    code = ok(agg.get("/aggregation/v1/requests/%s/otp" % ack["aggregation_id"]))["otp"]
    ok(agg.post("/aggregation/v1/requests/%s/verify-otp" % ack["aggregation_id"],
                json={"otp": code}))
    body = wait_callback(ack["correlation_id"])
    record = record_of(body)
    farmer = farmer_register(record)
    check("callback received", body is not None)
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
    check("subject_authentication = otp",
          body and body["header"]["meta"]["subject_authentication"] == "otp")
    row = agg.get("/aggregation/v1/requests/%s" % ack["aggregation_id"]).json()
    check("aggregation stands on a CM consent record", bool(row.get("cm_consent_id")))
    check("grant held by the aggregation layer",
          bool((row.get("grant_ids") or {}).get("FARMER_REGISTRY")))
    every = ok(cm.get("/consent/v1/my/consents", params={"view": "all", "size": 100}))
    every = every.get("items", every)
    check("CM holds no originated grant for the aggregator",
          not [r for r in every if r.get("source") == "originated"
               and r.get("partner_id") != px["id"]])

    print("\nB  never asked: raise -> approve on CM -> poll -> release")
    ack = ok(seek("PARTNER_Y", "partner-y", SCOPES))
    check("parked on a raised consent request",
          ack["consent_request_id"] and ack["otp_required"] is False)
    approve_on_cm(ack["consent_request_id"], [PERSONAL])   # grants one of two
    body = wait_callback(ack["correlation_id"])
    record = record_of(body)
    farmer = farmer_register(record)
    check("poller saw the approval and released the aggregation", body is not None)
    check("only the granted scope delivered",
          list(farmer.get("attributes") or {}) == ["farmer_personal_details"]
          and farmer.get("tables") == [], farmer)
    check("response validates against response.schema.json", not schema_errors(record))
    check("consent-screen OTP carried as the authentication",
          body and body["header"]["meta"]["subject_authentication"] == "otp")

    print("\nC  withdraw while waiting for OTP -> poll -> rejected")
    ack = ok(seek("PARTNER_X", "partner-x", SCOPES))
    check("pending OTP", ack["otp_required"] is True)
    ok(cm.post("/consent/v1/my/consents/%s/revoke" % consent_x["id"], json={}))
    row = wait_status(ack["aggregation_id"], "rejected")
    check("withdrawal read from the CM cancelled it",
          row.get("status") == "rejected" and row.get("failure_reason") == "consent_withdrawn",
          "%s/%s" % (row.get("status"), row.get("failure_reason")))

    print("\nD  consent request denied on the CM -> rejected")
    ack = ok(seek("PARTNER_Y", "partner-z", SCOPES))
    ok(cm.post("/consent/v1/consent-requests/%s/deny" % ack["consent_request_id"], json={}))
    row = wait_status(ack["aggregation_id"], "rejected")
    check("denial read from the CM rejected it",
          row.get("status") == "rejected" and row.get("failure_reason") == "consent_denied",
          "%s/%s" % (row.get("status"), row.get("failure_reason")))

    print("\nE  approved after the CM replay window -> rejected, re-seek")
    ack = ok(seek("PARTNER_Y", "partner-w", SCOPES))
    time.sleep(REPLAY_WINDOW + 2)
    approve_on_cm(ack["consent_request_id"], SCOPES)
    row = wait_status(ack["aggregation_id"], "rejected")
    check("late approval rejected with a reason the partner can act on",
          row.get("failure_reason") == "consent_approved_after_replay_window",
          "%s/%s" % (row.get("status"), row.get("failure_reason")))
    again = ok(seek("PARTNER_Y", "partner-w", SCOPES))
    check("re-seek is permitted on the grant the subject gave",
          not again.get("consent_request_id") and again["otp_required"] is True)

    failed = [n for n, good in results if not good]
    print("\n%d/%d passed" % (len(results) - len(failed), len(results)))
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
