"""Generate the Postman environment for the aggregator collection.

Postman's sandbox has no ECDSA, so it cannot mint the partner's consent object
- the same reason the main project has gen-postman-env.py. This does the parts
Postman cannot:

  1. registers a fresh partner signing key in Partner Management
  2. walks the subject through request -> authenticate -> approve so the
     aggregator binding has an active grant for the requested FIELD ALIASES
  3. signs the partner's consent object for aud=komal-aggregator
  4. writes everything into OpenG2P-Aggregator.postman_environment.json

Run it from the host (all services are reached on their published ports):

    python postman/agg-prep.py
    python postman/agg-prep.py --fields farmer.firstname,livestock.UIN

Then import the environment into Postman, select it, and run the collection
top to bottom.

TWO THINGS EXPIRE, and they are the usual cause of a confusing failure:

  * the consent object    - CM enforces replay_freshness_window_sec = 300
  * the OTP               - otp_ttl_sec, also 300 by default

so re-run this if a seek starts coming back `replay`.

One more trap worth knowing: the Consent Manager caches Partner Management keys
for 300s. This script registers a NEW kid each run, so if the aggregator process
has cached that partner's keys within the last five minutes the seek fails with
`signature_invalid`. Restart the aggregator (./run-local.sh) or wait it out.
"""
import argparse
import json
import os
import sys
import uuid
from datetime import datetime, timedelta, timezone

import httpx
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec
from jwt.api_jws import PyJWS

HERE = os.path.dirname(os.path.abspath(__file__))

KEYCLOAK = os.environ.get("G2P_KEYCLOAK", "http://localhost:8080")
CM = os.environ.get("G2P_CM", "http://localhost:8000")          # original backend
AGG = os.environ.get("G2P_AGG", "http://localhost:8110")        # aggregator backend
PM_ADMIN = os.environ.get("G2P_PM_ADMIN", "http://localhost:8051")
PM_KEYS = os.environ.get("G2P_PM_KEYS", "http://localhost:8050")
CALLBACK = os.environ.get("G2P_CALLBACK", "http://localhost:9099/on-search")

AUDIENCE = "komal-aggregator"
CONTROLLER = "consent-manager-aggregator"
PM_PARTNER = "PARTNER_KC"
SUBJECT = "staff"

DEFAULT_FIELDS = [
    "farmer.firstname", "farmer.lastname", "farmer.UIN",
    "livestock.UIN", "livestock.animal_details",
    "cropsown.crop_sown_details",
]
# Each registry keys on its own functional id; one value cannot match all three.
REGISTRY_QUERIES = {
    "farmer": "IND-0085",
    "livestock": "LS-000000000001",
    "cropsown": "REG/S1/2026/00001",
}


def canonical(obj) -> bytes:
    return json.dumps(obj, sort_keys=True, separators=(",", ":"),
                      ensure_ascii=False).encode("utf-8")


