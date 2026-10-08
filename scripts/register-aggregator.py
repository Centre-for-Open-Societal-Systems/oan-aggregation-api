"""Onboard the Aggregation Layer on a running stack. Idempotent, re-runnable.

The Aggregation Layer is a partner like any other, with its own identity:

1. Keycloak (staff realm): realm role CONSENT_MANAGER_SERVICE and a confidential
   client ``aggregation-layer`` whose service account holds it. That token is
   what the aggregation layer sends on its calls to the Consent Manager.
2. Its own Ed25519 signing key, written to deploy/keys/aggregation-layer.p12
   (generated once, never overwritten).
3. Partner Management: partner PARTNER_AGGREGATION_LAYER with that key, kid
   ``agg-2026-01``. A registry derives the envelope signer from the DCI header
   as PARTNER_{sender_id.upper()}, so sender_id "aggregation-layer" lands here.
4. CM bindings aggregation layer -> registry (agg-layer-farmer / -livestock /
   -cropsown), policy ceilings in registry block names, approved through AWE
   (only the tasks for these policies; nothing else in the inbox is touched).
5. deploy/.env: the client secret and the CM events HMAC secret filled in.

The partner -> aggregator binding (komal-aggregator, field-alias scopes) is the
same one the in-CM aggregator used and is not changed.

    python scripts/register-aggregator.py         # from a venv with httpx + cryptography
"""
import os
import pathlib
import secrets
import sys
import time

import httpx
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ed25519
from cryptography.hazmat.primitives.serialization import pkcs12

KC = os.environ.get("G2P_KEYCLOAK", "http://localhost:8080")
CM = os.environ.get("G2P_CM", "http://localhost:8000")
PM_ADMIN = os.environ.get("G2P_PM_ADMIN", "http://localhost:8051")
PM_KEYS = os.environ.get("G2P_PM_KEYS", "http://localhost:8050")

REPO = pathlib.Path(__file__).resolve().parent.parent
KEY_DIR = REPO / "deploy" / "keys"
P12 = KEY_DIR / "aggregation-layer.p12"
ENV_FILE = REPO / "deploy" / ".env"
ENV_EXAMPLE = REPO / "deploy" / ".env.example"
CM_ENV = pathlib.Path(os.environ.get(
    "G2P_CM_ENV", REPO.parent / "consent-management" / "backend" / ".env"))

AGG_PM_ID = "PARTNER_AGGREGATION_LAYER"
KID = "agg-2026-01"
CLIENT_ID = "aggregation-layer"
SERVICE_ROLE = "CONSENT_MANAGER_SERVICE"

BINDINGS = [
    # audience, controller_id, scopes (registry block names), label
    ("agg-layer-farmer", "farmer_registry",
     ["farmer_personal_details", "family_details", "farm_details"],
     "Aggregation Layer -> Farmer registry"),
    ("agg-layer-livestock", "livestock_registry",
     ["livestock_details", "animal_details", "health_event_details",
      "vaccination_details", "vital_event_details", "breeding_details"],
     "Aggregation Layer -> Livestock registry"),
    ("agg-layer-cropsown", "cropsown_registry",
     ["crop_sown_details", "crop_production_details", "farm_details",
      "infestation_details", "cluster_details"],
     "Aggregation Layer -> Crop Sown registry"),
]


def step(n, text):
    print("\n" + "=" * 74 + "\nSTEP %s: %s\n" % (n, text) + "=" * 74)


# ── 1. Keycloak ─────────────────────────────────────────────────────────────

def keycloak():
    t = httpx.post(KC + "/realms/master/protocol/openid-connect/token", data={
        "grant_type": "password", "client_id": "admin-cli",
        "username": os.environ.get("G2P_KC_ADMIN", "admin"),
        "password": os.environ.get("G2P_KC_ADMIN_PASSWORD", "admin")}, timeout=20)
    t.raise_for_status()
    H = {"Authorization": "Bearer " + t.json()["access_token"]}
    base = KC + "/admin/realms/staff"

    if httpx.get(base + "/roles/" + SERVICE_ROLE, headers=H).status_code == 404:
        httpx.post(base + "/roles", headers=H, json={
            "name": SERVICE_ROLE,
            "description": "Platform services calling the Consent Manager's service APIs"
        }).raise_for_status()
        print("  realm role %s created" % SERVICE_ROLE)
    role = httpx.get(base + "/roles/" + SERVICE_ROLE, headers=H).json()

    found = httpx.get(base + "/clients", headers=H, params={"clientId": CLIENT_ID}).json()
    if not found:
        httpx.post(base + "/clients", headers=H, json={
            "clientId": CLIENT_ID, "name": "Aggregation Layer", "enabled": True,
            "publicClient": False, "serviceAccountsEnabled": True,
            "standardFlowEnabled": False, "directAccessGrantsEnabled": False,
        }).raise_for_status()
        found = httpx.get(base + "/clients", headers=H, params={"clientId": CLIENT_ID}).json()
        print("  client %s created" % CLIENT_ID)
    cid = found[0]["id"]
    secret = httpx.get(base + "/clients/%s/client-secret" % cid, headers=H).json()["value"]

    sa = httpx.get(base + "/clients/%s/service-account-user" % cid, headers=H).json()
    have = httpx.get(base + "/users/%s/role-mappings/realm" % sa["id"], headers=H).json()
    if not any(r["name"] == SERVICE_ROLE for r in have):
        httpx.post(base + "/users/%s/role-mappings/realm" % sa["id"], headers=H,
                   json=[role]).raise_for_status()
        print("  %s granted to the service account" % SERVICE_ROLE)

    tok = httpx.post(KC + "/realms/staff/protocol/openid-connect/token", data={
        "grant_type": "client_credentials", "client_id": CLIENT_ID,
        "client_secret": secret}, timeout=20)
    tok.raise_for_status()
    print("  client_credentials token OK")
    return secret


