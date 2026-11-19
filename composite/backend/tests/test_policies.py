"""The PM policy cache: use-case grants with soft / hard / negative TTL, cooldown and single-flight."""

import asyncio

import httpx
import pytest
from conftest import PM_URL, PolicyStub, use_case_policy

from openg2p_agri_composite.core.policies import PartnerPolicyStore, PolicyUnavailable


class Clock:
    def __init__(self):
        self.now = 1000.0

    def __call__(self):
        return self.now


@pytest.fixture
def pm():
    return PolicyStub()


@pytest.fixture
def clock():
    return Clock()


def make_store(pm, clock, **kw):
    client = httpx.AsyncClient(transport=httpx.MockTransport(pm))
    opts = {"soft_ttl": 60, "hard_ttl": 3600, "negative_ttl": 30, "refresh_cooldown": 10, **kw}
    return PartnerPolicyStore(PM_URL, lambda: client, clock=clock, **opts)


async def test_grant_and_partner_reference(pm, clock):
    store = make_store(pm, clock)
    assert await store.granted("bank-a", "loan-profile@1") is True
    assert await store.granted("bank-a", "loan-profile@2") is False
    assert await store.granted("bank-a", "loan-profile@1") is True
    assert pm.calls == [f"{PM_URL}/policies/PARTNER_BANK_A?use_case=loan-profile%401",
                        f"{PM_URL}/policies/PARTNER_BANK_A?use_case=loan-profile%402"]  # cached per use case


async def test_soft_ttl_is_bounded_by_cache_control(pm, clock):
    pm.max_age = 5
    store = make_store(pm, clock)
    assert await store.granted("bank-a", "loan-profile@1")
    clock.now += 4
    await store.granted("bank-a", "loan-profile@1")
    assert len(pm.calls) == 1
    clock.now += 2  # past PM's max-age (5s), before the configured 60s
    pm.policies["PARTNER_BANK_A"] = [use_case_policy(use_case="other@1")]
    assert await store.granted("bank-a", "loan-profile@1") is False  # the revocation is seen
    assert len(pm.calls) == 2


async def test_negative_answer_is_cached_for_the_negative_ttl(pm, clock):
    store = make_store(pm, clock)
    assert await store.granted("bank-b", "loan-profile@1") is False
    clock.now += 29
    assert await store.granted("bank-b", "loan-profile@1") is False
    assert len(pm.calls) == 1
    pm.policies["PARTNER_BANK_B"] = [use_case_policy("PARTNER_BANK_B")]
    clock.now += 2
    assert await store.granted("bank-b", "loan-profile@1") is True
    assert len(pm.calls) == 2


async def test_outage_serves_the_last_answer_within_the_hard_ttl(pm, clock):
    store = make_store(pm, clock)
    assert await store.granted("bank-a", "loan-profile@1")
    pm.down = True
    clock.now += 61  # stale: refresh fails, the last answer is used
    assert await store.granted("bank-a", "loan-profile@1")
    calls = len(pm.calls)
    clock.now += 5  # within the cooldown: PM is not called again
    assert await store.granted("bank-a", "loan-profile@1")
    assert len(pm.calls) == calls
    clock.now += 3600  # past the hard TTL: fail (never "allowed")
    with pytest.raises(PolicyUnavailable):
        await store.granted("bank-a", "loan-profile@1")


async def test_outage_with_nothing_cached_raises_and_backs_off(pm, clock):
    pm.down = True
    store = make_store(pm, clock)
    with pytest.raises(PolicyUnavailable):
        await store.granted("bank-a", "loan-profile@1")
    with pytest.raises(PolicyUnavailable):
        await store.granted("bank-a", "loan-profile@1")
    assert len(pm.calls) == 1  # cooldown
    pm.down = False
    clock.now += 11
    assert await store.granted("bank-a", "loan-profile@1")


async def test_pm_error_status_is_an_outage_not_a_denial(clock):
    def handler(request):
        return httpx.Response(500)

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    store = PartnerPolicyStore(PM_URL, lambda: client, clock=clock)
    with pytest.raises(PolicyUnavailable):
        await store.granted("bank-a", "loan-profile@1")


async def test_no_pm_url_is_unavailable(clock):
    store = PartnerPolicyStore("", lambda: None, clock=clock)
    with pytest.raises(PolicyUnavailable):
        await store.granted("bank-a", "loan-profile@1")
    with pytest.raises(PolicyUnavailable):
        await store.list_for_use_case("loan-profile@1")


async def test_single_flight(clock):
    calls = []

    async def handler(request):
        calls.append(1)
        await asyncio.sleep(0.01)
        return httpx.Response(200, json={"partner_id": "PARTNER_BANK_A", "policies": [use_case_policy()]})

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    store = PartnerPolicyStore(PM_URL, lambda: client, clock=clock)
    results = await asyncio.gather(*(store.granted("bank-a", "loan-profile@1") for _ in range(5)))
    assert all(results) and len(calls) == 1


async def test_listing_by_use_case(pm, clock):
    pm.policies["PARTNER_BANK_B"] = [use_case_policy("PARTNER_BANK_B", use_case="other@1")]
    store = make_store(pm, clock)
    found = await store.list_for_use_case("loan-profile@1")
    assert [p["partner_id"] for p in found] == ["PARTNER_BANK_A"]
    assert pm.calls[-1] == f"{PM_URL}/policies?use_case=loan-profile%401"
    pm.down = True
    with pytest.raises(PolicyUnavailable):
        await store.list_for_use_case("loan-profile@1")


async def test_a_policy_for_another_use_case_does_not_grant(pm, clock):
    # One policy per use case: a scope policy or another use case's policy is not this grant.
    pm.policies["PARTNER_BANK_A"] = [
        use_case_policy(use_case="other@1"),
        use_case_policy(data_controller="farmer-registry", use_case=None, allowed_data_scopes=["farmer-registry.land"]),
    ]
    store = make_store(pm, clock)
    assert await store.granted("bank-a", "loan-profile@1") is False
    assert await store.granted("bank-a", "other@1") is True
    store.invalidate("bank-a")
    assert await store.granted("bank-a", "other@1") is True
    assert len(pm.calls) == 3
