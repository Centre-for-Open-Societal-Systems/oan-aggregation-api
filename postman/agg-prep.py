"""Generate the Postman environment for the Aggregation Layer collection.

Postman's sandbox has no ECDSA, so it cannot mint the partner's consent object.
This does the parts Postman cannot:

  1. registers a fresh partner signing key in Partner Management
  2. walks the subject through request -> authenticate -> approve so the
     partner's binding has an active grant for the requested SCOPE IDS
     (<registryCode>.<scope>, read from the registry catalog)
  3. signs the partner's consent object for that binding
  4. writes everything into OpenG2P-Aggregator.local.postman_environment.json
     (gitignored; the committed OpenG2P-Aggregator.postman_environment.json is
     an empty template)

The partner binding must already exist in the Consent Manager and allow the
scope ids (scripts/register-aggregator.py --partner <audience> extends it).

Run it from the host (all services are reached on their published ports):

    python postman/agg-prep.py
    python postman/agg-prep.py --scopes FARMER_REGISTRY.farmer_personal_details

    G2P_PARTNER_AUDIENCE   partner binding audience          (default demo-partner)
    G2P_PARTNER_PM_ID      its Partner Management partner id  (default PARTNER_DEMO)
    G2P_SUBJECT_USER/_PASSWORD  the subject's Keycloak login   (default staff/staff).
                           The subject is also the beneficiary: its username is
                           the foundationalId the registries are searched by.

Then import the environment into Postman, select it, and run the collection
top to bottom.

TWO THINGS EXPIRE, and they are the usual cause of a confusing failure:

  * the consent object    - CM enforces replay_freshness_window_sec = 300
  * the OTP               - otp_ttl_sec, also 300 by default

so re-run this if a seek starts coming back `replay`.

One more trap worth knowing: the Consent Manager caches Partner Management keys
for 300s. This script registers a NEW kid each run, so if the CM has cached that
partner's keys within the last five minutes the seek fails with
`signature_invalid`. Wait it out, or restart the CM.
"""
import argparse
import base64
import json
import os
import pathlib
import sys
import uuid
from datetime import datetime, timedelta, timezone

import httpx
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec
from jwt.api_jws import PyJWS

HERE = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent / "backend" / "src"))
from openg2p_aggregation_layer.registry_catalog import CatalogError, load_catalog  # noqa: E402

KEYCLOAK = os.environ.get("G2P_KEYCLOAK", "http://localhost:8080")
CM = os.environ.get("G2P_CM", "http://localhost:8000")
AGG = os.environ.get("G2P_AGG", "http://localhost:8110")
PM_ADMIN = os.environ.get("G2P_PM_ADMIN", "http://localhost:8051")
PM_KEYS = os.environ.get("G2P_PM_KEYS", "http://localhost:8050")
CALLBACK = os.environ.get("G2P_CALLBACK", "http://localhost:9099/on-search")

AUDIENCE = os.environ.get("G2P_PARTNER_AUDIENCE", "demo-partner")
PM_PARTNER = os.environ.get("G2P_PARTNER_PM_ID", "PARTNER_DEMO")
SUBJECT_USER = os.environ.get("G2P_SUBJECT_USER", "staff")
SUBJECT_PASSWORD = os.environ.get("G2P_SUBJECT_PASSWORD", "staff")
CONTEXT = "https://schemas.openg2p.org/beneficiary360/v1/context.jsonld"


def canonical(obj) -> bytes:
    return json.dumps(obj, sort_keys=True, separators=(",", ":"),
                      ensure_ascii=False).encode("utf-8")


