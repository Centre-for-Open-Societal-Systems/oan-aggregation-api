#!/usr/bin/env python3
"""Test an Agri Stack install end to end, as a partner (e.g. a bank) would use it.

One script, driven by a configuration file that names the install: the Exchange,
each registry and its department, the hostnames, the admin credentials, the test
partner and farmer. Nothing about namespaces, release or service names is fixed in
the code; the reference topology's file is scripts/test_install.yaml.

  python scripts/test_install.py --config scripts/test_install.yaml --dry-run
  python scripts/test_install.py --config scripts/test_install.yaml

It tests the install only. In production, partners, policies and keys are managed by
the administrators in Partner Management.

Steps
  1. Install     the Composite publishes the use case and its public key; each
                 department's Partner Management (PM) serves the Composite's key
                 (onboarded at install). Read only.
  2. Departments each department's scope policy for the Composite covers what the use
                 case reads from its registry (the department's decision in production;
                 the test raises and approves it, or waits for an administrator).
  3. Partner key the test partner's signing key (EC P-256), kept in the state directory.
  4. Partner     the partner onboarded in the Exchange's PM with its policy for the use
                 case (raised, then approved by the test or by an administrator).
  5. Portal user optional: a partner portal user for the partner (Keycloak realm
                 `partner`), its password kept in a Kubernetes Secret.
  6. Query       a consent (signed by the partner, or one the Exchange CM holds), the
                 signed query to the Composite, and the response signature checked
                 against the Composite's JWKS, as a partner does.

Admin credentials are read from the cluster with kubectl (namespace and Secret per the
configuration), or from environment variables named there; they are never printed.

Exit codes: 0 ok, 1 error, 2 a step was not completed (timed out, refused, or a
decision is needed), 3 the query failed or a source did not answer ok.
Needs: pip install -r scripts/requirements.txt
"""

from __future__ import annotations

import argparse
import base64
import json
import os
import re
import secrets
import subprocess
import sys
import time
import uuid
from datetime import datetime, timedelta, timezone
from typing import Dict, List, Optional

import httpx
import yaml
from cryptography import x509
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec
from jwt import PyJWS

HERE = os.path.dirname(os.path.abspath(__file__))
OUT_DIR = os.path.join(HERE, "out")
MANAGED_BY = "agri-stack-test-install"

EXIT_OK, EXIT_ERROR, EXIT_DECISION, EXIT_CALL_FAILED = 0, 1, 2, 3
SIGNING_ALG = "ES256"  # the test partner's key is EC P-256
POLICY_MAX_VALIDITY = "P90D"
CONSENT_VALID_DAYS = 1  # the test consent runs for a day; a policy must allow at least that
# Longest time the Composite and the Exchange CM may serve a cached PM answer from before
# this run's change (their policy cache TTL, 60 s by default), plus a margin.
CACHE_WAIT = 90


class InstallTestError(Exception):
    def __init__(self, message: str, code: int = EXIT_ERROR):
        super().__init__(message)
        self.code = code


# ── output ────────────────────────────────────────────────────────────────────

def step(title: str):
    print(f"\n== {title}", file=sys.stderr, flush=True)


def info(msg: str = ""):
    print(f"   {msg}", file=sys.stderr, flush=True)


def warn(msg: str):
    print("   ! " + str(msg).replace("\n", "\n     "), file=sys.stderr, flush=True)


def mask(value) -> str:
    value = str(value or "")
    return ("…" + value[-4:]) if len(value) > 4 else "…"


# ── configuration ─────────────────────────────────────────────────────────────

def _get(d: dict, path: str, default=None):
    for part in path.split("."):
        if not isinstance(d, dict) or part not in d or d[part] in (None, ""):
            return default
        d = d[part]
    return d


class Secret:
    """A credential: an environment variable if set, else a key of a Kubernetes Secret."""

    def __init__(self, cfg: dict, namespace: str, label: str):
        cfg = cfg or {}
        self.env = cfg.get("env") or ""
        self.name = cfg.get("secret") or ""
        self.key = cfg.get("key") or ""
        self.namespace = cfg.get("namespace") or namespace
        self.label = label

    def describe(self) -> str:
        where = []
        if self.env:
            where.append(f"${self.env}")
        if self.name:
            where.append(f"Secret {self.namespace}/{self.name} key {self.key}")
        return " or ".join(where) or "not configured"

    def read(self, kubectl: List[str]) -> str:
        if self.env and os.environ.get(self.env):
            return os.environ[self.env]
        if not self.name:
            raise InstallTestError(f"{self.label}: no credential configured (set env, or secret and key)")
        cmd = kubectl + ["-n", self.namespace, "get", "secret", self.name, "-o", f"jsonpath={{.data.{_jp(self.key)}}}"]
        r = subprocess.run(cmd, capture_output=True, text=True)
        if r.returncode != 0 or not r.stdout.strip():
            raise InstallTestError(f"{self.label}: cannot read {self.describe()}: {(r.stderr or 'empty').strip()[:200]}")
        return base64.b64decode(r.stdout.strip()).decode()


def _jp(key: str) -> str:
    """A Secret key in a kubectl jsonpath (dots escaped)."""
    return key.replace(".", "\\.")