def b64u(obj) -> str:
    import base64
    return base64.urlsafe_b64encode(
        json.dumps(obj).encode()).decode().rstrip("=")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--fields", help="comma-separated catalog aliases")
    args = ap.parse_args()
    fields = ([f.strip() for f in args.fields.split(",") if f.strip()]
              if args.fields else list(DEFAULT_FIELDS))

    token = httpx.post(
        KEYCLOAK + "/realms/staff/protocol/openid-connect/token", timeout=20,
        data={"grant_type": "password", "client_id": "consent-manager-ui",
              "username": "staff", "password": "staff", "scope": "openid"},
    ).json()["access_token"]
    H = {"Authorization": "Bearer " + token, "Content-Type": "application/json"}

    # ── the aggregator binding ──────────────────────────────────────────────
    rows = httpx.get(CM + "/consent/v1/partners", headers=H, timeout=30).json()
    rows = rows if isinstance(rows, list) else rows.get("items", [])
    match = [p for p in rows if p.get("audience") == AUDIENCE]
    if not match:
        raise SystemExit(
            "No CM binding for audience '%s'. Run register-aggregator.py first."
            % AUDIENCE)
    partner_uuid = match[0]["id"]
    print("[binding ] %s  (%s)" % (partner_uuid, AUDIENCE))

    # ── subject grant, over the FIELD ALIASES ───────────────────────────────
    r = httpx.post(CM + "/consent/v1/consent-requests", headers=H, timeout=30, json={
        "subject_id": {"type": "national_id", "value": SUBJECT},
        "partner_id": partner_uuid,
        "purpose": {"code": "loan_origination", "text": "postman aggregator"},
        "requested_scopes": fields,
    })
    if r.status_code >= 400:
        raise SystemExit("consent-request failed: %s %s" % (r.status_code, r.text[:300]))
    request_id = r.json()["id"]

    now = datetime.now(timezone.utc)
    id_token = (b64u({"alg": "none", "typ": "JWT"}) + "." + b64u({
        "iss": KEYCLOAK + "/realms/staff", "sub": uuid.uuid4().hex,
        "preferred_username": SUBJECT, "subject_id_value": SUBJECT,
        "amr": ["otp"], "iat": int(now.timestamp()),
        "exp": int(now.timestamp()) + 600}) + ".")
    httpx.post(CM + "/consent/v1/consent-requests/%s/authenticate" % request_id,
               headers=H, timeout=20, json={"id_token": id_token})
    ap_ = httpx.post(CM + "/consent/v1/consent-requests/%s/approve" % request_id,
                     headers=H, timeout=20, json={"granted_scopes": fields})
    if ap_.status_code >= 400:
        raise SystemExit("approve failed: %s %s" % (ap_.status_code, ap_.text[:300]))
    granted = ap_.json().get("effective_data_scopes") or fields
    print("[grant   ] %d field(s) granted to the aggregator binding" % len(granted))

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
               timeout=30, json={"notes": "postman aggregator"}).raise_for_status()
    # Kept (gitignored) so scripts/stack-check.py can sign a second object
    # with the same registered key. Dev only.
    with open(os.path.join(HERE, ".agg-prep-key.pem"), "wb") as fh:
        fh.write(priv.private_bytes(serialization.Encoding.PEM,
                                    serialization.PrivateFormat.PKCS8,
                                    serialization.NoEncryption()))
    served = httpx.get(PM_KEYS + "/keys/%s/%s" % (PM_PARTNER, kid), timeout=20)
    print("[key     ] %s/%s served by PM -> HTTP %s" % (PM_PARTNER, kid, served.status_code))

    now = datetime.now(timezone.utc)
    claims = {
        "@context": "https://openg2p.org/contexts/consent_object.jsonld",
        "@type": "ConsentObject",
        "jti": uuid.uuid4().hex,
        "aud": AUDIENCE,
        "data_controller": CONTROLLER,
        "subject_id": {"type": "national_id", "value": SUBJECT},
        "purpose": {"code": "loan_origination", "text": "postman aggregator"},
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
        {"key": "pm_admin_url", "value": PM_ADMIN},
        {"key": "callback_url", "value": CALLBACK},
        {"key": "consent_jws", "value": consent_jws},
        {"key": "partner_uuid", "value": partner_uuid},
        {"key": "partner_audience", "value": AUDIENCE},
        {"key": "subject_id_type", "value": "national_id"},
        {"key": "subject_id_value", "value": SUBJECT},
        {"key": "fields", "value": json.dumps(granted)},
        {"key": "registry_queries", "value": json.dumps(
            {k: v for k, v in REGISTRY_QUERIES.items() if k != "farmer"})},
        {"key": "query_id_value", "value": REGISTRY_QUERIES["farmer"]},
        {"key": "kid", "value": kid},
        # filled in by the collection as it runs
        {"key": "aggregation_id", "value": ""},
        {"key": "correlation_id", "value": ""},
        {"key": "otp", "value": ""},
    ]
    env = {
        "id": str(uuid.uuid4()),
        "name": "OpenG2P Aggregator (generated %s)" % now.strftime("%Y-%m-%d %H:%M:%SZ"),
        "values": [dict(v, enabled=True, type=v.get("type", "default")) for v in values],
        "_postman_variable_scope": "environment",
        "_postman_exported_at": now.isoformat(),
        "_postman_exported_using": "agg-prep.py",
    }
    out = os.path.join(HERE, "OpenG2P-Aggregator.postman_environment.json")
    with open(out, "w", encoding="utf8") as f:
        json.dump(env, f, indent=2)

    print("\nwrote %s  (%d variables)" % (out, len(values)))
    print("consent object and OTP both expire %s"
          % (now + timedelta(seconds=300)).strftime("%H:%M:%SZ"))
    print("\nIn Postman: import this environment, select it, run the collection in order.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
