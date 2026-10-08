"""Smoke-test the Aggregation Layer + Consent Manager on the running local stack.

Run postman/agg-prep.py first (it signs a fresh partner consent object for the
subject "staff" and writes the Postman environment); this reads that file.

  A  consent held (komal-aggregator, OTP policy): seek -> OTP ->
     farmer-consent-validate -> three registries -> on-search at :9099
  B  never asked: seek for a subject with no consent -> CM raises a consent
     request -> approve it on the CM (OTP) -> CM event -> released -> on-search

    python scripts/stack-check.py
"""
import json
import os
import pathlib
import sys
import time
import uuid
from datetime import datetime, timedelta, timezone

import httpx
from jwt.api_jws import PyJWS

REPO = pathlib.Path(__file__).resolve().parent.parent
ENV = json.loads((REPO / "postman" / "OpenG2P-Aggregator.postman_environment.json")
                 .read_text(encoding="utf-8"))
V = {v["key"]: v["value"] for v in ENV["values"]}
AGG, CM, KC = V["agg_url"], V["cm_url"], os.environ.get("G2P_KEYCLOAK", "http://localhost:8080")
CALLBACK_ALL = V["callback_url"].rsplit("/", 1)[0] + "/all"
results = []


def check(name, cond, extra=""):
    results.append(bool(cond))
    print("  %-58s %s %s" % (name, "PASS" if cond else "FAIL", extra))


def token():
    return httpx.post(KC + "/realms/staff/protocol/openid-connect/token", data={
        "grant_type": "password", "client_id": "consent-manager-ui",
        "username": "staff", "password": "staff", "scope": "openid"}).json()["access_token"]


def seek(consent_jws, fields, query, registry_queries):
    return httpx.post(AGG + "/dci/registry/async/search", timeout=60, json={
        "header": {"sender_id": V["partner_audience"], "receiver_id": "aggregation-layer",
                   "sender_uri": V["callback_url"]},
        "message": {"transaction_id": uuid.uuid4().hex[:12], "search_request": [{
            "reference_id": "chk-" + uuid.uuid4().hex[:6],
            "search_criteria": {
                "query": {"value": {"id_type": "functional_id", "id_value": query}},
                "fields": fields, "registry_queries": registry_queries,
                "purpose": {"code": "loan_origination"},
                "authorize": {"consent_jws": consent_jws}}}]}})


def wait_callback(correlation_id, seconds=60):
    end = time.time() + seconds
    while time.time() < end:
        for item in httpx.get(CALLBACK_ALL).json().get("items", []):
            body = item.get("body", item)
            if (body.get("message") or {}).get("correlation_id") == correlation_id:
                return body
        time.sleep(1)
    return None


def report(body):
    regs = body["header"]["meta"]["registries"]
    for name in ("farmer", "livestock", "cropsown"):
        if name in regs:
            r = regs[name]
            print("      %-10s %-6s records=%s %s" % (name, r.get("status"), r.get("records"),
                                                    r.get("fields") or r.get("reason") or ""))
    return regs


def main():
    H = {"Authorization": "Bearer " + token()}
    fields = json.loads(V["fields"])
    rq = json.loads(V["registry_queries"])

    print("A  consent held: seek -> OTP -> farmer-consent-validate -> callback")
    r = seek(V["consent_jws"], fields, V["query_id_value"], rq)
    ack = r.json()
    check("seek accepted (202, OTP required)", r.status_code == 202 and ack.get("otp_required"),
          "" if r.status_code == 202 else r.text[:200])
    if r.status_code != 202:
        print("  (consent_jws is valid for 300s - re-run postman/agg-prep.py)")
        return 1
    otp = httpx.get(AGG + "/consent/v1/aggregation/%s/otp" % ack["aggregation_id"],
                    headers=H).json().get("otp")
    check("OTP readable (dev)", bool(otp))
    v = httpx.post(AGG + "/consent/v1/farmer-consent-validate", headers=H, json={
        "aggregation_id": ack["aggregation_id"], "otp": otp})
    check("farmer-consent-validate", v.status_code == 200, v.text[:150])
    body = wait_callback(ack["correlation_id"])
    check("on-search delivered to :9099", body is not None)
    if body:
        regs = report(body)
        check("farmer registry answered", regs.get("farmer", {}).get("status") == "ok")
        check("every registry answered",
              all(regs.get(n, {}).get("status") == "ok"
                  for n in ("farmer", "livestock", "cropsown")))
        if not regs.get("cropsown", {}).get("records"):
            print("      note: cropsown has no record for %s - its register is empty after "
                  "the demo clean-up; approve an intake in the Cropsown UI (:3004)"
                  % regs.get("cropsown", {}).get("queried"))

    print("\nB  never asked: raise on the CM -> approve -> event -> callback")
    claims = json.loads(PyJWS().decode_complete(V["consent_jws"],
                                                options={"verify_signature": False})["payload"])
    print("  (needs the partner key agg-prep registered; signing a new object for a fresh subject)")
    key_file = REPO / "postman" / ".agg-prep-key.pem"
    if not key_file.exists():
        print("  skipped: no %s (agg-prep.py writes it)" % key_file.name)
    else:
        from cryptography.hazmat.primitives import serialization
        priv = serialization.load_pem_private_key(key_file.read_bytes(), None)
        now = datetime.now(timezone.utc)
        subject = "chk-" + uuid.uuid4().hex[:8]
        claims.update(jti=uuid.uuid4().hex, subject_id={"type": "national_id", "value": subject},
                      issued_at=now.isoformat(), data_scopes=["farmer.firstname", "farmer.lastname"],
                      validity={"valid_from": now.isoformat(),
                                "valid_until": (now + timedelta(days=1)).isoformat()})
        jws = PyJWS().encode(json.dumps(claims, sort_keys=True, separators=(",", ":")).encode(),
                             priv, algorithm="ES256", headers={"kid": V["kid"]})
        r = seek(jws, ["farmer.firstname", "farmer.lastname"], V["query_id_value"], {})
        ack = r.json()
        check("parked on a consent request raised in the CM",
              r.status_code == 202 and ack.get("consent_request_id"), r.text[:200])
        if ack.get("consent_request_id"):
            crid = ack["consent_request_id"]
            print("      consent_url: %s" % ack.get("consent_url"))
            httpx.post(CM + "/consent/v1/consent-requests/%s/otp" % crid, headers=H)
            code = httpx.get(CM + "/consent/v1/consent-requests/%s/otp" % crid, headers=H).json()["otp"]
            httpx.post(CM + "/consent/v1/consent-requests/%s/verify-otp" % crid, headers=H,
                       json={"otp": code})
            a = httpx.post(CM + "/consent/v1/consent-requests/%s/approve" % crid, headers=H,
                           json={"granted_scopes": ["farmer.firstname"]})
            check("approved on the CM (first name only)", a.status_code == 201, a.text[:150])
            body = wait_callback(ack["correlation_id"])
            check("CM event released it; on-search delivered", body is not None)
            if body:
                rec = ((body["message"]["search_response"][0]["data"]["reg_records"]) or [{}])[0]
                check("only the granted field", sorted(rec) == ["farmer.firstname"], rec)

    q = httpx.get(AGG + "/consent/v1/aggregation/queue", headers=H)
    check("queue status", q.status_code == 200, q.json().get("mode") if q.status_code == 200 else q.text[:100])
    print("\n%d/%d passed" % (sum(results), len(results)))
    return 0 if all(results) else 1


if __name__ == "__main__":
    sys.exit(main())