class Site:
    """One install: the Exchange, or a department (its registry's commons)."""

    def __init__(self, label: str, cfg: dict):
        self.label = label
        self.namespace = cfg.get("namespace") or ""
        self.domain = cfg.get("domain") or ""
        pm = cfg.get("partner_management") or {}

        def url(value, host):
            value = value or (f"https://{host}.{self.domain}" if self.domain else "")
            if not value:
                raise InstallTestError(f"{label}: no domain, and no URL for {host}")
            return value.rstrip("/")

        self.pm_staff = url(pm.get("staff_api_url"), "pm-staff-portal")
        self.pm_partner = url(pm.get("partner_api_url"), "partner-management-partner-api")
        self.pm_portal = url(pm.get("portal_url"), "partner-management")
        self.token_url = url(pm.get("token_url"), "keycloak") + (
            "" if pm.get("token_url") else "/realms/staff/protocol/openid-connect/token")
        self.client_id = pm.get("client_id") or "commons-services-staff-portal"
        self.client_secret = Secret(pm.get("client_secret") or {"secret": self.client_id, "key": "client_secret"},
                                    self.namespace, f"{label} PM admin client secret")


class Config:
    def __init__(self, path: str):
        with open(path) as fh:
            raw = yaml.safe_load(fh) or {}
        self.path = path
        self.kube_context = raw.get("kube_context") or ""
        ex = raw.get("exchange") or {}
        self.exchange = Site("Exchange", ex)
        comp = ex.get("composite") or {}
        self.composite_url = (comp.get("url") or f"https://agri-composite.{self.exchange.domain}").rstrip("/")
        self.composite_id = comp.get("partner_id") or "agri-composite"
        portal = ex.get("partner_portal") or {}
        self.portal = {
            "enabled": bool(portal.get("enabled")),
            "keycloak_url": (portal.get("keycloak_url") or f"https://keycloak.{self.exchange.domain}").rstrip("/"),
            "realm": portal.get("realm") or "partner",
            "client": portal.get("client") or "consent-partner-portal",
            "url": (portal.get("url") or f"https://consent-partner-portal.{self.exchange.domain}").rstrip("/"),
            "admin_user": portal.get("admin_user") or "admin",
            "admin_password": Secret(portal.get("admin_password") or {"secret": "commons-keycloak",
                                                                       "key": "admin-password"},
                                     self.exchange.namespace, "Keycloak admin password"),
            "role": portal.get("role") or "PARTNER_OPERATOR",
        }
        self.registries: Dict[str, Site] = {}
        for r in raw.get("registries") or []:
            if not r.get("controller"):
                raise InstallTestError(f"{path}: each registry needs a controller (its data controller ID)")
            self.registries[r["controller"]] = Site(f"Department {r['controller']}", r)
        t = raw.get("test") or {}
        self.partner = t.get("partner") or "bank-a"
        self.use_case = t.get("use_case") or "loan-profile"
        subject = t.get("subject") or {}
        self.subject = {"type": subject.get("type") or "FAYDA_FAN", "value": str(subject.get("value") or "")}
        if not self.subject["value"]:
            raise InstallTestError(f"{path}: test.subject.value (the farmer's ID) is required")
        self.parameters = t.get("parameters") or {}
        consent = str(t.get("consent") or "sign")
        if consent not in ("sign", "lookup") and not consent.startswith("id:"):
            raise InstallTestError(f"{path}: test.consent is sign, lookup or id:<consent ID>")
        self.consent = consent
        self.subject_id_types = t.get("subject_id_types") or ["FAYDA_FAN", "FARMER_ID"]
        self.auto_approve = bool(t.get("auto_approve", True))
        self.wait = int(t.get("wait_seconds") or 1800)
        self.poll = int(t.get("poll_seconds") or 10)
        self.state_dir = os.path.expanduser(t.get("state_dir") or os.path.join(
            "~", ".agri-stack-test", self.exchange.domain or "default"))
        self.insecure = bool(t.get("insecure_tls"))


# ── signing and policies ──────────────────────────────────────────────────────

def pm_reference(partner_id: str) -> str:
    """The partner's ID in PM: PARTNER_<ID>, upper-cased, '-' → '_' (e.g. PARTNER_BANK_A)."""
    return f"PARTNER_{partner_id.replace('-', '_').upper()}"


def canonical(obj) -> bytes:
    # Same bytes as the services' orjson OPT_SORT_KEYS: compact, sorted, UTF-8.
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()


def b64u(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).decode().rstrip("=")


def public_pem(key) -> str:
    return key.public_key().public_bytes(serialization.Encoding.PEM,
                                         serialization.PublicFormat.SubjectPublicKeyInfo).decode()


def load_public(pem: str):
    data = pem.encode() if isinstance(pem, str) else pem
    if b"BEGIN CERTIFICATE" in data:
        return x509.load_pem_x509_certificate(data).public_key()
    return serialization.load_pem_public_key(data)


def same_public_key(pem_a: Optional[str], pem_b: Optional[str]) -> bool:
    def spki(pem):
        return load_public(pem).public_bytes(serialization.Encoding.DER,
                                             serialization.PublicFormat.SubjectPublicKeyInfo)
    try:
        return bool(pem_a and pem_b) and spki(pem_a) == spki(pem_b)
    except Exception:
        return False


def jwk_public_pem(jwk: dict) -> Optional[str]:
    from jwt.algorithms import ECAlgorithm, OKPAlgorithm, RSAAlgorithm
    algo = {"EC": ECAlgorithm, "RSA": RSAAlgorithm, "OKP": OKPAlgorithm}.get(jwk.get("kty"))
    if not algo:
        return None
    pub = algo.from_jwk(json.dumps(jwk))
    return pub.public_bytes(serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo).decode()


def utc_ts(now=None) -> str:
    """DCI header timestamp, e.g. 2026-10-01T10:00:00.000Z."""
    now = now or datetime.now(timezone.utc)
    return now.isoformat(timespec="milliseconds").replace("+00:00", "Z")


