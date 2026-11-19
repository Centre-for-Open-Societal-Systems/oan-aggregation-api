"""Unit tests for scripts/test_install.py: configuration, signing and the PM plans."""

import base64
import json
import os
import sys

import pytest
from cryptography.hazmat.primitives.asymmetric import ec
from jwt import PyJWS

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import test_install as t  # noqa: E402

SCOPES = {"farmer-registry": ["farmer-registry.land"], "crop-sown-registry": ["crop-sown-registry.crop_season"]}


@pytest.fixture
def key():
    return ec.generate_private_key(ec.SECP256R1())


@pytest.fixture
def other():
    return ec.generate_private_key(ec.SECP256R1())


def _config(tmp_path, text):
    path = tmp_path / "c.yaml"
    path.write_text(text)
    return t.Config(str(path))


# ── configuration ─────────────────────────────────────────────────────────────

def test_reference_config_loads():
    c = t.Config(os.path.join(t.HERE, "test_install.yaml"))
    assert c.exchange.namespace == "agrix"
    assert c.composite_url == "https://agri-composite.agrix.openg2p.org"
    assert c.exchange.pm_staff == "https://pm-staff-portal.agrix.openg2p.org"
    assert c.exchange.token_url == "https://keycloak.agrix.openg2p.org/realms/staff/protocol/openid-connect/token"
    assert sorted(c.registries) == ["crop-sown-registry", "farmer-registry"]
    assert c.registries["farmer-registry"].pm_partner == "https://partner-management-partner-api.trial.openg2p.org"
    assert c.registries["farmer-registry"].client_secret.namespace == "trial"


def test_names_come_from_the_config(tmp_path):
    c = _config(tmp_path, """
exchange:
  namespace: gw
  domain: gw.example.org
  composite: {url: https://data.example.org/, partner_id: my-gateway}
  partner_management:
    staff_api_url: https://pm.example.org/api
    token_url: https://login.example.org/token
    client_id: pm-admin
    client_secret: {secret: pm-admin-secret, key: s, env: GW_PM}
registries:
  - controller: livestock
    namespace: vet
    domain: vet.example.org
test:
  partner: coop-1
  subject: {type: FARMER_ID, value: F-1}
  consent: id:abc
""")
    assert c.composite_url == "https://data.example.org" and c.composite_id == "my-gateway"
    assert c.exchange.pm_staff == "https://pm.example.org/api"
    assert c.exchange.token_url == "https://login.example.org/token"
    assert c.exchange.client_id == "pm-admin" and c.exchange.client_secret.name == "pm-admin-secret"
    assert c.registries["livestock"].pm_staff == "https://pm-staff-portal.vet.example.org"
    assert c.partner == "coop-1" and c.subject == {"type": "FARMER_ID", "value": "F-1"} and c.consent == "id:abc"


def test_config_errors(tmp_path):
    with pytest.raises(t.InstallTestError, match="subject.value"):
        _config(tmp_path, "exchange: {domain: x.org}\n")
    with pytest.raises(t.InstallTestError, match="sign, lookup"):
        _config(tmp_path, "exchange: {domain: x.org}\ntest: {subject: {value: '1'}, consent: other}\n")
    with pytest.raises(t.InstallTestError, match="no domain"):
        _config(tmp_path, "exchange: {}\ntest: {subject: {value: '1'}}\n")


def test_secret_from_env_wins(monkeypatch):
    s = t.Secret({"secret": "x", "key": "k", "env": "T_SECRET"}, "ns", "label")
    monkeypatch.setenv("T_SECRET", "from-env")
    assert s.read(["false-kubectl"]) == "from-env"


def test_secret_from_kubectl(monkeypatch):
    calls = []

    class R:
        returncode, stdout, stderr = 0, base64.b64encode(b"from-cluster").decode(), ""

    monkeypatch.setattr(t.subprocess, "run", lambda cmd, **kw: calls.append(cmd) or R())
    s = t.Secret({"secret": "pm", "key": "client_secret"}, "dept", "label")
    assert s.read(["kubectl", "--context", "c"]) == "from-cluster"
    assert calls[0][:7] == ["kubectl", "--context", "c", "-n", "dept", "get", "secret"]


# ── signing ───────────────────────────────────────────────────────────────────

def test_consent_has_a_grant_per_registry(key):
    jws = t.make_consent(key, partner="bank-a", kid="k1", subject={"type": "FAYDA_FAN", "value": "1"},
                         purpose="credit-assessment", scopes=SCOPES)
    claims = json.loads(PyJWS().decode(jws, key.public_key(), algorithms=["ES256"]))
    assert {g["data_controller"] for g in claims["grants"]} == set(SCOPES)
    assert claims["aud"] == "bank-a" and t.jws_header(jws)["kid"] == "k1"


def test_query_signature_covers_header_and_message(key):
    env = t.build_query(key, partner="bank-a", kid="k1", composite="agri-composite",
                        subject={"type": "FAYDA_FAN", "value": "1"}, consent_id="c-1")
    assert env["message"]["consent_id"] == "c-1" and "consent_jws" not in env["message"]
    p1, _, p3 = env["signature"].split(".")
    full = f"{p1}.{t.b64u(t.canonical({'header': env['header'], 'message': env['message']}))}.{p3}"
    PyJWS().decode(full, key.public_key(), algorithms=["ES256"])


def test_verify_response(key, other):
    body = {"header": {"a": 1}, "message": {"b": 2}}
    body["signature"] = t.sign_detached({"header": body["header"], "message": body["message"]}, key, "k")
    assert t.verify_response(body, key.public_key()) == "valid"
    assert t.verify_response(body, other.public_key()).startswith("INVALID")
    assert t.verify_response({}, key.public_key()) == "MISSING"