# ── 2. signing key ──────────────────────────────────────────────────────────

def signing_key():
    KEY_DIR.mkdir(parents=True, exist_ok=True)
    if P12.exists():
        key, _cert, _ = pkcs12.load_key_and_certificates(P12.read_bytes(), None)
        print("  using existing %s" % P12)
    else:
        key = ed25519.Ed25519PrivateKey.generate()
        P12.write_bytes(pkcs12.serialize_key_and_certificates(
            b"aggregation-layer", key, None, None, serialization.NoEncryption()))
        print("  generated %s" % P12)
    pub = key.public_key().public_bytes(
        serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo).decode()
    (KEY_DIR / "aggregation-layer.pub.pem").write_text(pub)
    return pub.strip()


# ── 3. Partner Management ───────────────────────────────────────────────────

def staff_headers():
    r = httpx.post(KC + "/realms/staff/protocol/openid-connect/token", timeout=20, data={
        "grant_type": "password", "client_id": "consent-manager-ui",
        "username": "staff", "password": "staff", "scope": "openid"})
    r.raise_for_status()
    return {"Authorization": "Bearer " + r.json()["access_token"]}


def pm_register(H, pub_pem):
    key = [{"public_key": pub_pem, "kid": KID, "algorithm": "EdDSA"}]
    if httpx.get(PM_ADMIN + "/partners/" + AGG_PM_ID, headers=H, timeout=20).status_code != 200:
        r = httpx.post(PM_ADMIN + "/partners/requests/onboarding", headers=H, timeout=30, json={
            "partner_id": AGG_PM_ID, "name": "Aggregation Layer", "org_name": "OpenG2P",
            "description": "Signs consent objects and DCI envelopes for the "
                           "cross-registry aggregated fetch.",
            "keys": key})
        if r.status_code >= 400:
            raise SystemExit("PM onboarding failed: %s %s" % (r.status_code, r.text[:400]))
        httpx.post(PM_ADMIN + "/partners/requests/%s/approve" % r.json()["id"], headers=H,
                   timeout=30, json={"notes": "aggregation layer"}).raise_for_status()
        print("  PM partner %s onboarded" % AGG_PM_ID)
    served = httpx.get(PM_KEYS + "/keys/%s" % AGG_PM_ID, timeout=20)
    current = [k for k in (served.json().get("keys", []) if served.status_code == 200 else [])
               if k.get("kid") == KID]
    if not current or current[0].get("public_key", "").strip() != pub_pem:
        x = httpx.post(PM_ADMIN + "/partners/requests/key-update", headers=H, timeout=30,
                       json={"partner_id": AGG_PM_ID, "keys": key})
        if x.status_code >= 400:
            raise SystemExit("key-update failed: %s %s" % (x.status_code, x.text[:400]))
        httpx.post(PM_ADMIN + "/partners/requests/%s/approve" % x.json()["id"], headers=H,
                   timeout=30, json={"notes": "aggregation layer key"}).raise_for_status()
        print("  key %s registered" % KID)
    ok = httpx.get(PM_KEYS + "/keys/%s/%s" % (AGG_PM_ID, KID), timeout=20).status_code == 200
    print("  PM serves %s/%s: %s" % (AGG_PM_ID, KID, "yes" if ok else "NO"))
    if not ok:
        raise SystemExit("registries would reject every hop with signature_invalid")


# ── 4. CM bindings ──────────────────────────────────────────────────────────

def our_tasks(H, policy_id):
    tasks = httpx.get(CM + "/consent/v1/awe/tasks", headers=H, timeout=30).json()
    rows = tasks if isinstance(tasks, list) else tasks.get("items", tasks.get("data", []))
    return [t for t in rows if t.get("artifact_id") == policy_id
            and t.get("status") in ("open", "claimed")]


