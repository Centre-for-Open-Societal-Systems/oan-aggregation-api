"""End-to-end: Aggregation Layer <-> Consent Manager over HTTP only.

Covers the five CM additions (docs/CM-API-CONTRACT.md) through the flows that
use them:

  A  partner already holds consent, policy demands an OTP
     seek -> validate (#1) -> OTP -> grants (#3) -> registry -> callback
  B  partner never asked -> consent raised -> approved on the CM
     seek -> by-audience (#2) -> raise -> approve -> event (#5)
     -> granted-scopes (#4) -> grants (#3) -> registry -> callback
  C  consent withdrawn while an aggregation waits for its OTP -> event (#5)
     -> aggregation rejected
  D  My consents still groups the registry grants under the consent

Started by run.sh, which brings up Postgres, the CM, the Aggregation Layer and
fakes.py first.
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


def binding(name, audience, controller, pm_id, scopes, otp):
    partner = ok(cm.post("/consent/v1/partners", json={
        "name": name, "audience": audience, "controller_id": controller,
        "partner_mgmt_id": pm_id}))
    ok(cm.put("/consent/v1/partners/%s/policy" % partner["id"], json={
        "allowed_data_scopes": scopes, "allowed_purposes": ["loan_origination"],
        "allowed_signing_algs": ["EdDSA"], "required_auth_method": "otp" if otp else None,
        "lawful_basis": "consent"}))
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
    py = binding("Partner Y", "partner-y", "agg", "PARTNER_Y", FIELDS, otp=True)
    binding("Aggregation Layer -> farmer", "aggregation-layer-farmer", "farmer-registry",
            "PARTNER_AGGREGATION_LAYER", ["farmer_personal_details"], otp=False)
    cr = ok(cm.post("/consent/v1/consent-requests", json={
        "subject_id": SUBJECT, "partner_id": px["id"], "purpose": PURPOSE,
        "requested_scopes": FIELDS}))
    consent_x = approve_on_cm(cr["id"], FIELDS)

    print("\n#1 validate carries partner_id / partner_audience")
    d = ok(cm.post("/consent/v1/validate", json={
        "consent_jws": partner_jws("PARTNER_X", "partner-x", FIELDS),
        "request_context": {"requested_scopes": FIELDS}}))
    check("permit", d["decision"] == "permit", d.get("reason_code"))
    check("partner_id = Partner X binding", d.get("partner_id") == px["id"])
    check("partner_audience = partner-x", d.get("partner_audience") == "partner-x")

    print("\n#2 partner by audience")
    p = cm.get("/consent/v1/partners/by-audience/partner-y")
    check("found", p.status_code == 200 and p.json()["id"] == py["id"])
    check("unknown audience -> 404",
          cm.get("/consent/v1/partners/by-audience/nobody").status_code == 404)

    print("\nA  consent held + OTP: seek -> OTP -> grants -> registry -> callback")
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
    check("registry accepted the recorded grant (#3)",
          body and body["header"]["meta"]["registries"].get("farmer", {}).get("status") == "ok",
          body and body["header"]["meta"]["registries"].get("farmer"))
    check("record = Mary Bell", record.get("farmer.firstname") == "Mary"
          and record.get("farmer.lastname") == "Bell", record)
    check("subject_authentication = otp",
          body and body["header"]["meta"]["subject_authentication"] == "otp")

    print("\nB  never asked: raise -> approve on CM -> event -> release")
    ack = ok(seek("PARTNER_Y", "partner-y", FIELDS))
    check("parked on a raised consent request",
          ack["consent_request_id"] and ack["otp_required"] is False)
    approve_on_cm(ack["consent_request_id"], ["farmer.firstname"])   # grants one of two
    gs = ok(cm.get("/consent/v1/consent-requests/%s/granted-scopes" % ack["consent_request_id"]))
    check("#4 granted-scopes = what the farmer ticked",
          gs["granted_scopes"] == ["farmer.firstname"] and gs["otp_verified_at"], gs)
    body = wait_callback(ack["correlation_id"])
    record = ((body or {}).get("message", {}).get("search_response", [{}])[0]
              .get("data", {}).get("reg_records") or [{}])[0]
    check("#5 approved event released the aggregation", body is not None)
    check("only the granted field delivered", record == {"farmer.firstname": "Mary"}, record)

    print("\nC  withdraw while waiting for OTP -> event -> rejected")
    ack = ok(seek("PARTNER_X", "partner-x", FIELDS))
    check("pending OTP", ack["otp_required"] is True)
    ok(cm.post("/consent/v1/my/consents/%s/revoke" % consent_x["id"], json={}))
    row = wait_status(ack["aggregation_id"], "rejected")
    check("#5 withdrawn event cancelled it",
          row.get("status") == "rejected" and row.get("failure_reason") == "consent_withdrawn",
          "%s/%s" % (row.get("status"), row.get("failure_reason")))

    print("\nD  My consents grouping")
    items = ok(cm.get("/consent/v1/my/consents", params={"view": "consents", "size": 100}))
    rows = items.get("items", items)
    loose = [r for r in rows if r.get("record_kind") == "registry_grant"]
    check("no registry grant shown as a loose top-level row", not loose,
          "%d top-level, %d loose grants" % (len(rows), len(loose)))
    every = ok(cm.get("/consent/v1/my/consents", params={"view": "all", "size": 100}))
    every = every.get("items", every)
    consent_ids = {r["id"] for r in rows}
    grants = [r for r in every if r.get("record_kind") == "registry_grant"]
    check("registry grants exist and each hangs off a consent",
          grants and all(g.get("derived_from") in consent_ids for g in grants),
          "%d grant(s), parents %s" % (len(grants),
                                       sorted({g.get("derived_from") for g in grants})))
    # B asked the registry for the same block, purpose and method as A, so the
    # CM reused A's grant (exact-match reuse) instead of minting a second one.
    check("B reused A's grant; it stays under consent X",
          len(grants) == 1 and grants[0].get("derived_from") == consent_x["id"])

    failed = [n for n, good in results if not good]
    print("\n%d/%d passed" % (len(results) - len(failed), len(results)))
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
