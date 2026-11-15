"""Smoke-test the Aggregation Layer + Consent Manager on the running local stack.

Run postman/agg-prep.py first (it gives the subject an active consent for the
partner and signs a partner consent object; it writes the local Postman
environment); this reads that file and the registry catalog, so it checks
whatever registries the catalog lists.

  A  sync:  consent held -> POST /dci/registry/sync/search -> the on-search is
            the response, every catalog registry answered
  B  async: consent held -> POST /dci/registry/async/search -> 202 -> the
            on-search arrives at the callback receiver
  C  never asked: a subject with no consent -> 403 no_subject_consent; the
     partner raises a consent request in the CM, the subject confirms it with
     the one-time code, ONE scope is granted -> a new consent object -> sync ->
     only that scope's registry is queried

    python scripts/stack-check.py [--catalog deploy/registries.yaml]

C needs the CM with otp_debug_enabled (the code is read back from the CM).
Needs a venv with httpx, pyjwt, cryptography, pydantic and pyyaml (jsonschema
optional: with it the response is checked against the bene-360 schema in
test/fixtures).
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


def search(V, consent_jws, foundational_id, mode="sync"):
    return httpx.post(V["agg_url"] + "/dci/registry/%s/search" % mode, timeout=90, json={
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


def check_record(catalog, validator, body, V):
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
              "beneficiary the registries know and re-run agg-prep.py" % V["foundational_id"])


def sign(V, priv, claims, **changes):
    now = datetime.now(timezone.utc)
    claims = dict(claims, jti=uuid.uuid4().hex, issued_at=now.isoformat(),
                  validity={"valid_from": now.isoformat(),
                            "valid_until": (now + timedelta(days=1)).isoformat()}, **changes)
    return PyJWS().encode(json.dumps(claims, sort_keys=True, separators=(",", ":")).encode(),
                          priv, algorithm="ES256", headers={"kid": V["kid"]})


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
    key_file = REPO / "postman" / ".agg-prep-key.pem"
    if not key_file.exists():
        raise SystemExit("no %s (agg-prep.py writes it)" % key_file.name)
    from cryptography.hazmat.primitives import serialization
    priv = serialization.load_pem_private_key(key_file.read_bytes(), None)
    claims = json.loads(PyJWS().decode_complete(
        V["consent_jws"], options={"verify_signature": False})["payload"])

    print("A  sync: consent held -> the on-search is the response")
    r = search(V, sign(V, priv, claims), V["foundational_id"])
    check("sync search answered (200)", r.status_code == 200,
          "" if r.status_code == 200 else r.text[:200])
    if r.status_code == 200:
        body = r.json()
        check("signed on-search", body.get("signature") and
              body["header"].get("action") == "on-search")
        check_record(catalog, validator, body, V)

    print("\nB  async: consent held -> 202 -> on-search at the callback")
    r = search(V, sign(V, priv, claims), V["foundational_id"], mode="async")
    ack = r.json()
    check("async search accepted (202)", r.status_code == 202, r.text[:200])
    if r.status_code == 202:
        body = wait_callback(ack["correlation_id"])
        check("on-search delivered to the callback receiver", body is not None)
        if body:
            check_record(catalog, validator, body, V)

    print("\nC  never asked: 403 -> consent request + OTP on the CM -> sync")
    subject = "chk-" + uuid.uuid4().hex[:8]
    first = catalog.scope_ids()[0]
    unasked = dict(claims, subject_id={"type": "national_id", "value": subject},
                   data_scopes=catalog.scope_ids())
    r = search(V, sign(V, priv, unasked), subject)
    check("refused: 403 no_subject_consent",
          r.status_code == 403 and r.json().get("error") == "no_subject_consent", r.text[:200])
    CM = V["cm_url"]
    cr = httpx.post(CM + "/consent/v1/consent-requests", headers=H, timeout=30, json={
        "subject_id": {"type": "national_id", "value": subject},
        "partner_id": V["partner_uuid"],
        "purpose": {"code": "loan_origination", "text": "stack-check"},
        "requested_scopes": catalog.scope_ids()})
    check("partner raised a consent request in the CM", cr.status_code == 201, cr.text[:200])
    if cr.status_code == 201:
        crid = cr.json()["id"]
        httpx.post(CM + "/consent/v1/consent-requests/%s/otp" % crid, headers=H, timeout=30)
        peek = httpx.get(CM + "/consent/v1/consent-requests/%s/otp" % crid, headers=H)
        code = peek.json().get("otp") if peek.status_code == 200 else None
        check("one-time code issued (read back: CM otp_debug_enabled)", bool(code),
              "" if code else peek.text[:150])
        v = httpx.post(CM + "/consent/v1/consent-requests/%s/verify-otp" % crid, headers=H,
                       json={"otp": code or "000000"})
        check("code verified on the CM", v.status_code == 200, v.text[:150])
        a = httpx.post(CM + "/consent/v1/consent-requests/%s/approve" % crid, headers=H,
                       json={"granted_scopes": [first]})
        check("consent granted (%s only)" % first, a.status_code == 201, a.text[:150])
        r = search(V, sign(V, priv, unasked), subject)
        check("sync search answered after the consent (200)", r.status_code == 200, r.text[:200])
        if r.status_code == 200:
            record = record_of(r.json())
            registry = first.split(".", 1)[0]
            check("only the granted scope's registry was queried",
                  record["meta"]["sourceSystemsQueried"] == [registry],
                  record["meta"]["sourceSystemsQueried"])

    print("\n%d/%d passed" % (sum(results), len(results)))
    return 0 if all(results) else 1


if __name__ == "__main__":
    sys.exit(main())
