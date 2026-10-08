"""End-to-end: Aggregation Layer <-> Consent Manager over the CM's generic APIs only.

The CM carries no aggregation code. The aggregation layer uses
POST /consent/v1/validate, GET /consent/v1/partners(/{id}/policy),
POST + GET /consent/v1/consent-requests(/{id}) and
GET /consent/v1/consents/{id}/status, and keeps its own grants.

  A  partner already holds consent, policy demands an OTP
     seek -> validate -> OTP -> own grant -> registry (legitimate_interest
     binding) -> callback; the CM records no grant for the aggregator
  B  partner never asked -> consent raised -> approved on the CM
     -> poller reads the request -> re-validate (granted scopes) -> callback
  C  consent withdrawn while an aggregation waits for its OTP
     -> poller reads the consent status -> aggregation rejected
  D  consent request denied on the CM -> aggregation rejected
  E  approval after the CM's replay window -> rejected, partner must re-seek

Started by run.sh, which brings up Postgres, the CM (replay window shortened
so E is quick), the Aggregation Layer (poll every second) and fakes.py first.
"""
import os
import pathlib
import sys
import time
import uuid
from datetime import datetime, timedelta, timezone

import httpx
import jwt

CM = os.environ.get("E2E_CM", "http://127.0.0.1:18000")
AGG = os.environ.get("E2E_AGG", "http://127.0.0.1:18100")
FAKES = os.environ.get("E2E_FAKES", "http://127.0.0.1:18090")
KEYS = pathlib.Path(os.environ["E2E_KEYS"])
# Must match consent_manager_replay_freshness_window_sec in run.sh.
REPLAY_WINDOW = int(os.environ.get("E2E_REPLAY_WINDOW", "8"))

# auth is off on both services, so every caller is the dev subject.
SUBJECT = {"type": "national_id", "value": "dev"}
PURPOSE = {"code": "loan_origination"}
FIELDS = ["farmer.firstname", "farmer.lastname"]

cm = httpx.Client(base_url=CM, timeout=20)
agg = httpx.Client(base_url=AGG, timeout=20)
results = []


def check(name, cond, extra=""):
    results.append((name, bool(cond)))
    print("  %-60s %s %s" % (name, "PASS" if cond else "FAIL", extra))


def ok(response):
    if response.status_code >= 400:
        raise SystemExit("%s %s -> %s %s" % (response.request.method, response.request.url,
                                             response.status_code, response.text))
    return response.json()