def b64u(obj) -> str:
    return base64.urlsafe_b64encode(json.dumps(obj).encode()).decode().rstrip("=")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--catalog", default=str(HERE.parent / "deploy" / "registries.yaml"))
    ap.add_argument("--scopes", help="comma-separated scope ids (default: every scope "
                                     "in the catalog)")
    args = ap.parse_args()
    try:
        catalog = load_catalog(args.catalog)
    except CatalogError as exc:
        raise SystemExit(str(exc))
    scopes = ([s.strip() for s in args.scopes.split(",") if s.strip()]
              if args.scopes else catalog.scope_ids())

    token = httpx.post(
        KEYCLOAK + "/realms/staff/protocol/openid-connect/token", timeout=20,
        data={"grant_type": "password", "client_id": "consent-manager-ui",
              "username": SUBJECT_USER, "password": SUBJECT_PASSWORD, "scope": "openid"},
    ).json()["access_token"]
    H = {"Authorization": "Bearer " + token, "Content-Type": "application/json"}
    subject = SUBJECT_USER

    # ── the partner's binding with the aggregation layer ────────────────────
    rows = httpx.get(CM + "/consent/v1/partners", headers=H, timeout=30).json()
    rows = rows if isinstance(rows, list) else rows.get("items", [])
    match = [p for p in rows if p.get("audience") == AUDIENCE]
    if not match:
        raise SystemExit(
            "No CM binding for audience '%s'. Onboard the partner, then run "
            "scripts/register-aggregator.py --partner %s." % (AUDIENCE, AUDIENCE))
    partner_uuid = match[0]["id"]
    controller = match[0].get("controller_id") or AUDIENCE
    print("[binding ] %s  (%s)" % (partner_uuid, AUDIENCE))

    # ── subject grant, over the SCOPE IDS ───────────────────────────────────
    r = httpx.post(CM + "/consent/v1/consent-requests", headers=H, timeout=30, json={
        "subject_id": {"type": "national_id", "value": subject},
        "partner_id": partner_uuid,
        "purpose": {"code": "loan_origination", "text": "postman aggregation"},
        "requested_scopes": scopes,
    })
    if r.status_code >= 400:
        raise SystemExit("consent-request failed: %s %s" % (r.status_code, r.text[:300]))
    request_id = r.json()["id"]

    now = datetime.now(timezone.utc)
    id_token = (b64u({"alg": "none", "typ": "JWT"}) + "." + b64u({
        "iss": KEYCLOAK + "/realms/staff", "sub": uuid.uuid4().hex,
        "preferred_username": subject, "subject_id_value": subject,
        "amr": ["otp"], "iat": int(now.timestamp()),
        "exp": int(now.timestamp()) + 600}) + ".")
    httpx.post(CM + "/consent/v1/consent-requests/%s/authenticate" % request_id,
               headers=H, timeout=20, json={"id_token": id_token})
    ap_ = httpx.post(CM + "/consent/v1/consent-requests/%s/approve" % request_id,
                     headers=H, timeout=20, json={"granted_scopes": scopes})
    if ap_.status_code >= 400:
        raise SystemExit("approve failed: %s %s" % (ap_.status_code, ap_.text[:300]))
    granted = ap_.json().get("effective_data_scopes") or scopes
    print("[grant   ] %d scope id(s) granted to %s" % (len(granted), AUDIENCE))

    # ── partner key + consent object ────────────────────────────────────────
    kid = "agg-postman-" + uuid.uuid4().hex[:6]
    priv = ec.generate_private_key(ec.SECP256R1())
    pub = priv.public_key().public_bytes(
        serialization.Encoding.PEM,
        serialization.PublicFormat.SubjectPublicKeyInfo).decode()
    x = httpx.post(PM_ADMIN + "/partners/requests/key-update", headers=H, timeout=30,
                   json={"partner_id": PM_PARTNER,
                         "keys": [{"public_key": pub, "kid": kid, "algorithm": "ES256"}]})
    x.raise_for_status()
    httpx.post(PM_ADMIN + "/partners/requests/%s/approve" % x.json()["id"], headers=H,
               timeout=30, json={"notes": "postman aggregation"}).raise_for_status()
    # Kept (gitignored) so scripts/stack-check.py can sign a second object
    # with the same registered key. Dev only.
    (HERE / ".agg-prep-key.pem").write_bytes(priv.private_bytes(
        serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption()))
    served = httpx.get(PM_KEYS + "/keys/%s/%s" % (PM_PARTNER, kid), timeout=20)
    print("[key     ] %s/%s served by PM -> HTTP %s" % (PM_PARTNER, kid, served.status_code))

    now = datetime.now(timezone.utc)
    claims = {
        "@context": "https://openg2p.org/contexts/consent_object.jsonld",
        "@type": "ConsentObject",
        "jti": uuid.uuid4().hex,
        "aud": AUDIENCE,
        "data_controller": controller,
        "subject_id": {"type": "national_id", "value": subject},
        "purpose": {"code": "loan_origination", "text": "postman aggregation"},
        "data_scopes": granted,
        "fetch_type": "oneshot",
        "validity": {"valid_from": now.isoformat(),
                     "valid_until": (now + timedelta(days=1)).isoformat()},
        "issued_at": now.isoformat(),
    }
    consent_jws = PyJWS().encode(canonical(claims), priv, algorithm="ES256",
                                 headers={"kid": kid})

    values = [
        {"key": "access_token", "value": token, "type": "secret"},
        {"key": "agg_url", "value": AGG},
        {"key": "cm_url", "value": CM},
        {"key": "callback_url", "value": CALLBACK},
        {"key": "consent_jws", "value": consent_jws},
        {"key": "partner_uuid", "value": partner_uuid},
        {"key": "partner_audience", "value": AUDIENCE},
        {"key": "subject_id_type", "value": "national_id"},
        {"key": "subject_id_value", "value": subject},
        # The beneficiary is the consent's subject.
        {"key": "foundational_id", "value": subject},
        {"key": "bene360_context", "value": CONTEXT},
        {"key": "scopes", "value": json.dumps(granted)},
        {"key": "kid", "value": kid},
        # filled in by the collection as it runs
        {"key": "aggregation_id", "value": ""},
        {"key": "correlation_id", "value": ""},
        {"key": "otp", "value": ""},
    ]
    env = {
        "id": str(uuid.uuid4()),
        "name": "OpenG2P Aggregation Layer (generated %s)"
                % now.strftime("%Y-%m-%d %H:%M:%SZ"),
        "values": [dict(v, enabled=True, type=v.get("type", "default")) for v in values],
        "_postman_variable_scope": "environment",
        "_postman_exported_at": now.isoformat(),
        "_postman_exported_using": "agg-prep.py",
    }
    # Holds a live token and consent object: written next to the committed
    # template, under a gitignored name.
    out = HERE / "OpenG2P-Aggregator.local.postman_environment.json"
    out.write_text(json.dumps(env, indent=2), encoding="utf8")

    print("\nwrote %s  (%d variables)" % (out, len(values)))
    print("consent object and OTP both expire %s"
          % (now + timedelta(seconds=300)).strftime("%H:%M:%SZ"))
    print("\nIn Postman: import this environment, select it, run the collection in order.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
