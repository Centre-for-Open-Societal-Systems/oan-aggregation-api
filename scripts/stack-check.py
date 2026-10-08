"""Smoke-test the Aggregation Layer + Consent Manager on the running local stack.

Run postman/agg-prep.py first (it signs a fresh partner consent object for the
subject and writes the local Postman environment); this reads that file and
the registry catalog, so it checks whatever registries the catalog lists.

  A  consent held (OTP policy): Beneficiary-360 seek -> OTP -> verify-otp ->
     every catalog registry -> on-search at the callback receiver
  B  never asked: seek for a subject with no consent -> CM raises a consent
     request -> approve ONE scope on the CM (OTP) -> AL polls the CM ->
     released -> only that scope's registry is queried

    python scripts/stack-check.py [--catalog deploy/registries.yaml]

Needs a venv with httpx, pyjwt, cryptography, pydantic and pyyaml (jsonschema
optional: with it the delivered response is checked against the bene-360
schema in test/fixtures).
"""
import argparse
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
sys.path.insert(0, str(REPO / "backend" / "src"))
from openg2p_aggregation_layer.registry_catalog import CatalogError, load_catalog  # noqa: E402

ENV_FILE = REPO / "postman" / "OpenG2P-Aggregator.local.postman_environment.json"
CALLBACK_ALL = os.environ.get("G2P_CALLBACK_ALL", "http://localhost:9099/all")
CONTEXT = "https://schemas.openg2p.org/beneficiary360/v1/context.jsonld"
results = []


def check(name, cond, extra=""):
    results.append(bool(cond))
    print("  %-60s %s %s" % (name, "PASS" if cond else "FAIL", extra))


def schema_validator():
    try:
        from jsonschema import Draft202012Validator
    except ImportError:
        return None
    path = REPO / "test" / "fixtures" / "bene360" / "response.schema.json"
    return Draft202012Validator(json.loads(path.read_text(encoding="utf-8")))


