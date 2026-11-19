"""The console's backend: scope catalogue, call log and the read-only admin API."""

import asyncio
import json
import types
from datetime import datetime, timedelta, timezone

import httpx
import pytest
from conftest import CSR_URL, FR_URL
from sqlalchemy import update
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from test_engine import REGISTRIES, Harness

from openg2p_agri_composite.core.activity import ActivityRow, ActivityStore
from openg2p_agri_composite.core.catalogue import ScopeCatalogue, catalogue_url

CSR_CATALOGUE = "http://csr.test/partner/data_scopes"
FR_CATALOGUE = "http://fr.test/partner/data_scopes"

CSR_SCOPES = {
    "data_controller": "crop-sown-registry",
    "data_scopes": [
        {"scope_id": "crop-sown-registry.crop_season", "name": "crop_season", "label": "Crop season",
         "status": "ACTIVE", "current_version": 2,
         "versions": [{"version": 1, "fields": ["CropSown.context.crop"]},
                      {"version": 2, "fields": ["CropSown.context.crop", "CropSown.context.season"],
                       "resolved_fields": ["CropSown.context.crop", "CropSown.context.season"]}]},
        {"scope_id": "crop-sown-registry.activity", "name": "activity", "label": "Activity details",
         "status": "ACTIVE", "current_version": 1, "versions": [{"version": 1, "fields": ["CropSown.activity.channel"]}]},
    ],
}


@pytest.fixture
def h(use_cases_dir, composite_p12, pm_transport, registries, partner_key):
    return Harness(use_cases_dir, composite_p12, pm_transport, registries, partner_key)


@pytest.fixture
async def store():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    s = ActivityStore(async_sessionmaker(engine, expire_on_commit=False), engine, retention_days=30)
    await s.start(maintenance=False)
    yield s
    await s.stop()
    await engine.dispose()


# ── scope catalogue ─────────────────────────────────────────────────────────

def test_catalogue_url_from_the_search_url():
    assert catalogue_url({"url": "https://partner-fr.trial.openg2p.org/dci/registry/sync/search"}) == \
        "https://partner-fr.trial.openg2p.org/partner/data_scopes"
    assert catalogue_url({"url": FR_URL, "catalogue_url": "http://x/scopes"}) == "http://x/scopes"


async def test_catalogue_is_a_signed_post_and_cached(h):
    calls = []

    async def handler(request: httpx.Request):
        calls.append(json.loads(request.content))
        await asyncio.sleep(0.01)  # a real registry takes time: concurrent fetches overlap
        if str(request.url) == CSR_CATALOGUE:
            return httpx.Response(200, json=CSR_SCOPES)
        return httpx.Response(503)

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    cat = ScopeCatalogue(REGISTRIES, h.crypto, lambda: client, "agri-composite", ttl=300)

    result = await cat.fetch("crop-sown-registry")
    assert result["error"] is None and result["data_controller"] == "crop-sown-registry"
    assert [s["name"] for s in result["data_scopes"]] == ["crop_season", "activity"]
    body = calls[0]
    assert body["header"]["sender_id"] == "agri-composite" and body["header"]["receiver_id"] == "crop-sown-registry"
    assert body["signature"].count(".") == 2 and body["signature"].split(".")[1] == ""  # detached JWS
    await cat.fetch("crop-sown-registry")
    assert len(calls) == 1  # cached
    await cat.fetch("crop-sown-registry", refresh=True)
    assert len(calls) == 2

    # concurrent fetches share one request
    await asyncio.gather(*(cat.fetch("crop-sown-registry", refresh=True) for _ in range(5)))
    assert len(calls) == 3

    failed = await cat.fetch("farmer-registry")
    assert failed["error"] == f"HTTP 503 from {FR_CATALOGUE}" and failed["data_scopes"] == []


# ── call log ────────────────────────────────────────────────────────────────