def test_jwk_public_pem_matches(key):
    from jwt.algorithms import ECAlgorithm
    jwk = json.loads(ECAlgorithm.to_jwk(key.public_key()))
    assert t.same_public_key(t.jwk_public_pem(jwk), t.public_pem(key))


# ── PM plans ──────────────────────────────────────────────────────────────────

def _scope(scopes=("farmer-registry.land",), purposes=("credit-assessment",)):
    return t.policy_input(data_controller="farmer-registry", scopes=scopes, purposes=purposes,
                          subject_id_types=["FAYDA_FAN"])


def test_partner_absent_is_onboarded(key):
    plan = t.plan_partner(ref="P", kid="k", pub_pem=t.public_pem(key), served=[], partner=None, pending=[])
    assert plan["actions"] == [{"op": "onboard"}]


def test_partner_served_key_is_ok(key):
    served = [{"kid": "k", "public_key": t.public_pem(key)}]
    assert t.plan_partner(ref="P", kid="k", pub_pem=t.public_pem(key), served=served, partner={},
                          pending=[])["state"] == "ok"


def test_partner_same_kid_other_key_is_conflict(key, other):
    served = [{"kid": "k", "public_key": t.public_pem(other)}]
    assert t.plan_partner(ref="P", kid="k", pub_pem=t.public_pem(key), served=served, partner={},
                          pending=[])["state"] == "conflict"


def test_partner_disabled_is_enabled_then_key_update(key):
    plan = t.plan_partner(ref="P", kid="k", pub_pem=t.public_pem(key), served=[], partner={"status": "disabled"},
                          pending=[])
    assert [a["op"] for a in plan["actions"]] == ["enable", "key_update"]


def test_partner_open_request_with_our_kid_is_approved(key):
    pending = [{"id": "r1", "request_type": "key_update", "proposed_keys": [{"kid": "k"}]}]
    plan = t.plan_partner(ref="P", kid="k", pub_pem=t.public_pem(key), served=[], partner={"status": "active"},
                          pending=pending)
    assert plan["actions"] == [{"op": "approve", "request_id": "r1"}]


def test_policy_missing_is_requested():
    plan = t.plan_policies(needed=[_scope()], active=[], pending=[], subject_type="FAYDA_FAN")
    assert len(plan["request"]) == 1 and not plan["approve"]


def test_policy_in_place_is_ok():
    plan = t.plan_policies(needed=[_scope()], active=[{**_scope(), "status": "active"}], pending=[],
                           subject_type="FAYDA_FAN")
    assert plan["ok"] == ["farmer-registry"] and not plan["request"]


def test_policy_is_widened_never_narrowed():
    active = {**_scope(scopes=("farmer-registry.main_crops",)), "status": "active"}
    plan = t.plan_policies(needed=[_scope()], active=[active], pending=[], subject_type="FAYDA_FAN")
    assert plan["request"][0]["allowed_data_scopes"] == ["farmer-registry.land", "farmer-registry.main_crops"]


def test_policy_other_purpose_needs_a_request():
    active = {**_scope(purposes=("other",)), "status": "active"}
    plan = t.plan_policies(needed=[_scope()], active=[active], pending=[], subject_type="FAYDA_FAN")
    assert plan["request"][0]["allowed_purposes"] == ["credit-assessment", "other"]


def test_open_request_that_covers_is_approved():
    pending = [{"id": "r1", "request_type": "policy", "proposed_policies": [_scope()]}]
    plan = t.plan_policies(needed=[_scope()], active=[], pending=pending, subject_type="FAYDA_FAN")
    assert plan["approve"] == ["r1"] and not plan["request"]


def test_open_request_that_does_not_cover_is_a_conflict():
    pending = [{"id": "r1", "request_type": "policy", "proposed_policies": [_scope(scopes=())]}]
    plan = t.plan_policies(needed=[_scope()], active=[], pending=pending, subject_type="FAYDA_FAN")
    assert plan["conflicts"]


def test_use_case_policy_is_its_own_key():
    uc = t.policy_input(use_case="loan-profile@1", subject_id_types=["FAYDA_FAN"], allow_partner_consents=True)
    plan = t.plan_policies(needed=[uc], active=[{**_scope(), "status": "active"}], pending=[],
                           subject_type="FAYDA_FAN")
    assert plan["request"][0]["use_case"] == "loan-profile@1"
    assert not t.policy_covers({**uc, "status": "active", "allow_partner_consents": False}, uc, "FAYDA_FAN")


def test_use_case_policy_lists_no_purposes():
    # Granting a use case grants its purpose: PM refuses purposes on a use-case policy.
    uc = t.policy_input(use_case="loan-profile@1", subject_id_types=["FAYDA_FAN"])
    assert "allowed_purposes" not in uc
    with pytest.raises(ValueError):
        t.policy_input(use_case="loan-profile@1", purposes=["credit-assessment"])
    # An active policy from before (with purposes) still covers; widening it sends no purposes.
    old = {**uc, "status": "active", "allowed_purposes": ["credit-assessment"], "allowed_subject_id_types": ["X"]}
    assert not t.policy_covers(old, uc, "FAYDA_FAN")
    plan = t.plan_policies(needed=[uc], active=[old], pending=[], subject_type="FAYDA_FAN")
    assert "allowed_purposes" not in plan["request"][0]
    assert plan["request"][0]["allowed_subject_id_types"] == ["FAYDA_FAN", "X"]


def test_policy_validity_cap():
    assert t.duration_days("P90D") == 90 and t.duration_days("P1Y") == 365 and t.duration_days("") is None
    short = {**_scope(), "status": "active", "max_validity_duration": "P0D"}
    assert not t.policy_covers(short, _scope(), "FAYDA_FAN")