def new_kid(owner: str) -> str:
    return f"{owner}-test-{datetime.now(timezone.utc):%Y%m%d}-{secrets.token_hex(2)}"


def write_private(path: str, data: bytes):
    os.makedirs(os.path.dirname(path), mode=0o700, exist_ok=True)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "wb") as fh:
        fh.write(data)
    os.chmod(path, 0o600)


def policy_input(*, data_controller=None, use_case=None, scopes=(), purposes=(), subject_id_types=(),
                 allow_partner_consents=False) -> dict:
    """PM PolicyInput: exactly one of data_controller (scope policy) or use_case (name@major).

    A use-case policy lists no purposes (granting a use case grants its purpose; PM refuses them)."""
    if use_case and purposes:
        raise ValueError("a use-case policy takes no purposes")
    body = {"data_controller": data_controller, "use_case": use_case, "allowed_data_scopes": sorted(set(scopes)),
            "allowed_subject_id_types": list(subject_id_types),
            "allowed_signing_algs": [SIGNING_ALG], "max_validity_duration": POLICY_MAX_VALIDITY,
            "fetch_type": "oneshot", "allow_partner_consents": allow_partner_consents}
    if not use_case:
        body["allowed_purposes"] = sorted(set(purposes))
    return body


def policy_key(policy: dict) -> tuple:
    if policy.get("data_controller"):
        return ("data_controller", policy["data_controller"])
    return ("use_case", policy.get("use_case") or None)


def policy_label(policy: dict) -> str:
    field, value = policy_key(policy)
    return f"scope policy {value}" if field == "data_controller" else f"use-case policy {value}"


def describe_policy(policy: dict) -> str:
    what = (f"scopes {policy.get('allowed_data_scopes')}, purposes {policy.get('allowed_purposes')}, "
            if policy.get("data_controller") else "")
    return f"{policy_label(policy)}: {what}ID types {policy.get('allowed_subject_id_types')}"


_DURATION = re.compile(r"^P(?:(\d+)Y)?(?:(\d+)M)?(?:(\d+)W)?(?:(\d+)D)?$")


def duration_days(value: Optional[str]) -> Optional[float]:
    """Days in a simple ISO-8601 date duration (PnYnMnWnD); None = no cap or not parsable."""
    m = _DURATION.match(value or "")
    if not value or not m:
        return None
    y, mo, w, d = (int(x or 0) for x in m.groups())
    return y * 365 + mo * 30 + w * 7 + d


def policy_covers(policy: Optional[dict], needed: dict, subject_type: str) -> bool:
    """The (active) policy allows everything `needed` (a PolicyInput) asks for."""
    if not policy or policy.get("status", "active") != "active" or policy_key(policy) != policy_key(needed):
        return False
    if not set(needed.get("allowed_data_scopes") or []) <= set(policy.get("allowed_data_scopes") or []):
        return False
    if needed.get("allow_partner_consents") and not policy.get("allow_partner_consents"):
        return False
    for field, values in (("allowed_purposes", needed.get("allowed_purposes") or []),
                          ("allowed_subject_id_types", [subject_type]),
                          ("allowed_signing_algs", needed["allowed_signing_algs"])):
        allowed = policy.get(field) or []
        if allowed and not set(values) <= set(allowed):
            return False
    cap = duration_days(policy.get("max_validity_duration"))
    return cap is None or cap >= CONSENT_VALID_DAYS


def merged_policy(active: Optional[dict], needed: dict) -> dict:
    """The needed policy, widened by what the active policy already allows (never narrows)."""
    if not active:
        return dict(needed)
    out = dict(needed)
    for field in ("allowed_data_scopes", "allowed_purposes", "allowed_subject_id_types", "allowed_signing_algs"):
        if field == "allowed_purposes" and out.get("use_case"):
            continue  # a use-case policy lists no purposes
        out[field] = sorted(set(needed.get(field) or []) | set(active.get(field) or []))
    out["allow_partner_consents"] = bool(needed.get("allow_partner_consents") or active.get("allow_partner_consents"))
    old, new = duration_days(active.get("max_validity_duration")), duration_days(needed.get("max_validity_duration"))
    if old is not None and new is not None and old > new:
        out["max_validity_duration"] = active["max_validity_duration"]
    return out


def plan_policies(*, needed: List[dict], active: List[dict], pending: List[dict], subject_type: str) -> dict:
    """What a partner still needs in PM for the `needed` policies.

    Returns {"request": [PolicyInput to raise in one policy request, widened, never
    narrowed], "approve": [open request IDs that already carry it], "ok": [covered keys],
    "conflicts": [str]}.
    """
    request, approve, ok, conflicts = [], [], [], []
    for body in needed:
        key = policy_key(body)
        current = next((p for p in active if policy_key(p) == key and p.get("status", "active") == "active"), None)
        if policy_covers(current, body, subject_type):
            ok.append(key[1])
            continue
        open_req = proposal = None
        for r in pending:
            proposal = next((p for p in r.get("proposed_policies") or [] if policy_key(p) == key), None)
            if proposal is not None:
                open_req = r
                break
        if open_req is not None:
            if policy_covers({**proposal, "status": "active"}, body, subject_type):
                if open_req["id"] not in approve:
                    approve.append(open_req["id"])
            else:
                conflicts.append(f"{policy_label(body)}: open {open_req.get('request_type')} request {open_req['id']} "
                                 "proposes a policy that does not cover the test; approve or reject it in Partner "
                                 "Management, then rerun")
            continue
        request.append(merged_policy(current, body))
    return {"request": request, "approve": approve, "ok": ok, "conflicts": conflicts}