def seek(V, consent_jws, foundational_id):
    return httpx.post(V["agg_url"] + "/dci/registry/async/search", timeout=60, json={
        "header": {"sender_id": V["partner_audience"], "receiver_id": "aggregation-layer",
                   "sender_uri": V["callback_url"]},
        "message": {"transaction_id": uuid.uuid4().hex[:12], "search_request": [{
            "reference_id": "chk-" + uuid.uuid4().hex[:6],
            "search_criteria": {
                "query_type": "beneficiary360",
                "query": {"@context": CONTEXT, "foundationalId": foundational_id,
                          "timeframe": "Timeframe-Medium", "sections": ["REGISTRIES"]},
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


def record_of(body):
    return (body["message"]["search_response"][0]["data"]["reg_records"] or [{}])[0]


def report(catalog, record):
    """One line per catalog registry: matched, empty, or why not."""
    matched = {r["registryCode"]: r for r in record.get("registries") or []}
    warnings = {w["system"]: w for w in record.get("meta", {}).get("warnings") or []}
    queried = record.get("meta", {}).get("sourceSystemsQueried") or []
    for code in catalog.codes():
        if code in matched:
            registers = matched[code]["registers"]
            line = "matched   %d register(s): %s" % (len(registers), ", ".join(
                r["registerMnemonic"] for r in registers))
        elif code in warnings:
            line = "%-9s %s" % (warnings[code].get("code"), warnings[code].get("message"))
        elif code in queried:
            line = "queried   no record for this foundationalId"
        else:
            line = "-"
        print("      %-24s %s" % (code, line))
    return matched, warnings


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--catalog", default=str(REPO / "deploy" / "registries.yaml"))
    args = ap.parse_args()
    try:
        catalog = load_catalog(args.catalog)
    except CatalogError as exc:
        raise SystemExit(str(exc))
    if not ENV_FILE.exists():
        raise SystemExit("%s not found - run postman/agg-prep.py first" % ENV_FILE.name)
    V = {v["key"]: v["value"] for v in json.loads(ENV_FILE.read_text("utf-8"))["values"]}
    H = {"Authorization": "Bearer " + V["access_token"]}
    validator = schema_validator()

    print("A  consent held: seek -> OTP -> verify-otp -> callback")
    r = seek(V, V["consent_jws"], V["foundational_id"])
    ack = r.json()
    check("seek accepted (202, OTP required)", r.status_code == 202 and ack.get("otp_required"),
          "" if r.status_code == 202 else r.text[:200])
    if r.status_code != 202:
        print("  (consent_jws is valid for 300s - re-run postman/agg-prep.py)")
        return 1
    otp = httpx.get(V["agg_url"] + "/aggregation/v1/requests/%s/otp" % ack["aggregation_id"],
                    headers=H).json().get("otp")
    check("OTP readable (dev)", bool(otp))
    v = httpx.post(V["agg_url"] + "/aggregation/v1/requests/%s/verify-otp"
                   % ack["aggregation_id"], headers=H, json={"otp": otp})
    check("verify-otp with the subject's token", v.status_code == 200, v.text[:150])
    body = wait_callback(ack["correlation_id"])
    check("on-search delivered to the callback receiver", body is not None)
    if body:
        record = record_of(body)
        check("record is a Beneficiary-360 response",
              record.get("@type") == "Beneficiary360Response")
        if validator is not None:
            errors = list(validator.iter_errors(record))
            check("validates against response.schema.json", not errors,
                  errors[0].message if errors else "")
        matched, warnings = report(catalog, record)
        failed = [c for c in catalog.codes() if c in warnings]
        check("every catalog registry answered (no warning)", not failed, ", ".join(failed))
        if not matched:
            print("      note: no registry holds foundationalId %s - set G2P_SUBJECT_USER to a "
                  "beneficiary the registries know and re-run agg-prep.py"
                  % V["foundational_id"])

    print("\nB  never asked: raise on the CM -> approve one scope -> poll -> callback")
    key_file = REPO / "postman" / ".agg-prep-key.pem"
    if not key_file.exists():
        print("  skipped: no %s (agg-prep.py writes it)" % key_file.name)
    else:
        from cryptography.hazmat.primitives import serialization
        priv = serialization.load_pem_private_key(key_file.read_bytes(), None)
        claims = json.loads(PyJWS().decode_complete(
            V["consent_jws"], options={"verify_signature": False})["payload"])
        now = datetime.now(timezone.utc)
        subject = "chk-" + uuid.uuid4().hex[:8]
        first = catalog.scope_ids()[0]
        claims.update(jti=uuid.uuid4().hex, subject_id={"type": "national_id", "value": subject},
                      issued_at=now.isoformat(), data_scopes=catalog.scope_ids(),
                      validity={"valid_from": now.isoformat(),
                                "valid_until": (now + timedelta(days=1)).isoformat()})
        jws = PyJWS().encode(json.dumps(claims, sort_keys=True, separators=(",", ":")).encode(),
                             priv, algorithm="ES256", headers={"kid": V["kid"]})
        r = seek(V, jws, subject)
        ack = r.json()
        check("parked on a consent request raised in the CM",
              r.status_code == 202 and ack.get("consent_request_id"), r.text[:200])
        if ack.get("consent_request_id"):
            crid, CM = ack["consent_request_id"], V["cm_url"]
            print("      consent_url: %s" % ack.get("consent_url"))
            httpx.post(CM + "/consent/v1/consent-requests/%s/otp" % crid, headers=H)
            code = httpx.get(CM + "/consent/v1/consent-requests/%s/otp" % crid,
                             headers=H).json()["otp"]
            httpx.post(CM + "/consent/v1/consent-requests/%s/verify-otp" % crid, headers=H,
                       json={"otp": code})
            a = httpx.post(CM + "/consent/v1/consent-requests/%s/approve" % crid, headers=H,
                           json={"granted_scopes": [first]})
            check("approved on the CM (%s only)" % first, a.status_code == 201, a.text[:150])
            body = wait_callback(ack["correlation_id"])
            check("poll saw the approval; on-search delivered", body is not None)
            if body:
                record = record_of(body)
                registry = first.split(".", 1)[0]
                check("only the granted scope's registry was queried",
                      record["meta"]["sourceSystemsQueried"] == [registry],
                      record["meta"]["sourceSystemsQueried"])

    q = httpx.get(V["agg_url"] + "/aggregation/v1/queue", headers=H)
    check("queue status", q.status_code == 200,
          q.json().get("mode") if q.status_code == 200 else q.text[:100])
    print("\n%d/%d passed" % (sum(results), len(results)))
    return 0 if all(results) else 1


if __name__ == "__main__":
    sys.exit(main())
