"""Onboard the Aggregation Layer on a running stack. Idempotent, re-runnable.

Every registry comes from the registry catalog (deploy/registries.yaml by
default) - this script holds no registry of its own, so onboarding a new
registry is: add it to the catalog, run this again.

The Aggregation Layer is a partner like any other, with its own identity:

1. Keycloak (staff realm): a confidential client ``aggregation-layer`` whose
   service account holds CONSENT_MANAGER_ADMIN. That token is what the
   aggregation layer sends on its calls to the Consent Manager; the CM's
   policy read and partner list accept only the admin role (the CM has no
   narrower service role), so the client must be held as tightly as an admin.
2. Its own Ed25519 signing key, written to deploy/keys/aggregation-layer.p12
   (generated once, never overwritten).
3. Partner Management: partner PARTNER_AGGREGATION_LAYER with that key, kid
   ``agg-2026-01``. A registry derives the envelope signer from the DCI header
   as PARTNER_{sender_id.upper()}, so sender_id "aggregation-layer" lands here.
4. One CM binding aggregation layer -> registry per catalog entry
   (``binding.audience`` / ``binding.controller_id``), policy ceiling = the
   entry's scopes (registry block names), lawful basis
   ``legitimate_interest``, approved through AWE (only the tasks for these
   policies; nothing else in the inbox is touched).

   legitimate_interest means the CM asks for no subject grant on the hop and
   caps it at the binding's ceiling. The subject's consent and OTP are
   enforced by the aggregation layer before it calls a registry (its own
   aggregation_grants table), so these ceilings are the most any single hop
   can ever release - keep the catalog's scopes to the blocks partners need.
   Moving a binding from consent to legitimate_interest is a WIDENING: with
   AWE on it lands ``pending`` and only takes effect once approved (this
   script approves its own tasks). The CM caches a partner's policy for
   partner_cache_ttl_sec (60s by default), so a change can take up to a
   minute to reach /validate.
5. Optional, ``--partner AUDIENCE``: extend an existing partner -> aggregation
   layer binding so its policy also allows the catalog's scope ids
   (``<registryCode>.<scope>``). Scopes are only ever added, never removed,
   and the rest of the policy is kept as it is.
6. deploy/.env: the client secret filled in.

    python scripts/register-aggregator.py                       # every registry
    python scripts/register-aggregator.py --registry NEW_REGISTRY --partner partner-x

Needs a venv with httpx, cryptography, pydantic and pyyaml.
"""
import argparse
import os
import pathlib
import sys
import time

import httpx
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ed25519
from cryptography.hazmat.primitives.serialization import pkcs12

REPO = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "backend" / "src"))
from openg2p_aggregation_layer.registry_catalog import CatalogError, load_catalog  # noqa: E402

KC = os.environ.get("G2P_KEYCLOAK", "http://localhost:8080")
CM = os.environ.get("G2P_CM", "http://localhost:8000")
PM_ADMIN = os.environ.get("G2P_PM_ADMIN", "http://localhost:8051")
PM_KEYS = os.environ.get("G2P_PM_KEYS", "http://localhost:8050")

KEY_DIR = REPO / "deploy" / "keys"
P12 = KEY_DIR / "aggregation-layer.p12"
ENV_FILE = REPO / "deploy" / ".env"
ENV_EXAMPLE = REPO / "deploy" / ".env.example"
CATALOG = REPO / "deploy" / "registries.yaml"

AGG_PM_ID = "PARTNER_AGGREGATION_LAYER"
KID = "agg-2026-01"
CLIENT_ID = "aggregation-layer"
# The CM role its policy read and partner list require. There is no
# narrower one; see the module docstring.
SERVICE_ROLE = "CONSENT_MANAGER_ADMIN"
# Written on a registry binding whose catalog entry names no purposes.
DEFAULT_PURPOSES = ["loan_origination"]
# The policy fields carried over unchanged when a partner binding is extended.
POLICY_FIELDS = ("allowed_data_scopes", "allowed_purposes", "allowed_subject_id_types",
                 "allowed_signing_algs", "max_validity_duration", "fetch_type",
                 "required_auth_method", "lawful_basis")


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
            "description": "Consent Manager administration"
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
        "username": os.environ.get("G2P_STAFF_USER", "staff"),
        "password": os.environ.get("G2P_STAFF_PASSWORD", "staff"), "scope": "openid"})
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


# ── 4/5. CM bindings ────────────────────────────────────────────────────────

def our_tasks(H, policy_id):
    tasks = httpx.get(CM + "/consent/v1/awe/tasks", headers=H, timeout=30).json()
    rows = tasks if isinstance(tasks, list) else tasks.get("items", tasks.get("data", []))
    return [t for t in rows if t.get("artifact_id") == policy_id
            and t.get("status") in ("open", "claimed")]


def partner_by_audience(H, audience):
    rows = httpx.get(CM + "/consent/v1/partners", headers=H, timeout=30).json()
    rows = rows if isinstance(rows, list) else rows.get("items", rows.get("data", []))
    match = [p for p in rows if p.get("audience") == audience]
    return match[0]["id"] if match else None


def current_policy(H, pid):
    cur = httpx.get(CM + "/consent/v1/partners/%s/policy" % pid, headers=H, timeout=30)
    return cur.json() if cur.status_code == 200 else {}