def plan_partner(*, ref: str, kid: str, pub_pem: str, served: List[dict], partner: Optional[dict],
                 pending: List[dict]) -> dict:
    """What PM needs so that `ref` serves (kid, pub_pem).

    Returns {"state": "ok"|"actions"|"conflict", "actions": [...], "reason": str}; actions:
    {"op": "onboard"} | {"op": "key_update"} | {"op": "approve", "request_id"} | {"op": "enable"}.
    """
    for k in served:
        if k.get("kid") == kid:
            if same_public_key(k.get("public_key"), pub_pem):
                return {"state": "ok", "actions": [], "reason": f"PM serves kid {kid}"}
            return {"state": "conflict", "actions": [],
                    "reason": f"PM serves kid {kid} for {ref} with a different public key; rerun with --new-key"}
    if partner is None:
        return {"state": "actions", "actions": [{"op": "onboard"}], "reason": f"{ref} is not in PM"}
    status = partner.get("status")
    mine = [r for r in pending if kid in [(k or {}).get("kid") for k in r.get("proposed_keys") or []]]
    actions: List[dict] = [{"op": "enable"}] if status == "disabled" else []
    if mine:
        actions.append({"op": "approve", "request_id": mine[0]["id"]})
        return {"state": "actions", "actions": actions,
                "reason": f"a {mine[0].get('request_type')} request with kid {kid} is open"}
    if status == "created":
        return {"state": "conflict", "actions": [],
                "reason": f"{ref} is onboarded but never approved; approve or reject its onboarding in PM, then rerun"}
    actions.append({"op": "key_update"})
    return {"state": "actions", "actions": actions, "reason": f"{ref} is {status} without kid {kid}"}


def make_consent(key, *, partner: str, kid: str, subject: dict, purpose: str, scopes: Dict[str, List[str]],
                 valid_days: float = CONSENT_VALID_DAYS, now=None) -> str:
    """A partner-signed consent (compact JWS) with one grant per registry."""
    now = now or datetime.now(timezone.utc)
    claims = {
        "jti": str(uuid.uuid4()), "aud": partner, "subject_id": dict(subject), "purpose": {"code": purpose},
        "grants": [{"data_controller": c, "data_scopes": list(s)} for c, s in scopes.items() if s],
        "fetch_type": "oneshot",
        "validity": {"valid_from": now.isoformat(timespec="seconds"),
                     "valid_until": (now + timedelta(days=valid_days)).isoformat(timespec="seconds")},
        "issued_at": now.isoformat(timespec="seconds"),
    }
    return PyJWS().encode(canonical(claims), key, algorithm=SIGNING_ALG, headers={"kid": kid})


def sign_detached(payload: dict, key, kid: str) -> str:
    """Detached JWS (header..signature) over the canonical JSON of payload."""
    p1, _p2, p3 = PyJWS().encode(canonical(payload), key, algorithm=SIGNING_ALG, headers={"kid": kid}).split(".")
    return f"{p1}..{p3}"


def build_query(key, *, partner: str, kid: str, composite: str, subject: dict, parameters=None,
                consent_jws=None, consent_id=None, now=None) -> dict:
    """The signed request a partner posts to .../use-cases/{use_case}/query."""
    header = {"version": "1.0.0", "message_id": str(uuid.uuid4()), "message_ts": utc_ts(now),
              "action": "query", "sender_id": partner, "receiver_id": composite}
    message = {"subject": dict(subject), "parameters": dict(parameters or {})}
    if consent_jws:
        message["consent_jws"] = consent_jws
    if consent_id:
        message["consent_id"] = consent_id
    return {"signature": sign_detached({"header": header, "message": message}, key, kid),
            "header": header, "message": message}


def jws_header(jws: str) -> dict:
    first = (jws or "").split(".")[0]
    try:
        return json.loads(base64.urlsafe_b64decode(first + "=" * (-len(first) % 4)))
    except ValueError:
        return {}


def verify_response(body, public_key) -> str:
    """'valid', 'MISSING' or 'INVALID (...)' for the Composite's detached signature."""
    parts = ((body or {}).get("signature") or "").split(".")
    if len(parts) != 3:
        return "MISSING"
    full = f"{parts[0]}.{b64u(canonical({'header': body['header'], 'message': body['message']}))}.{parts[2]}"
    try:
        PyJWS().decode(full, public_key, algorithms=["ES256", "RS256", "EdDSA"])
        return "valid"
    except Exception as e:
        return f"INVALID ({e})"


# ── the run ───────────────────────────────────────────────────────────────────

def _detail(r: httpx.Response) -> str:
    return f"HTTP {r.status_code} {r.request.method} {r.request.url} → {(r.text or '')[:300]}"


def check(r: httpx.Response, what: str, ok=(200, 201)) -> httpx.Response:
    if r.status_code in (401, 403):
        raise InstallTestError(f"{what}: {_detail(r)} — the admin client lacks the role, or its token is not accepted")
    if r.status_code not in ok:
        raise InstallTestError(f"{what}: {_detail(r)}")
    return r