async def test_call_log_records_filters_and_purges(store):
    for partner, uc, outcome in [("bank-a", "loan-profile@1", "success"), ("bank-b", "loan-profile@1", "denied"),
                                 ("bank-a", "other@2", "failure")]:
        store.record(request_id="r", use_case=uc, partner_id=partner, http_status=200, outcome=outcome,
                     reason=None, duration_ms=12, sources={"farmer": "ok"}, consent_id=f"c-{partner}")
    await store.drain()
    total, capped, items = await store.query()
    assert total == 3 and capped is False and items[0]["sources"] == {"farmer": "ok"} and items[0]["at"].endswith("Z")
    assert {i["consent_id"] for i in items} == {"c-bank-a", "c-bank-b"}
    assert (await store.query(partner="bank-a"))[0] == 2
    assert (await store.query(use_case="loan-profile"))[0] == 2
    assert (await store.query(use_case="other@2", outcome="failure"))[0] == 1
    assert await store.summary(24) == {"calls": 3, "failed": 2}

    old = datetime.now(timezone.utc) - timedelta(days=31)
    async with store._session_maker() as session:
        await session.execute(update(ActivityRow).where(ActivityRow.partner_id == "bank-b").values(at=old))
        await session.commit()
    assert await store.purge() == 1
    assert (await store.query())[0] == 2


async def test_call_log_off_without_a_database():
    s = ActivityStore()
    assert not s.enabled
    s.record(request_id="r", use_case="u", partner_id="p", http_status=200, outcome="success",
             reason=None, duration_ms=1, sources={})  # no-op, no error


async def test_authenticated_queries_are_recorded_unsigned_ones_are_not(h, store):
    h.engine.activity = store
    status, _ = await h.query(h.envelope())
    assert status == 200
    # bank-z has no key in PM: its signature does not verify, so its name is not trusted.
    status, _ = await h.query(h.envelope(sender="bank-z"))
    assert status == 401
    garbage = h.envelope()
    garbage["signature"] = "not-a-signature"
    assert (await h.query(garbage))[0] == 401
    await store.drain()
    total, _capped, items = await store.query()
    assert total == 1
    row = items[0]
    assert row["partner_id"] == "bank-a" and row["outcome"] == "success" and row["use_case"] == "loan-profile@1"
    assert set(row["sources"]) == {"farmer", "season_summaries", "crop_seasons"}


async def test_failures_after_authentication_are_recorded(h, store):
    h.engine.activity = store
    h.pm.policies.clear()  # bank-a has no use-case policy in PM
    status, _ = await h.query(h.envelope())
    assert status == 403
    await store.drain()
    _total, _c, items = await store.query()
    assert items[0]["partner_id"] == "bank-a" and items[0]["reason"] == "partner_not_allowed"


async def test_count_is_capped(store, monkeypatch):
    from openg2p_agri_composite.core import activity as act

    monkeypatch.setattr(act, "COUNT_CAP", 3)
    for _ in range(5):
        store.record(request_id="r", use_case="u@1", partner_id="p", http_status=200, outcome="success",
                     reason=None, duration_ms=1, sources={})
    await store.drain()
    total, capped, items = await store.query(limit=2)
    assert (total, capped, len(items)) == (3, True, 2)


# ── admin API ───────────────────────────────────────────────────────────────

@pytest.fixture
async def admin(h, store, monkeypatch):
    from openg2p_agri_composite.controllers import admin_controller as ac

    h.engine.activity = store

    def handler(request: httpx.Request):
        url = str(request.url)
        if url == CSR_CATALOGUE:
            return httpx.Response(200, json=CSR_SCOPES)
        if url == FR_CATALOGUE:
            raise httpx.ConnectError("down")
        return httpx.Response(404)

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))

    async def signing_kid():
        return "composite-test"

    async def partner_keys(ref):
        return ([{"kid": "k1", "algorithm": "ES256"}], None) if ref == "PARTNER_BANK_A" else ([], None)

    service = types.SimpleNamespace(
        registry=h.registry, engine=h.engine, audit=h.engine.audit, activity=store,
        catalogue=ScopeCatalogue(REGISTRIES, h.crypto, lambda: client, "agri-composite"),
        signing_kid=signing_kid, partner_keys=partner_keys, policies=h.policies,
    )
    monkeypatch.setattr(ac.CompositeService, "get_component", classmethod(lambda cls, *a, **k: service))
    return ac.AdminController()