def put_policy(H, pid, audience, policy):
    """Write a policy version and, if AWE parks it, approve this script's own tasks."""
    p = httpx.put(CM + "/consent/v1/partners/%s/policy" % pid, headers=H, timeout=30,
                  json=policy)
    if p.status_code >= 400:
        raise SystemExit("policy %s failed: %s %s" % (audience, p.status_code, p.text[:300]))
    body = p.json()
    if body.get("status") == "pending":
        mine = []
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
            cur = current_policy(H, pid)
            if cur.get("status") == "active" and cur.get("id") == body["id"]:
                break
            time.sleep(1)
    cur = current_policy(H, pid)
    print("  %-26s %s v%s" % (audience, cur.get("status"), cur.get("version")))
    if cur.get("status") != "active":
        raise SystemExit("policy for %s is not active" % audience)


def registry_binding(H, code, entry):
    audience, scopes = entry.binding.audience, sorted(entry.scopes)
    pid = partner_by_audience(H, audience)
    if pid is None:
        r = httpx.post(CM + "/consent/v1/partners", headers=H, timeout=30, json={
            "name": "Aggregation Layer -> %s" % entry.name, "audience": audience,
            "controller_id": entry.binding.controller_id, "partner_mgmt_id": AGG_PM_ID})
        if r.status_code >= 400:
            raise SystemExit("create %s failed: %s %s" % (audience, r.status_code, r.text[:300]))
        pid = r.json()["id"]
        print("  created %-26s %s (%s)" % (audience, pid, code))

    cur = current_policy(H, pid)
    if (cur.get("status") == "active"
            and sorted(cur.get("allowed_data_scopes") or []) == scopes
            and cur.get("lawful_basis") == "legitimate_interest"):
        print("  %-26s active v%s (%s, unchanged)" % (audience, cur.get("version"), code))
        return
    put_policy(H, pid, audience, {
        "allowed_data_scopes": scopes,
        "allowed_purposes": entry.binding.allowed_purposes or DEFAULT_PURPOSES,
        "allowed_subject_id_types": ["national_id"],
        "allowed_signing_algs": ["EdDSA"],
        "max_validity_duration": "P1Y", "fetch_type": "oneshot",
        # The subject never meets this binding. The CM seeks no grant on the
        # hop; the aggregation layer enforces the subject's consent + OTP
        # before calling (no auth method may be set under this basis).
        "required_auth_method": None, "lawful_basis": "legitimate_interest"})


def partner_binding(H, audience, scope_ids):
    """Let an existing partner -> aggregation layer binding ask for these scopes."""
    pid = partner_by_audience(H, audience)
    if pid is None:
        raise SystemExit("no CM binding for partner audience '%s' - onboard the partner "
                         "first" % audience)
    cur = current_policy(H, pid)
    if not cur:
        raise SystemExit("partner '%s' has no policy to extend" % audience)
    have = cur.get("allowed_data_scopes") or []
    missing = [s for s in scope_ids if s not in have]
    if not missing:
        print("  %-26s already allows all %d scope id(s)" % (audience, len(scope_ids)))
        return
    policy = {k: cur.get(k) for k in POLICY_FIELDS if k in cur}
    policy["allowed_data_scopes"] = have + missing
    print("  %-26s + %s" % (audience, ", ".join(missing)))
    put_policy(H, pid, audience, policy)


# ── 6. env files ────────────────────────────────────────────────────────────

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


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--catalog", default=str(CATALOG), help="registry catalog YAML")
    ap.add_argument("--registry", action="append", default=[],
                    help="only this registry code (repeatable; default: all)")
    ap.add_argument("--partner", action="append", default=[],
                    help="partner audience whose binding gets the scope ids (repeatable)")
    args = ap.parse_args()

    try:
        catalog = load_catalog(args.catalog)
    except CatalogError as exc:
        raise SystemExit(str(exc))
    unknown = [c for c in args.registry if catalog.get(c) is None]
    if unknown:
        raise SystemExit("not in %s: %s" % (args.catalog, ", ".join(unknown)))
    codes = args.registry or catalog.codes()

    step(1, "Keycloak client %s with %s" % (CLIENT_ID, SERVICE_ROLE))
    client_secret = keycloak()
    step(2, "Signing key")
    pub = signing_key()
    H = staff_headers()
    step(3, "Partner Management: %s" % AGG_PM_ID)
    pm_register(H, pub)
    step(4, "CM bindings aggregation layer -> registry (%s)" % ", ".join(codes))
    for code in codes:
        registry_binding(H, code, catalog.get(code))
    scope_ids = catalog.scope_ids(codes)
    if args.partner:
        step(5, "Partner bindings: allow the scope ids")
        for audience in args.partner:
            partner_binding(H, audience, scope_ids)

    step(6, "deploy/.env")
    if not ENV_FILE.exists():
        ENV_FILE.write_text(ENV_EXAMPLE.read_text())
    set_env(ENV_FILE, {"AGGREGATION_LAYER_CM_CLIENT_SECRET": client_secret})
    print("  %s updated" % ENV_FILE)
    # Nothing is written to the CM's configuration: it needs none for this
    # service. It does need subject_consent_required=true for the
    # raise-a-consent path (see docs/CM-API-CONTRACT.md).
    print("\nScope ids partners consent to for these registries:")
    for scope_id in scope_ids:
        print("  " + scope_id)
    print("\nDone. The CM caches policies for up to 60s; allow that before the first seek.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