def partner_jws(ref, audience, fields):
    now = datetime.now(timezone.utc)
    claims = {
        "@context": "https://openg2p.org/contexts/consent_object.jsonld",
        "@type": "ConsentObject", "jti": uuid.uuid4().hex, "iss": audience,
        "aud": audience, "data_controller": "agg",
        "subject_id": SUBJECT, "purpose": PURPOSE, "data_scopes": fields,
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
    """What the farmer does on the CM consent screen."""
    ok(cm.post("/consent/v1/consent-requests/%s/otp" % request_id))
    code = ok(cm.get("/consent/v1/consent-requests/%s/otp" % request_id))["otp"]
    ok(cm.post("/consent/v1/consent-requests/%s/verify-otp" % request_id, json={"otp": code}))
    return ok(cm.post("/consent/v1/consent-requests/%s/approve" % request_id,
                      json={"granted_scopes": granted}))


def seek(ref, audience, fields):
    return agg.post("/dci/registry/async/search", json={
        "header": {"sender_id": audience, "receiver_id": "aggregation-layer",
                   "sender_uri": FAKES + "/callback"},
        "message": {"transaction_id": uuid.uuid4().hex[:12], "search_request": [{
            "reference_id": "ref-" + uuid.uuid4().hex[:6],
            "search_criteria": {
                "query": {"value": {"id_type": "functional_id", "id_value": "FR-1"}},
                "fields": fields, "purpose": PURPOSE,
                "authorize": {"consent_jws": partner_jws(ref, audience, fields)}}}]}})


def wait_callback(correlation_id, seconds=20):
    end = time.time() + seconds
    while time.time() < end:
        for body in httpx.get(FAKES + "/received").json():
            if body["message"]["correlation_id"] == correlation_id:
                return body
        time.sleep(0.5)
    return None


def wait_status(aggregation_id, want, seconds=15):
    end = time.time() + seconds
    row = {}
    while time.time() < end:
        row = agg.get("/consent/v1/aggregation/%s" % aggregation_id).json()
        if row.get("status") == want:
            return row
        time.sleep(0.5)
    return row


def main():
    print("setup: CM bindings")
    px = binding("Partner X", "partner-x", "agg", "PARTNER_X", FIELDS, otp=True)
    binding("Partner Y", "partner-y", "agg", "PARTNER_Y", FIELDS, otp=True)
    binding("Partner Z", "partner-z", "agg", "PARTNER_Y", FIELDS, otp=True)
    binding("Partner W", "partner-w", "agg", "PARTNER_Y", FIELDS, otp=True)
    # The aggregator's registry binding: legitimate_interest, so the CM caps
    # the hop at this ceiling and seeks no subject grant on it.
    binding("Aggregation Layer -> farmer", "aggregation-layer-farmer", "farmer-registry",
            "PARTNER_AGGREGATION_LAYER", ["farmer_personal_details"], otp=False,
            basis="legitimate_interest")
    cr = ok(cm.post("/consent/v1/consent-requests", json={
        "subject_id": SUBJECT, "partner_id": px["id"], "purpose": PURPOSE,
        "requested_scopes": FIELDS}))
    consent_x = approve_on_cm(cr["id"], FIELDS)

    print("\n0  the CM carries no aggregation API")
    paths = ok(cm.get("/openapi.json"))["paths"]
    agg_paths = [p for p in paths if "aggregat" in p or "grants" in p
                 or "by-audience" in p or "granted-scopes" in p or "async" in p]
    check("no aggregation / grants / by-audience route on the CM", not agg_paths, agg_paths)

    print("\nA  consent held + OTP: seek -> OTP -> own grant -> registry -> callback")
    ack = ok(seek("PARTNER_X", "partner-x", FIELDS))
    check("ack asks for OTP", ack["otp_required"] is True)
    code = ok(agg.get("/consent/v1/aggregation/%s/otp" % ack["aggregation_id"]))["otp"]
    ok(agg.post("/consent/v1/aggregation/%s/verify-otp" % ack["aggregation_id"],
                json={"otp": code}))
    body = wait_callback(ack["correlation_id"])
    record = (body or {}).get("message", {}).get("search_response", [{}])[0] \
        .get("data", {}).get("reg_records", [{}])
    record = record[0] if record else {}
    check("callback received", body is not None)
    check("registry permitted the legitimate_interest hop",
          body and body["header"]["meta"]["registries"].get("farmer", {}).get("status") == "ok",
          body and body["header"]["meta"]["registries"].get("farmer"))
    check("record = Mary Bell", record.get("farmer.firstname") == "Mary"
          and record.get("farmer.lastname") == "Bell", record)
    check("subject_authentication = otp",
          body and body["header"]["meta"]["subject_authentication"] == "otp")
    row = agg.get("/consent/v1/aggregation/%s" % ack["aggregation_id"]).json()
    check("aggregation stands on a CM consent record", bool(row.get("cm_consent_id")))
    check("grant held by the aggregation layer", bool((row.get("grant_ids") or {}).get("farmer")))
    every = ok(cm.get("/consent/v1/my/consents", params={"view": "all", "size": 100}))
    every = every.get("items", every)
    check("CM holds no originated grant for the aggregator",
          not [r for r in every if r.get("source") == "originated"
               and r.get("partner_id") != px["id"]])

    print("\nB  never asked: raise -> approve on CM -> poll -> release")
    ack = ok(seek("PARTNER_Y", "partner-y", FIELDS))
    check("parked on a raised consent request",
          ack["consent_request_id"] and ack["otp_required"] is False)
    approve_on_cm(ack["consent_request_id"], ["farmer.firstname"])   # grants one of two
    body = wait_callback(ack["correlation_id"])
    record = ((body or {}).get("message", {}).get("search_response", [{}])[0]
              .get("data", {}).get("reg_records") or [{}])[0]
    check("poller saw the approval and released the aggregation", body is not None)
    check("only the granted field delivered", record == {"farmer.firstname": "Mary"}, record)
    check("consent-screen OTP carried as the authentication",
          body and body["header"]["meta"]["subject_authentication"] == "otp")

    print("\nC  withdraw while waiting for OTP -> poll -> rejected")
    ack = ok(seek("PARTNER_X", "partner-x", FIELDS))
    check("pending OTP", ack["otp_required"] is True)
    ok(cm.post("/consent/v1/my/consents/%s/revoke" % consent_x["id"], json={}))
    row = wait_status(ack["aggregation_id"], "rejected")
    check("withdrawal read from the CM cancelled it",
          row.get("status") == "rejected" and row.get("failure_reason") == "consent_withdrawn",
          "%s/%s" % (row.get("status"), row.get("failure_reason")))

    print("\nD  consent request denied on the CM -> rejected")
    ack = ok(seek("PARTNER_Y", "partner-z", FIELDS))
    ok(cm.post("/consent/v1/consent-requests/%s/deny" % ack["consent_request_id"], json={}))
    row = wait_status(ack["aggregation_id"], "rejected")
    check("denial read from the CM rejected it",
          row.get("status") == "rejected" and row.get("failure_reason") == "consent_denied",
          "%s/%s" % (row.get("status"), row.get("failure_reason")))

    print("\nE  approved after the CM replay window -> rejected, re-seek")
    ack = ok(seek("PARTNER_Y", "partner-w", FIELDS))
    time.sleep(REPLAY_WINDOW + 2)
    approve_on_cm(ack["consent_request_id"], FIELDS)
    row = wait_status(ack["aggregation_id"], "rejected")
    check("late approval rejected with a reason the partner can act on",
          row.get("failure_reason") == "consent_approved_after_replay_window",
          "%s/%s" % (row.get("status"), row.get("failure_reason")))
    again = ok(seek("PARTNER_Y", "partner-w", FIELDS))
    check("re-seek is permitted on the grant the subject gave",
          not again.get("consent_request_id") and again["otp_required"] is True)

    failed = [n for n, good in results if not good]
    print("\n%d/%d passed" % (len(results) - len(failed), len(results)))
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