def binding(H, audience, controller, scopes, label):
    rows = httpx.get(CM + "/consent/v1/partners", headers=H, timeout=30).json()
    match = [p for p in rows if p.get("audience") == audience]
    if match:
        pid = match[0]["id"]
    else:
        r = httpx.post(CM + "/consent/v1/partners", headers=H, timeout=30, json={
            "name": label, "audience": audience, "controller_id": controller,
            "partner_mgmt_id": AGG_PM_ID})
        if r.status_code >= 400:
            raise SystemExit("create %s failed: %s %s" % (audience, r.status_code, r.text[:300]))
        pid = r.json()["id"]
        print("  created %-22s %s" % (audience, pid))

    cur = httpx.get(CM + "/consent/v1/partners/%s/policy" % pid, headers=H, timeout=30)
    cur = cur.json() if cur.status_code == 200 else {}
    if cur.get("status") == "active" and sorted(cur.get("allowed_data_scopes") or []) == sorted(scopes):
        print("  %-22s active v%s" % (audience, cur.get("version")))
        return pid
    p = httpx.put(CM + "/consent/v1/partners/%s/policy" % pid, headers=H, timeout=30, json={
        "allowed_data_scopes": scopes,
        "allowed_purposes": ["loan_origination", "subsidy_verification"],
        "allowed_subject_id_types": ["national_id"],
        "allowed_signing_algs": ["EdDSA"],
        "max_validity_duration": "P1Y", "fetch_type": "oneshot",
        # The subject never meets this binding: its grant is recorded by the CM
        # from what they did on the partner's request (POST /consent/v1/grants).
        "required_auth_method": None, "lawful_basis": "consent"})
    body = p.json() if p.status_code < 300 else {}
    if p.status_code >= 400:
        raise SystemExit("policy %s failed: %s %s" % (audience, p.status_code, p.text[:300]))
    if body.get("status") == "pending":
        for _ in range(15):
            mine = our_tasks(H, body["id"])
            if mine:
                break
            time.sleep(1)
        for t in mine:
            httpx.post(CM + "/consent/v1/awe/tasks/%s/claim" % t["id"], headers=H, timeout=30)
            httpx.post(CM + "/consent/v1/awe/tasks/%s/decision" % t["id"], headers=H,
                       timeout=30, json={"action": "approve",
                                         "comment": "aggregation layer onboarding"})
        for _ in range(30):
            cur = httpx.get(CM + "/consent/v1/partners/%s/policy" % pid, headers=H).json()
            if cur.get("status") == "active" and cur.get("id") == body["id"]:
                break
            time.sleep(1)
    cur = httpx.get(CM + "/consent/v1/partners/%s/policy" % pid, headers=H).json()
    print("  %-22s %s v%s" % (audience, cur.get("status"), cur.get("version")))
    if cur.get("status") != "active":
        raise SystemExit("policy for %s is not active" % audience)
    return pid


# ── 5. env files ────────────────────────────────────────────────────────────

def set_env(path, values):
    lines = path.read_text().splitlines() if path.exists() else []
    seen = set()
    for i, line in enumerate(lines):
        key = line.split("=", 1)[0].strip()
        if key in values:
            lines[i] = "%s=%s" % (key, values[key])
            seen.add(key)
    lines += ["%s=%s" % (k, v) for k, v in values.items() if k not in seen]
    path.write_text("\n".join(lines) + "\n")


def current(path, key):
    if path.exists():
        for line in path.read_text().splitlines():
            if line.startswith(key + "="):
                return line.split("=", 1)[1].strip()
    return ""


def main():
    step(1, "Keycloak client %s with %s" % (CLIENT_ID, SERVICE_ROLE))
    client_secret = keycloak()
    step(2, "Signing key")
    pub = signing_key()
    H = staff_headers()
    step(3, "Partner Management: %s" % AGG_PM_ID)
    pm_register(H, pub)
    step(4, "CM bindings aggregation layer -> registry")
    for audience, controller, scopes, label in BINDINGS:
        binding(H, audience, controller, scopes, label)

    step(5, "deploy/.env and the CM's backend/.env")
    if not ENV_FILE.exists():
        ENV_FILE.write_text(ENV_EXAMPLE.read_text())
    hmac_secret = (current(ENV_FILE, "AGGREGATION_LAYER_CM_EVENTS_HMAC_SECRET")
                   or secrets.token_hex(24))
    set_env(ENV_FILE, {"AGGREGATION_LAYER_CM_CLIENT_SECRET": client_secret,
                       "AGGREGATION_LAYER_CM_EVENTS_HMAC_SECRET": hmac_secret})
    print("  %s updated" % ENV_FILE)
    if CM_ENV.exists():
        set_env(CM_ENV, {
            "CONSENT_MANAGER_AUTH_SERVICE_ROLE": SERVICE_ROLE,
            "CONSENT_MANAGER_AGGREGATION_LAYER_EVENTS_URL":
                "http://aggregation-layer:8100/aggregation/v1/cm-events",
            "CONSENT_MANAGER_AGGREGATION_LAYER_EVENTS_HMAC_SECRET": hmac_secret})
        print("  %s updated (recreate the CM backend to load it)" % CM_ENV)
    else:
        print("  CM env not found at %s - set the events URL + secret by hand" % CM_ENV)
    print("\nDone.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