async def test_admin_endpoints_require_the_view_permission(admin):
    from iam_core.user_auth.enums import EndpointMetadataKey

    assert len(admin.router.routes) == 7
    for route in admin.router.routes:
        assert getattr(route.endpoint, EndpointMetadataKey.REQUIRED_PERMISSIONS) == frozenset({"composite:view"}), route.path


async def test_admin_overview_and_use_cases(admin, h, store):
    await h.query(h.envelope())
    await store.drain()
    o = await admin.overview()
    assert o["consent_mode"] == "passthrough" and o["exchange_cm_url"] == ""
    assert o["signing_kid"] == "composite-test" and o["activity_recording"] is True
    assert o["use_cases"] == {"published": 1, "errors": 0} and o["registries"] == 2 and o["partners"] == 1
    assert o["activity_24h"] == {"calls": 1, "failed": 0}

    lst = await admin.list_use_cases()
    uc = lst["use_cases"][0]
    assert uc["use_case"] == "loan-profile@1" and uc["granted_to"] == ["PARTNER_BANK_A"]
    assert uc["granted_to_error"] is None
    assert uc["file"] == "loan-profile.yaml" and "farmer-registry" in uc["consent_scopes"]
    one = await admin.get_use_case("loan-profile")
    assert "use_case: loan-profile" in one["source_yaml"] and "farmer.name" in one["mapping"]
    assert one["granted_to"] == ["PARTNER_BANK_A"]
    assert (await admin.get_use_case("nope")).status_code == 404


async def test_admin_use_cases_when_pm_is_down(admin, h):
    h.pm.down = True
    uc = (await admin.list_use_cases())["use_cases"][0]
    assert uc["granted_to"] is None and "Partner Management" in uc["granted_to_error"]
    assert (await admin.overview())["partners"] is None
    lst = await admin.list_partners()
    assert lst["partners"] == [] and "Partner Management" in lst["pm_error"]


async def test_admin_registries_and_scopes(admin):
    regs = {r["id"]: r for r in (await admin.list_registries())["registries"]}
    assert regs["crop-sown-registry"]["reachable"] is True and regs["crop-sown-registry"]["scope_count"] == 2
    assert regs["farmer-registry"]["reachable"] is False and "unreachable" in regs["farmer-registry"]["error"]
    assert regs["farmer-registry"]["used_by"] == ["loan-profile@1"]

    scopes = await admin.get_data_scopes("crop-sown-registry")
    by_id = {s["scope_id"]: s for s in scopes["data_scopes"]}
    season = by_id["crop-sown-registry.crop_season"]
    assert season["fields"] == ["CropSown.context.crop", "CropSown.context.season"]  # the current version's
    assert {(u["source"], u["required"]) for u in season["used_by"]} == {("season_summaries", True), ("crop_seasons", True)}
    assert by_id["crop-sown-registry.activity"]["used_by"] == []
    assert (await admin.get_data_scopes("nope")).status_code == 404


async def test_admin_partners_and_activity(admin, h, store):
    from conftest import use_case_policy

    # Partners come from PM use-case policies (a partner granted only other use cases is not listed).
    h.pm.policies["PARTNER_BANK_C"] = [use_case_policy("PARTNER_BANK_C", use_case="other@1")]
    lst = await admin.list_partners()
    assert lst["pm_error"] is None
    assert lst["partners"] == [{"partner_id": "PARTNER_BANK_A", "use_cases": ["loan-profile@1"],
                                "pm_keys": [{"kid": "k1", "algorithm": "ES256"}], "pm_error": None}]
    await h.query(h.envelope())
    await store.drain()
    act = await admin.list_activity(limit=50, offset=0, partner="bank-a", use_case=None, outcome=None)
    assert act["recording"] is True and act["total"] == 1
    assert act["partners"] == ["bank-a"]  # the filter's choices: senders in the call log
    assert (await admin.list_activity(limit=50, offset=0, partner=None, use_case=None, outcome="bad")).status_code == 400
    admin.service.activity = ActivityStore()
    assert await admin.list_activity(limit=50, offset=0, partner=None, use_case=None, outcome=None) == \
        {"recording": False, "total": 0, "total_capped": False, "items": [], "partners": []}


def test_registry_urls_used_in_tests():
    assert REGISTRIES["farmer-registry"]["url"] == FR_URL and REGISTRIES["crop-sown-registry"]["url"] == CSR_URL