class Run:
    def __init__(self, c: Config, args):
        self.c = c
        self.dry = args.dry_run
        self.new_key = args.new_key
        self.out_dir = args.out_dir
        self.print_response = args.print
        self.http = httpx.Client(timeout=30, verify=not c.insecure)
        self.kubectl = ["kubectl"] + (["--context", c.kube_context] if c.kube_context else [])
        self.ref = pm_reference(c.partner)
        self.composite_ref = pm_reference(c.composite_id)
        self._tokens: Dict[str, dict] = {}
        # Set when this run changed a policy or key in PM: the Composite and the Exchange CM
        # may still hold the old answer in their caches.
        self.changed = False

    def close(self):
        self.http.close()

    # helpers -----------------------------------------------------------------
    def admin(self, site: Site) -> dict:
        """Headers with a PM admin token for the site (client credentials of its staff realm)."""
        if site.label not in self._tokens:
            secret = site.client_secret.read(self.kubectl)
            r = self.http.post(site.token_url, data={"grant_type": "client_credentials",
                                                     "client_id": site.client_id, "client_secret": secret})
            if r.status_code != 200:
                raise InstallTestError(f"{site.label}: PM admin token for client {site.client_id} at {site.token_url}: "
                                f"HTTP {r.status_code}")
            self._tokens[site.label] = {"Authorization": f"Bearer {r.json()['access_token']}"}
        return self._tokens[site.label]

    def served_keys(self, site: Site, ref: str) -> List[dict]:
        r = self.http.get(f"{site.pm_partner}/keys/{ref}")
        if r.status_code == 404:
            return []
        return check(r, f"{site.label} PM GET /keys/{ref}").json().get("keys") or []

    def served_policies(self, site: Site, ref: str, **params) -> List[dict]:
        r = self.http.get(f"{site.pm_partner}/policies/{ref}", params=params)
        if r.status_code == 404:
            return []
        return check(r, f"{site.label} PM GET /policies/{ref}").json().get("policies") or []

    def pm_state(self, site: Site, ref: str):
        h = self.admin(site)
        r = self.http.get(f"{site.pm_staff}/partners/{ref}", headers=h)
        partner = None if r.status_code == 404 else check(r, f"{site.label} PM GET /partners/{ref}").json()
        r = self.http.get(f"{site.pm_staff}/partners/requests", headers=h, params={"partner_id": ref, "status": "created"})
        pending = check(r, f"{site.label} PM GET /partners/requests").json().get("requests") or []
        active = []
        if partner:
            r = self.http.get(f"{site.pm_staff}/partners/{ref}/policies", headers=h)
            data = [] if r.status_code == 404 else check(r, f"{site.label} PM GET policies").json()
            active = (data.get("policies") if isinstance(data, dict) else data) or []
        return partner, pending, active

    def wait(self, what: str, done) -> None:
        deadline = time.monotonic() + self.c.wait
        info(f"waiting for {what} (up to {self.c.wait}s; Ctrl-C to stop) …")
        while time.monotonic() < deadline:
            if done():
                return
            time.sleep(self.c.poll)
        raise InstallTestError(f"timed out waiting for {what}", EXIT_DECISION)

    def approve(self, site: Site, request_id: str):
        if self.c.auto_approve:
            check(self.http.post(f"{site.pm_staff}/partners/requests/{request_id}/approve", headers=self.admin(site),
                                 json={"notes": f"approved by {MANAGED_BY} (test.auto_approve)"}),
                  f"{site.label} PM approve")
            info(f"request {request_id} approved (test.auto_approve)")
        else:
            info(f"an administrator approves request {request_id} in {site.pm_portal}")

    def ensure_policies(self, site: Site, ref: str, needed: List[dict], what: str):
        """Raise (and approve) what `ref` still needs in the site's PM, then wait until PM serves it."""
        partner, pending, active = self.pm_state(site, ref)
        if partner is None:
            raise InstallTestError(f"{site.label}: {ref} is not in Partner Management", EXIT_DECISION)
        plan = plan_policies(needed=needed, active=active, pending=pending, subject_type=self.c.subject["type"])
        if plan["conflicts"]:
            raise InstallTestError("; ".join(plan["conflicts"]), EXIT_DECISION)
        if not plan["request"] and not plan["approve"]:
            info(f"{what}: in place")
            return
        for p in plan["request"]:
            info(f"{what}: needs {describe_policy(p)}")
        if self.dry:
            info("(dry run: nothing raised)")
            return
        ids = list(plan["approve"])
        if plan["request"]:
            body = {"partner_id": ref, "description": f"{MANAGED_BY}: {what}", "policies": plan["request"]}
            rid = check(self.http.post(f"{site.pm_staff}/partners/requests/policy", headers=self.admin(site), json=body),
                        f"{site.label} PM policy request").json()["id"]
            info(f"policy request {rid} raised")
            ids.append(rid)
        for rid in ids:
            self.approve(site, rid)
        self.changed = True

        def served():
            got = {policy_key(p): p for p in self.served_policies(site, ref)}
            return all(policy_covers(got.get(policy_key(n)), n, self.c.subject["type"]) for n in needed)
        self.wait(f"{site.label} PM to serve {what}", served)
        info(f"{what}: served")

    # 1. install --------------------------------------------------------------
    def check_install(self):
        c = self.c
        step(f"1. Install (Composite {c.composite_url})")
        r = check(self.http.get(f"{c.composite_url}/composite/v1/use-cases/{c.use_case}"), "GET use case")
        d = r.json()
        self.use_case_ref = d.get("use_case") or c.use_case  # name@major: what the partner's policy grants
        self.purpose = d.get("purpose") or ""
        declared = d.get("consent_scopes") or {}
        self.scopes = {ctl: sorted(set(s.get("required", []) + s.get("optional", []))) for ctl, s in declared.items()}
        info(f"{self.use_case_ref} — {d.get('title')}; purpose {self.purpose}; registries {sorted(self.scopes)}")
        missing = [ctl for ctl in self.scopes if ctl not in c.registries]
        if missing:
            raise InstallTestError(f"the use case reads {missing}, which are not under registries in {c.path}")

        r = self.http.get(f"{c.composite_url}/.well-known/jwks.json")
        if r.status_code == 404:
            raise InstallTestError("the Composite does not serve /.well-known/jwks.json: it is older than this "
                                   "script; upgrade the Composite chart")
        jwks = check(r, "GET Composite JWKS").json()
        self.composite_keys = {k.get("kid"): jwk_public_pem(k) for k in jwks.get("keys") or []}
        if not self.composite_keys:
            raise InstallTestError("the Composite serves no signing key (/.well-known/jwks.json is empty): check its "
                            "signing key Secret and its log")
        info(f"Composite signs with kid {', '.join(self.composite_keys)}")

        # The internal API (consent templates, read in-cluster by the Exchange CM) must not be public.
        r = self.http.get(f"{c.composite_url}/composite/v1/internal/consent-templates", params={"partner": c.partner})
        if r.status_code != 404:
            raise InstallTestError(f"the Composite's internal API answers HTTP {r.status_code} on its public host "
                                   f"({c.composite_url}); it must be 404 there: upgrade the Composite chart (its API "
                                   "VirtualService keeps /composite/v1/internal off the public host)")
        info("Composite's internal API is not on the public host (404)")

        problems = []
        for site in [c.registries[ctl] for ctl in sorted(self.scopes)]:
            served = self.served_keys(site, self.composite_ref)
            match = [k["kid"] for k in served
                     if same_public_key(k.get("public_key"), self.composite_keys.get(k.get("kid")))]
            if match:
                info(f"{site.label} PM serves the Composite's key ({self.composite_ref}, kid {match[0]})")
            elif served:
                problems.append(f"{site.label} PM serves other keys for {self.composite_ref} "
                                f"({[k.get('kid') for k in served]}), not the Composite's")
            else:
                problems.append(f"{site.label} PM ({site.pm_partner}) does not serve {self.composite_ref}: it is "
                                "onboarded at install from the Composite's JWKS URL; check the PM staff API log")
        if problems:
            raise InstallTestError("\n".join(problems), EXIT_DECISION)

    # 2. departments ----------------------------------------------------------
    def departments(self):
        step(f"2. Departments' scope policies for the Composite ({self.composite_ref})")
        for ctl in sorted(self.scopes):
            site = self.c.registries[ctl]
            needed = [policy_input(data_controller=ctl, scopes=self.scopes[ctl], purposes=[self.purpose],
                                   subject_id_types=self.c.subject_id_types)]
            self.ensure_policies(site, self.composite_ref, needed, f"scope policy {ctl}")

    # 3. key ------------------------------------------------------------------
    def partner_key(self):
        step(f"3. Partner key ({self.c.state_dir})")
        path = os.path.join(self.c.state_dir, f"{self.c.partner}.key.pem")
        meta_path = os.path.join(self.c.state_dir, "state.json")
        try:
            with open(meta_path) as fh:
                meta = json.load(fh)
        except (OSError, ValueError):
            meta = {}
        kid = (meta.get(self.c.partner) or {}).get("kid")
        if kid and os.path.exists(path) and not self.new_key:
            with open(path, "rb") as fh:
                self.key, self.kid = serialization.load_pem_private_key(fh.read(), password=None), kid
            info(f"{self.c.partner} ({self.ref}): kid {kid}")
            return
        self.key, self.kid = ec.generate_private_key(ec.SECP256R1()), new_kid(self.c.partner)
        if self.dry:
            info(f"{self.c.partner}: a new key would be generated (dry run: not saved)")
            return
        write_private(path, self.key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                                                   serialization.NoEncryption()))
        meta[self.c.partner] = {"kid": self.kid}
        write_private(meta_path, json.dumps(meta, indent=2).encode())
        info(f"{self.c.partner} ({self.ref}): new kid {self.kid}, saved")

    # 4. partner --------------------------------------------------------------
    def needed_partner_policy(self) -> dict:
        # The test signs its own consent unless it uses one the Exchange CM holds.
        return policy_input(use_case=self.use_case_ref, subject_id_types=self.c.subject_id_types,
                            allow_partner_consents=self.c.consent == "sign")

    def partner(self):
        ex = self.c.exchange
        step(f"4. Partner {self.ref} in the Exchange's Partner Management")
        pub = public_pem(self.key)
        partner, pending, _ = self.pm_state(ex, self.ref)
        plan = plan_partner(ref=self.ref, kid=self.kid, pub_pem=pub, served=self.served_keys(ex, self.ref),
                            partner=partner, pending=pending)
        info(f"key: {plan['reason']}")
        if plan["state"] == "conflict":
            raise InstallTestError(plan["reason"], EXIT_DECISION)
        key_body = [{"public_key": pub, "kid": self.kid, "algorithm": SIGNING_ALG}]
        if plan["actions"] and self.dry:
            info(f"(dry run: would {', '.join(a['op'] for a in plan['actions'])})")
        elif plan["actions"]:
            h = self.admin(ex)
            for act in plan["actions"]:
                if act["op"] == "enable":
                    check(self.http.post(f"{ex.pm_staff}/partners/{self.ref}/enable", headers=h), "PM enable")
                    info(f"{self.ref}: enabled")
                elif act["op"] == "approve":
                    self.approve(ex, act["request_id"])
                elif act["op"] == "onboard":
                    body = {"partner_id": self.ref, "name": f"TEST {self.c.partner}", "org_name": f"TEST {self.c.partner}",
                            "description": f"{MANAGED_BY}: test partner", "keys": key_body,
                            "policies": [self.needed_partner_policy()]}
                    rid = check(self.http.post(f"{ex.pm_staff}/partners/requests/onboarding", headers=h, json=body),
                                "PM onboarding request").json()["id"]
                    info(f"onboarding request {rid} raised, with the policy for {self.use_case_ref}")
                    self.approve(ex, rid)
                else:
                    body = {"partner_id": self.ref, "description": f"{MANAGED_BY}: key {self.kid}", "keys": key_body}
                    rid = check(self.http.post(f"{ex.pm_staff}/partners/requests/key-update", headers=h, json=body),
                                "PM key-update request").json()["id"]
                    info(f"key-update request {rid} raised")
                    self.approve(ex, rid)
            self.changed = True
            self.wait(f"PM to serve {self.ref} kid {self.kid}",
                      lambda: any(k.get("kid") == self.kid and same_public_key(k.get("public_key"), pub)
                                  for k in self.served_keys(ex, self.ref)))
            info(f"PM serves kid {self.kid}")
        if self.dry and not partner:
            info(f"(dry run: {self.ref} is not onboarded yet, so its policy is not checked)")
            return
        self.ensure_policies(ex, self.ref, [self.needed_partner_policy()], f"use-case policy {self.use_case_ref}")

    # 5. portal user ----------------------------------------------------------
    def portal_user(self):
        p = self.c.portal
        if not p["enabled"]:
            return
        username = f"{self.c.partner}-operator"
        secret_name = f"partner-user-{username}"
        ns = self.c.exchange.namespace
        step(f"5. Partner portal user {username} (realm {p['realm']})")
        if self.dry:
            info("(dry run: not created)")
            return
        token = self.http.post(f"{p['keycloak_url']}/realms/master/protocol/openid-connect/token",
                               data={"grant_type": "password", "client_id": "admin-cli", "username": p["admin_user"],
                                     "password": p["admin_password"].read(self.kubectl)})
        if token.status_code != 200:
            raise InstallTestError(f"Keycloak admin login at {p['keycloak_url']}: HTTP {token.status_code}")
        h = {"Authorization": f"Bearer {token.json()['access_token']}"}
        base = f"{p['keycloak_url']}/admin/realms/{p['realm']}"
        found = check(self.http.get(f"{base}/users", headers=h, params={"username": username, "exact": "true"}),
                      "Keycloak GET users").json()
        # Keycloak's user profile requires email, first and last name: without them
        # the user is sent to an "update profile" form at first login.
        profile = {"email": f"{username}@{self.c.exchange.domain or 'example.org'}",
                   "firstName": self.c.partner, "lastName": "Operator"}
        if found:
            user_id = found[0]["id"]
            user = check(self.http.get(f"{base}/users/{user_id}", headers=h), "Keycloak GET user").json()
            missing = {k: v for k, v in profile.items() if not user.get(k)}
            if missing or "UPDATE_PROFILE" in (user.get("requiredActions") or []):
                user.update(missing)
                user["emailVerified"] = True
                user["requiredActions"] = [a for a in user.get("requiredActions") or [] if a != "UPDATE_PROFILE"]
                check(self.http.put(f"{base}/users/{user_id}", headers=h, json=user),
                      "Keycloak update user", ok=(200, 204))
                info(f"{username} exists: profile completed (password left as it is)")
            else:
                info(f"{username} exists (password left as it is)")
        else:
            password = self._portal_password(ns, secret_name, username)
            body = {"username": username, "enabled": True, "emailVerified": True, **profile,
                    "attributes": {"partner_id": [self.c.partner]},
                    "credentials": [{"type": "password", "value": password, "temporary": False}]}
            check(self.http.post(f"{base}/users", headers=h, json=body), "Keycloak create user", ok=(201,))
            user_id = check(self.http.get(f"{base}/users", headers=h, params={"username": username, "exact": "true"}),
                            "Keycloak GET users").json()[0]["id"]
            info(f"{username} created (partner_id {self.c.partner})")
        clients = check(self.http.get(f"{base}/clients", headers=h, params={"clientId": p["client"]}),
                        "Keycloak GET clients").json()
        if not clients:
            raise InstallTestError(f"Keycloak: no client {p['client']} in realm {p['realm']} (the CM chart creates it)")
        cid = clients[0]["id"]
        role = check(self.http.get(f"{base}/clients/{cid}/roles/{p['role']}", headers=h), "Keycloak GET role").json()
        check(self.http.post(f"{base}/users/{user_id}/role-mappings/clients/{cid}", headers=h, json=[role]),
              "Keycloak assign role", ok=(200, 204))
        info(f"portal    {p['url']}  (user {username}, role {p['role']})")
        info(f"password  kubectl -n {ns} get secret {secret_name} -o jsonpath='{{.data.password}}' | base64 -d")

    def _portal_password(self, ns: str, name: str, username: str) -> str:
        """The user's password: kept in a Secret, created once."""
        get = self.kubectl + ["-n", ns, "get", "secret", name, "-o", "jsonpath={.data.password}"]
        r = subprocess.run(get, capture_output=True, text=True)
        if r.returncode == 0 and r.stdout.strip():
            return base64.b64decode(r.stdout.strip()).decode()
        password = secrets.token_urlsafe(18)
        create = self.kubectl + ["-n", ns, "create", "secret", "generic", name,
                                 f"--from-literal=username={username}", f"--from-literal=password={password}"]
        r = subprocess.run(create, capture_output=True, text=True)
        if r.returncode != 0:
            raise InstallTestError(f"cannot create Secret {ns}/{name}: {r.stderr.strip()[:200]}")
        return password

    # 6. query ----------------------------------------------------------------
    def query(self) -> int:
        c = self.c
        step(f"6. Consent and query {self.use_case_ref} as {c.partner}")
        if self.dry:
            info("(dry run: no query)")
            return EXIT_OK
        deadline = time.monotonic() + (CACHE_WAIT if self.changed else 0)
        while True:
            status, body = self._send()
            if not (self._grant_refused(status, body) and time.monotonic() < deadline):
                break
            info(f"access refused, as a cached answer from before this run's PM change would be; "
                 f"retrying in {c.poll}s (up to {CACHE_WAIT}s)")
            time.sleep(c.poll)
        if self.print_response:
            print(json.dumps(body, indent=2, ensure_ascii=False) if not isinstance(body, str) else body, flush=True)
        return self.result(status, body)

    @staticmethod
    def _grant_refused(status, body) -> bool:
        if not isinstance(body, dict):
            return False
        err = (body.get("message") or {}).get("error") or {}
        text = f"{err.get('code')} {err.get('message')}"
        return (status == 403 and err.get("code") == "partner_not_allowed") or any(
            code in text for code in ("unknown_partner",))

    def _send(self):
        c = self.c
        build = dict(partner=c.partner, kid=self.kid, composite=c.composite_id, subject=c.subject,
                     parameters=c.parameters)
        if c.consent == "lookup":
            env, how = build_query(self.key, **build), "no consent (looked up by the Exchange CM)"
        elif c.consent.startswith("id:"):
            cid = c.consent[3:]
            env, how = build_query(self.key, **build, consent_id=cid), f"stored consent {cid}"
        else:
            consent = make_consent(self.key, partner=c.partner, kid=self.kid, subject=c.subject,
                                   purpose=self.purpose, scopes=self.scopes)
            env, how = build_query(self.key, **build, consent_jws=consent), f"consent grants {sorted(self.scopes)}"
        info(f"subject {c.subject['type']} {mask(c.subject['value'])}, parameters {c.parameters or '{}'}, {how}")
        url = f"{c.composite_url}/composite/v1/use-cases/{c.use_case}/query"
        started = time.monotonic()
        r = self.http.post(url, json=env, timeout=90)
        elapsed = int((time.monotonic() - started) * 1000)
        try:
            body = r.json()
        except ValueError:
            body = r.text
        text = json.dumps(body, indent=2, ensure_ascii=False) if not isinstance(body, str) else body
        # The answer holds a farmer's personal data: git-ignored folder, owner-only file.
        path = os.path.join(self.out_dir, f"{c.use_case}-{c.exchange.domain or 'env'}-"
                                          f"{datetime.now():%Y%m%d-%H%M%S}.json")
        write_private(path, (text + "\n").encode())
        info(f"POST {url} → HTTP {r.status_code} in {elapsed} ms; response saved to {path}")
        return r.status_code, body

    def result(self, status, body) -> int:
        step("Result")
        if not isinstance(body, dict):
            warn("the Composite returned no signed envelope")
            return EXIT_CALL_FAILED
        kid = jws_header(body.get("signature") or "").get("kid")
        key = self.composite_keys.get(kid)
        sig = (verify_response(body, load_public(key)) + f" (JWKS key {kid})") if key else \
            f"not checked (the Composite's JWKS has no kid {kid})"
        info(f"response signature: {sig}")
        header, msg = body.get("header") or {}, body.get("message") or {}
        info(f"header.status {header.get('status')}" + (
            f", {header.get('status_reason_code')}: {header.get('status_reason_message')}"
            if header.get("status_reason_code") else ""))
        if msg.get("consent_id"):
            info(f"consent used {msg['consent_id']}")
        sources = msg.get("sources") or {}
        for sid, s in sources.items():
            info(f"source {sid:17} {s.get('status')}" + (f" — {s['detail']}" if s.get("detail") else ""))
        if "INVALID" in sig or sig == "MISSING":
            warn("the response signature does not verify")
            return EXIT_CALL_FAILED
        if status != 200 or header.get("status") != "succ":
            err = msg.get("error") or {}
            warn(f"FAILED: {err.get('code')}: {err.get('message')}")
            return EXIT_CALL_FAILED
        bad = {sid: s for sid, s in sources.items() if s.get("status") not in ("ok", "no_record")}
        if bad:
            warn("FAILED: " + "; ".join(f"{sid} {s.get('status')}" for sid, s in bad.items()))
            return EXIT_CALL_FAILED
        info("OK")
        return EXIT_OK


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--config", "-c", default=os.path.join(HERE, "test_install.yaml"),
                   help="the install's configuration (default scripts/test_install.yaml)")
    p.add_argument("--dry-run", action="store_true", help="check and plan only: nothing is written")
    p.add_argument("--new-key", action="store_true", help="a new key (new kid) for the test partner")
    p.add_argument("--out-dir", default=OUT_DIR, help="where the response JSON is saved (git-ignored)")
    p.add_argument("--print", action="store_true", help="also print the response JSON to stdout")
    a = p.parse_args(argv)

    run = None
    try:
        run = Run(Config(a.config), a)
        run.check_install()
        run.departments()
        run.partner_key()
        run.partner()
        run.portal_user()
        return run.query()
    except InstallTestError as e:
        print("\nERROR: " + str(e).replace("\n", "\n       "), file=sys.stderr)
        return e.code
    except httpx.HTTPError as e:
        print(f"\nERROR: {type(e).__name__}: {e}", file=sys.stderr)
        return EXIT_ERROR
    except KeyboardInterrupt:
        print("\ninterrupted", file=sys.stderr)
        return 130
    finally:
        if run:
            run.close()


if __name__ == "__main__":
    sys.exit(main())
