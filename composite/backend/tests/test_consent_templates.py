"""Consent templates (internal API): published use cases granted to a partner, for the Consent Manager."""

import types

import httpx
import pytest
from conftest import use_case_policy
from fastapi import FastAPI
from test_engine import Harness

BASE = "/composite/v1/internal/consent-templates"


@pytest.fixture
def h(use_cases_dir, composite_p12, pm_transport, registries, partner_key):
    return Harness(use_cases_dir, composite_p12, pm_transport, registries, partner_key)


@pytest.fixture
async def client(h, monkeypatch):
    from openg2p_agri_composite.controllers import consent_template_controller as ctc

    service = types.SimpleNamespace(registry=h.registry, policies=h.policies)
    monkeypatch.setattr(ctc.CompositeService, "get_component", classmethod(lambda cls, *a, **k: service))
    app = FastAPI()
    app.include_router(ctc.ConsentTemplateController().router)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://agri-composite") as c:
        yield c


async def test_templates_for_a_granted_partner(client, h):
    h.pm.policies["PARTNER_BANK_A"] = [use_case_policy(
        allowed_subject_id_types=["FAYDA_FAN"], max_validity_duration="P1Y", allowed_purposes=["x"])]
    r = await client.get(BASE, params={"partner": "bank-a"})
    assert r.status_code == 200, r.text
    assert "max-age=60" in r.headers["cache-control"]
    [t] = r.json()["templates"]
    compiled = h.registry.get("loan-profile@1")
    assert t["code"] == "loan-profile@1" and t["purpose"] == "credit-assessment"
    assert t["title"] == compiled.spec.title and t["description"] == compiled.spec.description
    # every registry the use case reads; required / optional scopes as the use case declares them
    assert t["scopes"] == {c: compiled.spec.consent_scopes().get(c, {"required": [], "optional": []})
                           for c in ("crop-sown-registry", "farmer-registry")}
    assert t["scopes"]["farmer-registry"]["required"]
    # limits from bank-a's use-case policy (no signing algorithms, no purposes)
    assert t["limits"] == {"allowed_subject_id_types": ["FAYDA_FAN"], "max_validity_duration": "P1Y",
                           "allow_partner_consents": False, "allow_assisted_confirmation": False}

    one = await client.get(f"{BASE}/loan-profile@1", params={"partner": "bank-a"})
    assert one.status_code == 200 and one.json() == t


async def test_template_limits_carry_the_policy_consent_permissions(client, h):
    # partner consents and assisted confirmation (scenario 0: national-ID login, one "I agree")
    h.pm.policies["PARTNER_BANK_A"] = [use_case_policy(allow_partner_consents=True,
                                                       allow_assisted_confirmation=True)]
    r = await client.get(f"{BASE}/loan-profile@1", params={"partner": "bank-a"})
    assert r.status_code == 200, r.text
    assert r.json()["limits"] == {"allow_partner_consents": True, "allow_assisted_confirmation": True}


async def test_a_partner_not_granted_sees_no_template(client, h):
    lst = await client.get(BASE, params={"partner": "bank-b"})
    assert lst.status_code == 200 and lst.json() == {"templates": []}
    h.pm.policies["PARTNER_BANK_A"] = [use_case_policy(use_case="other@1")]
    lst = await client.get(BASE, params={"partner": "bank-a"})
    assert lst.json() == {"templates": []}
    # unknown template, not granted, unknown partner, a malformed partner: one answer
    bodies = []
    for code, partner in (("loan-profile@1", "bank-a"), ("loan-profile@1", "bank-b"), ("nope@1", "bank-a"),
                          ("loan-profile", "bank-a"), ("loan-profile@1", "../x")):
        r = await client.get(f"{BASE}/{code}", params={"partner": partner})
        assert r.status_code == 404, (code, partner, r.text)
        bodies.append(r.json())
    assert all(b == bodies[0] for b in bodies)


async def test_partner_is_required(client):
    assert (await client.get(BASE)).status_code == 422


async def test_pm_down_is_503(client, h):
    h.pm.down = True
    r = await client.get(BASE, params={"partner": "bank-a"})
    assert r.status_code == 503 and r.json()["error"]["code"] == "partner_policy_unavailable"


async def test_not_answered_through_the_public_gateway(client):
    # The public VirtualService sets x-forwarded-host (and answers 404 there itself).
    for path in (BASE, f"{BASE}/loan-profile@1"):
        r = await client.get(path, params={"partner": "bank-a"}, headers={"x-forwarded-host": "agri-composite.example"})
        assert r.status_code == 404, r.text


async def test_not_in_the_openapi_document(client):
    from openg2p_agri_composite.controllers import consent_template_controller as ctc

    assert all(not r.include_in_schema for r in ctc.ConsentTemplateController().router.routes)
