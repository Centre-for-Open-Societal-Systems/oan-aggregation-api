"""Staff search: the staff_api catalog block, exact matching of staff-api hits,
the search answer (summary + tabs), the tab answer (sections, fields, pages),
the binding of a tab request to its search, and the checks at the door.

No staff-api is called: StaffApiClient._post is replaced by a fake that answers
from the shapes the real staff-apis return (seen on the local stack)."""
import asyncio
import copy
import json
import logging
import time

import pytest

from openg2p_aggregation_layer.auth import CallerIdentity
from openg2p_aggregation_layer.registry_catalog import CatalogError, parse_catalog
from openg2p_aggregation_layer.services import staff_search_service
from openg2p_aggregation_layer.services.aggregator_service import AggregationError
from openg2p_aggregation_layer.services.registry_client import RegistryError
from openg2p_aggregation_layer.services.staff_api_client import StaffApiClient
from openg2p_aggregation_layer.services.staff_search_service import (
    StaffSearchService,
    _fields,
    _sections,
)

ENTRY = {
    "name": "Livestock Registry",
    "partner_api": {"base_url": "http://ls-partner:8000", "receiver_id": "ls"},
    "staff_api": {"base_url": "http://ls-staff:8000", "registers": ["Livestock"]},
    "binding": {"audience": "agg-ls", "controller_id": "ls"},
    "search": {"reg_type": "Livestock", "reg_record_type": "x:LivestockRecord", "id_type": "UIN",
               "match": {"path": "details.ids[]", "strip_prefixes": ["FAN-"]}},
    "registers": {"LIVESTOCK": {"name": "Holding"}},
    "scopes": {"details": {"register": "LIVESTOCK", "fields": ["*"]}},
}
CODE = "LIVESTOCK_REGISTRY"


def _catalog(**staff_api):
    entry = copy.deepcopy(ENTRY)
    if staff_api.pop("_drop", False):
        entry.pop("staff_api")
    else:
        entry["staff_api"].update(staff_api)
    return parse_catalog({"version": 1, "registries": {CODE: entry}})


# ── catalog ─────────────────────────────────────────────────────────────────


def test_staff_api_is_optional():
    assert _catalog(_drop=True).staff_codes() == []
    assert _catalog().staff_codes() == [CODE]


@pytest.mark.parametrize("bad", [
    {"base_url": "ftp://x"},
    {"registers": []},
    {"match_fields": ["a b"]},
    {"unknown_key": 1},
    {"auth": {"token_url": "http://kc/token", "client_id": "c"}},   # no client_secret_env
])
def test_staff_api_is_validated(bad):
    with pytest.raises(CatalogError):
        _catalog(**bad)


def test_hidden_tabs():
    staff_api = _catalog(hidden_tabs=["secret_tab"]).get(CODE).staff_api
    assert staff_api.shows_tab("animal_tab")
    assert not staff_api.shows_tab("secret_tab")
    assert not staff_api.shows_tab(None)


def test_staff_hit_must_carry_the_id_exactly():
    entry = _catalog().get(CODE)
    fid = "2222444466668888"
    assert entry.staff_hit_identifies({"link_foundational_id": "FAN-" + fid}, fid)
    assert entry.staff_hit_identifies({"foundational_id": fid}, "fan-" + fid)
    # a substring search also returns records that merely contain the number
    assert not entry.staff_hit_identifies({"link_foundational_id": fid + "9"}, fid)
    assert not entry.staff_hit_identifies({"phone": fid}, fid)
    assert not entry.staff_hit_identifies({"foundational_id": ""}, "")
    only_own = _catalog(match_fields=["foundational_id"]).get(CODE)
    assert not only_own.staff_hit_identifies({"link_foundational_id": fid}, fid)


# ── the staff-api calls ─────────────────────────────────────────────────────

FID = "2222444466668888"
REG = "reg-livestock"
ANIMAL = "reg-animal"
HIT = {"internal_record_id": "ls-1", "functional_record_id": "LS/1", "foundational_id": None,
       "link_foundational_id": "FAN-" + FID, "record_name": "Abeba holding",
       "created_at": "2026-10-09T08:08:09", "last_approved_at": None,
       "display_fields": [{"field_name": "kebele_name", "value": "Kebele 03", "order": 2},
                          {"field_name": "region_name", "value": "Oromia", "order": 1}]}
TABS = [{"tab_id": "animal_tab", "tab_label": "animal_tab", "tab_order": 2, "is_active": True},
        {"tab_id": "main_tab", "tab_label": "Holding", "tab_order": 0, "is_active": True},
        {"tab_id": "hidden_tab", "tab_label": "x", "tab_order": 9, "is_active": False}]
UI_MAIN = {"panels": [{"widgets": [
    {"widget": "text", "widget-label": "Holding ID", "widget-data-path": REG + ".functional_record_id"},
    {"widget": "date", "widget-label": "Registered", "widget-data-path": REG + ".created_at"}]}]}
UI_ANIMAL = {"panels": [{"widgets": [{
    "widget": "dialog-table", "widget-label": "Animals", "widget-data-path": ANIMAL + ".records",
    "widget-data-columns": [
        {"widget": "text", "widget-label": "Tag", "widget-data-path": "tag"},
        {"widget": "select", "widget-label": "Species", "widget-data-path": "species"}]}]}]}
SECTIONS = {
    "main_tab": [{"section_order": 10, "section_data": {
        "section_id": "s-main", "section_register_id": REG, "section_mnemonic": "ls_main",
        "section_description": "Holding", "is_list": False, "section_ui_schema": UI_MAIN}}],
    "animal_tab": [{"section_order": 10, "section_data": {
        "section_id": "s-animal", "section_register_id": ANIMAL, "section_mnemonic": "ls_animal",
        "section_description": "Animals", "is_list": True, "section_ui_schema": UI_ANIMAL}}],
}


def _fake_staff_api(calls, hits=None, tabs=None):
    """Answers like a livestock staff-api holding one holding for FID."""
    hits = hits if hits is not None else [
        HIT, {"internal_record_id": "ls-decoy", "link_foundational_id": FID + "0"}]
    tab_rows = tabs if tabs is not None else TABS

    async def post(registry, entry, path, payload, page_size=100, search_text=None):
        calls.append((path, payload, search_text))
        if path == "/register-metadata/get_all_registers":
            return [{"register_mnemonic": "Livestock", "register_id": REG},
                    {"register_mnemonic": "Animal", "register_id": ANIMAL}]
        if path == "/register-data/search_in_a_register":
            return hits
        if path == "/register-tab-metadata/get_all_tabs":
            return list(tab_rows)
        if path == "/register-tab-metadata/get_sections":
            return SECTIONS.get(payload["tab_id"], [])
        if path == "/register-data/get_tab_records":
            if payload["tab_id"] == "main_tab":
                return [{"section_register_id": REG, "is_list": False,
                         "records": [{"internal_record_id": "ls-1"}]}]
            return [{"section_register_id": ANIMAL, "is_list": True,
                     "records": [{"tag": "A%d" % i} for i in range(5)]}]
        raise AssertionError("unexpected call " + path)

    return post


def _client(calls, pages=None, **kw):
    """``pages``: {path: [page1_rows, page2_rows, ...]} answered with a
    pagination_response; other list calls are one page without one."""
    client = object.__new__(StaffApiClient)
    client._init_state()
    post = _fake_staff_api(calls, **kw)
    client._post = post

    async def post_page(registry, entry, path, payload, page_size=100, search_text=None,
                        current_page=1):
        if pages and path in pages:
            calls.append((path, payload, search_text, current_page))
            rows = pages[path]
            return rows[current_page - 1], {"number_of_items": sum(map(len, rows)),
                                            "number_of_pages": len(rows)}
        return await post(registry, entry, path, payload, page_size, search_text), None

    client._post_page = post_page
    return client


def test_search_is_one_call_per_register_and_exact():
    calls = []
    entry = _catalog().get(CODE)
    found, complete = asyncio.run(_client(calls).search(CODE, entry, "FAN-" + FID))
    assert complete
    search = [c for c in calls if c[0] == "/register-data/search_in_a_register"]
    # searched with the prefix stripped; the decoy hit is dropped; no record / tab calls
    assert search == [("/register-data/search_in_a_register", {"register_id": REG}, FID)]
    assert [(f["register"], f["registerId"], f["hit"]["internal_record_id"]) for f in found] == [
        ("Livestock", REG, "ls-1")]
    assert not any(c[0].startswith("/register-data/get_") for c in calls)


def test_no_exact_hit_means_no_records():
    calls = []
    entry = _catalog().get(CODE)
    hits = [{"internal_record_id": "x", "link_foundational_id": FID + "1"}]
    assert asyncio.run(_client(calls, hits=hits).search(CODE, entry, FID)) == ([], True)


def test_unknown_register_is_reported():
    entry = _catalog(registers=["Livestock", "Nope"]).get(CODE)
    with pytest.raises(RegistryError) as caught:
        asyncio.run(_client([]).search(CODE, entry, FID))
    assert caught.value.reason == "unknown_register"


def test_metadata_is_cached_and_expires(monkeypatch):
    calls = []
    client = _client(calls)
    entry = _catalog().get(CODE)

    async def twice():
        await client.tabs(CODE, entry, REG)
        await client.tabs(CODE, entry, REG)

    asyncio.run(twice())
    assert sum(c[0].endswith("get_all_tabs") for c in calls) == 1
    monkeypatch.setattr(staff_search_service._config, "staff_api_metadata_ttl_sec", 0)
    asyncio.run(client.tabs(CODE, entry, REG))
    assert sum(c[0].endswith("get_all_tabs") for c in calls) == 2


def test_a_new_tab_is_found_without_waiting_for_the_ttl():
    calls = []
    tabs = list(TABS)
    client = _client(calls, tabs=tabs)
    entry = _catalog().get(CODE)
    assert asyncio.run(client.find_tab(CODE, entry, REG, "irrigation_tab")) is None
    # the registry adds a tab; the cache was loaded long enough ago to refresh
    tabs.append({"tab_id": "irrigation_tab", "tab_label": "Irrigation", "tab_order": 5})
    client._tabs_cache._items[(CODE, REG)] = (0.0, client._tabs_cache._items[(CODE, REG)][1])
    assert asyncio.run(client.find_tab(CODE, entry, REG, "irrigation_tab"))["tab_label"] == (
        "Irrigation")


# ── building the answers ────────────────────────────────────────────────────


def test_fields_come_from_the_ui_schema():
    assert _fields(UI_MAIN, REG) == [
        {"key": "functional_record_id", "label": "Holding ID", "type": "text", "order": 0},
        {"key": "created_at", "label": "Registered", "type": "date", "order": 1}]
    assert [f["key"] for f in _fields(UI_ANIMAL, ANIMAL)] == ["tag", "species"]
    assert _fields(None, REG) == []


def test_composite_widgets_and_static_options():
    geo = {"widget": "geo-hierarchy", "widget-label": "Location",
           "widget-data-path": {"value": REG + ".geo_id", "hierarchy": REG + ".geo_json"}}
    header = {"widget": "header-section", "widget-labels": {"status": "state"},
              "widget-data-path": {"name": REG + ".record_name", "status": REG + ".state"}}
    sel = {"widget": "select", "widget-label": "Sex", "widget-data-path": REG + ".sex",
           "widget-data-source": {"type": "static", "options": [{"label": "Male", "value": "M"}]}}
    fields = _fields({"widgets": [geo, header, sel]}, REG)
    assert [(f["key"], f["label"]) for f in fields] == [
        ("geo_id", "Location"), ("geo_json", "Location (hierarchy)"),
        ("record_name", "name"), ("state", "state"), ("sex", "Sex")]
    assert fields[-1]["options"] == [{"label": "Male", "value": "M"}]


def test_sections_are_paged_and_nothing_is_dropped():
    groups = [{"section_register_id": ANIMAL, "is_list": True,
               "records": [{"tag": i} for i in range(5)]},
              {"section_register_id": "reg-undescribed", "is_list": False, "records": [{"a": 1}]}]
    out = _sections(SECTIONS["animal_tab"], groups, {ANIMAL: "Animal"}, page=2, size=2)
    animal, extra = out
    assert (animal["sectionId"], animal["label"], animal["register"]) == (
        "s-animal", "Animals", "Animal")
    assert animal["records"] == [{"tag": 2}, {"tag": 3}]
    assert animal["pagination"] == {"page": 2, "pageSize": 2, "totalItems": 5, "totalPages": 3}
    # a group no section metadata describes still reaches the client, without fields
    assert (extra["registerId"], extra["fields"], extra["records"]) == (
        "reg-undescribed", [], [{"a": 1}])
    assert extra["pagination"] is None


# ── the service ─────────────────────────────────────────────────────────────


def _identity(roles, subject="u-1"):
    return CallerIdentity(subject=subject, subject_id_type=None, subject_id_value="staff",
                          name="Staff User", roles=roles,
                          raw_claims={"preferred_username": "staff"})


STAFF = _identity(["AGGREGATION_STAFF_VIEWER"])


def test_role_check(monkeypatch):
    monkeypatch.setattr(staff_search_service._config, "staff_search_roles",
                        ["AGGREGATION_STAFF_VIEWER"])
    assert StaffSearchService.allowed_roles(_identity(["AGGREGATION_STAFF_VIEWER", "x"])) == [
        "AGGREGATION_STAFF_VIEWER"]
    assert StaffSearchService.allowed_roles(_identity(["Data Supervisor"])) == []
    monkeypatch.setattr(staff_search_service._config, "staff_search_roles", [])
    assert StaffSearchService.allowed_roles(_identity(["AGGREGATION_STAFF_VIEWER"])) == []


def _service(monkeypatch, catalog, client):
    """The service over ``client``; ``saved`` collects its audit log lines."""
    monkeypatch.setattr(staff_search_service, "get_catalog", lambda: catalog)
    svc = object.__new__(StaffSearchService)
    svc.staff_api = client
    svc._calls = {}
    svc._mac_key = b"k" * 32
    saved = []

    class Collect(logging.Handler):
        def emit(self, record):
            saved.append(json.loads(record.getMessage()))

    handler = Collect()
    audit = staff_search_service._audit
    monkeypatch.setattr(audit, "level", logging.INFO)
    # Setting .level directly skips setLevel's cache reset: a test that logged
    # an audit line earlier would leave "INFO disabled" cached.
    audit._cache.clear()
    monkeypatch.setattr(audit, "handlers", [handler])
    return svc, saved


class _Failing:
    async def search(self, code, entry, fid):
        raise RegistryError(code, "staff_api_forbidden", "HTTP 403")


def test_registry_filter(monkeypatch):
    svc, _ = _service(monkeypatch, _catalog(), None)
    assert svc.registries_for(None) == [CODE]
    with pytest.raises(AggregationError) as caught:
        svc.registries_for(["FARMER_REGISTRY"])
    assert (caught.value.status, caught.value.reason) == (400, "unknown_registry")
    svc, _ = _service(monkeypatch, _catalog(_drop=True), None)
    with pytest.raises(AggregationError) as caught:
        svc.registries_for(None)
    assert caught.value.reason == "staff_search_not_configured"


def test_search_audits_and_isolates_a_failing_registry(monkeypatch):
    svc, saved = _service(monkeypatch, _catalog(), _Failing())
    out = asyncio.run(svc.search(STAFF, " " + FID + " ", purpose="field_verification"))
    reg = out["registries"][0]
    assert (reg["status"], reg["error"]["reason"]) == ("error", "staff_api_forbidden")
    assert out["searchedBy"] == "staff" and out["foundationalId"] == FID
    [audit] = saved
    assert audit["event"] == "staff_search"
    assert (audit["staff_username"], audit["purpose"]) == ("staff", "field_verification")
    # The ID is masked in the log; only the last four characters are kept.
    assert audit["foundational_id"].endswith(FID[-4:]) and FID not in json.dumps(audit)
    assert audit["outcome"] == {CODE: {
        "status": "error", "records": 0,
        "error": {"reason": "staff_api_forbidden", "detail": "HTTP 403"}}}
    with pytest.raises(AggregationError):
        asyncio.run(svc.search(STAFF, "  "))


def test_search_answers_summary_and_tabs_without_tab_data(monkeypatch):
    calls = []
    svc, saved = _service(monkeypatch, _catalog(hidden_tabs=["animal_tab"]), _client(calls))
    out = asyncio.run(svc.search(STAFF, FID))
    [reg] = out["registries"]
    assert reg["status"] == "found"
    [rec] = reg["records"]
    assert (rec["register"], rec["registerId"], rec["recordId"]) == ("Livestock", REG, "ls-1")
    assert rec["summary"]["name"] == "Abeba holding"
    assert rec["summary"]["displayFields"][0] == {
        "name": "kebele_name", "value": "Kebele 03", "order": 2}
    # active, in order, hidden_tabs left out; each with the request that opens it
    assert [t["tabId"] for t in rec["tabs"]] == ["main_tab"]
    # The record's own ticket, not the search's.
    assert rec["searchId"] and rec["searchId"] != out["searchId"]
    assert rec["tabs"][0]["request"] == {"method": "POST", "url": "/aggregation/v1/staff/registry/tab",
                                         "body": {"searchId": rec["searchId"], "registryCode": CODE,
                                                  "registerId": REG, "recordId": "ls-1",
                                                  "tabId": "main_tab"}}
    assert not any(c[0] == "/register-data/get_tab_records" for c in calls)
    assert saved[0]["outcome"][CODE]["records"] == 1


def _search_then(monkeypatch, **catalog):
    calls = []
    svc, saved = _service(monkeypatch, _catalog(**catalog), _client(calls))
    out = asyncio.run(svc.search(STAFF, FID))
    return svc, saved, calls, out


def _ticket_of(out):
    """The found record's ticket (the one in its tab requests)."""
    return out["registries"][0]["records"][0]["searchId"]


def test_tab_answers_one_tab_and_audits(monkeypatch):
    svc, saved, calls, out = _search_then(monkeypatch)
    body = out["registries"][0]["records"][0]["tabs"][1]["request"]["body"]
    assert body["tabId"] == "animal_tab"
    before = len(calls)
    tab = asyncio.run(svc.tab(STAFF, body["searchId"], body["registryCode"], body["registerId"],
                              body["recordId"], body["tabId"], page=1, page_size=2))
    tab_calls = [c[0] for c in calls[before:]]
    assert tab_calls.count("/register-data/get_tab_records") == 1
    assert tab["label"] == "animal_tab"
    [section] = tab["sections"]
    assert [f["key"] for f in section["fields"]] == ["tag", "species"]
    assert section["records"] == [{"tag": "A0"}, {"tag": "A1"}]
    assert section["pagination"]["totalItems"] == 5
    view = saved[-1]
    assert (view["event"], view["search"], view["tab_id"], view["record_id"],
            view["outcome"]["status"]) == (
        "staff_tab_view", saved[0]["search"], "animal_tab", "ls-1", "ok")


@pytest.mark.parametrize("change, status, reason", [
    ({"record_id": "ls-other"}, 400, "record_not_in_search"),
    ({"register_id": ANIMAL}, 400, "record_not_in_search"),
    ({"registry_code": "FARMER_REGISTRY"}, 400, "unknown_registry"),
    ({"tab_id": "hidden_tab"}, 400, "unknown_tab"),
    ({"search_id": "nope"}, 404, "search_not_found"),
    ({"search_id": "e30.forged"}, 404, "search_not_found"),
    ({"identity": _identity(["AGGREGATION_STAFF_VIEWER"], subject="u-2")}, 404,
     "search_not_found"),
    # The search's own searchId names the search; it opens no record.
    ({"search_id": "TOP"}, 400, "record_not_in_search"),
])
def test_tab_is_bound_to_its_search(monkeypatch, change, status, reason):
    svc, saved, calls, out = _search_then(monkeypatch)
    if change.get("search_id") == "TOP":
        change = {"search_id": out["searchId"]}
    args = {"identity": STAFF, "search_id": _ticket_of(out), "registry_code": CODE,
            "register_id": REG, "record_id": "ls-1", "tab_id": "main_tab"} | change
    with pytest.raises(AggregationError) as caught:
        asyncio.run(svc.tab(**args))
    assert (caught.value.status, caught.value.reason) == (status, reason)
    assert not any(c[0] == "/register-data/get_tab_records" for c in calls)


def test_hidden_tab_cannot_be_opened(monkeypatch):
    svc, _, _, out = _search_then(monkeypatch, hidden_tabs=["animal_tab"])
    with pytest.raises(AggregationError) as caught:
        asyncio.run(svc.tab(STAFF, _ticket_of(out), CODE, REG, "ls-1", "animal_tab"))
    assert caught.value.reason == "unknown_tab"


def test_a_ticket_cannot_be_altered(monkeypatch):
    svc, _, _, out = _search_then(monkeypatch)
    ticket = _ticket_of(out)
    body, mac = ticket.split(".")
    search = svc._search_of(STAFF, ticket)
    search["r"] = [CODE, "anything", "ls-other"]
    forged = svc._ticket(search).split(".")[0] + "." + mac
    with pytest.raises(AggregationError) as caught:
        asyncio.run(svc.tab(STAFF, forged, CODE, "anything", "ls-other", "main_tab"))
    assert caught.value.reason == "search_not_found"
    # Another key (another deployment) cannot read it either.
    svc._mac_key = b"x" * 32
    with pytest.raises(AggregationError):
        svc._search_of(STAFF, ticket)


def test_ticket_size_does_not_grow_with_records(monkeypatch):
    """At the largest page the search answers, every tab request it hands out
    is accepted by the tab endpoint (whose searchId limit is 8,192)."""
    from openg2p_aggregation_layer.schemas.staff import StaffTabRequest

    hits = [dict(HIT, internal_record_id="ls-%03d-%s" % (i, "x" * 200)) for i in range(100)]
    calls = []
    svc, _ = _service(monkeypatch, _catalog(page_size=100), _client(calls, hits=hits))
    out = asyncio.run(svc.search(STAFF, FID))
    records = out["registries"][0]["records"]
    assert len(records) == 100
    sizes = {len(r["searchId"]) for r in records}
    assert max(sizes) < 1024
    for rec in (records[0], records[-1]):
        body = rec["tabs"][0]["request"]["body"]
        StaffTabRequest(**body)                       # the schema accepts it
        tab = asyncio.run(svc.tab(STAFF, body["searchId"], body["registryCode"],
                                  body["registerId"], body["recordId"], body["tabId"]))
        assert tab["sections"]
    # A record's ticket does not open another record.
    first = records[0]["tabs"][0]["request"]["body"]
    with pytest.raises(AggregationError) as caught:
        asyncio.run(svc.tab(STAFF, first["searchId"], CODE, REG, records[1]["recordId"],
                            "main_tab"))
    assert caught.value.reason == "record_not_in_search"


def test_ticket_from_before_the_change_still_opens_its_records(monkeypatch):
    """A ticket listing the records found (issued before per-record tickets,
    still within its TTL) keeps working until it expires."""
    svc, _, _, out = _search_then(monkeypatch)
    legacy = svc._ticket({"n": "x", "sub": STAFF.subject, "exp": int(time.time()) + 60,
                          "found": {CODE: [[REG, "ls-1"]]}})
    assert asyncio.run(svc.tab(STAFF, legacy, CODE, REG, "ls-1", "main_tab"))["sections"]
    with pytest.raises(AggregationError) as caught:
        asyncio.run(svc.tab(STAFF, legacy, CODE, REG, "ls-other", "main_tab"))
    assert caught.value.reason == "record_not_in_search"


# ── pagination of staff-api lists ────────────────────────────────────────────

def test_search_reads_every_page_for_the_exact_match(monkeypatch):
    """Page 1 holds only substring matches; the person is on page 2."""
    decoy = {"internal_record_id": "ls-decoy", "link_foundational_id": FID + "0"}
    calls = []
    client = _client(calls, pages={"/register-data/search_in_a_register": [[decoy], [HIT]]})
    found, complete = asyncio.run(client.search(CODE, _catalog().get(CODE), FID))
    assert complete and [f["hit"]["internal_record_id"] for f in found] == ["ls-1"]
    pages = [c[3] for c in calls if c[0] == "/register-data/search_in_a_register"]
    assert pages == [1, 2]


def test_search_cut_short_is_incomplete_not_absent(monkeypatch):
    decoy = {"internal_record_id": "ls-decoy", "link_foundational_id": FID + "0"}
    monkeypatch.setattr(staff_search_service._config, "staff_api_max_pages", 2)
    calls = []
    client = _client(calls, pages={
        "/register-data/search_in_a_register": [[decoy], [decoy], [HIT]]})
    svc, _ = _service(monkeypatch, _catalog(), client)
    out = asyncio.run(svc.search(STAFF, FID))
    [reg] = out["registries"]
    assert (reg["status"], reg["complete"], reg["records"]) == ("incomplete", False, [])

    # Found within the cap, but more pages exist: found, and says so.
    client = _client([], pages={"/register-data/search_in_a_register": [[HIT], [decoy], [decoy]]})
    svc, _ = _service(monkeypatch, _catalog(), client)
    [reg] = asyncio.run(svc.search(STAFF, FID))["registries"]
    assert (reg["status"], reg["complete"], len(reg["records"])) == ("found", False, 1)


def test_metadata_lists_read_every_page(monkeypatch):
    calls = []
    client = _client(calls, pages={"/register-metadata/get_all_registers": [
        [{"register_mnemonic": "Animal", "register_id": ANIMAL}],
        [{"register_mnemonic": "Livestock", "register_id": REG}]]})
    ids = asyncio.run(client.register_ids(CODE, _catalog().get(CODE)))
    assert ids == {"Livestock": REG}


def test_expired_search_cannot_open_tabs(monkeypatch):
    svc, saved, _, out = _search_then(monkeypatch)
    real = time.time
    monkeypatch.setattr(staff_search_service.time, "time", lambda: real() + 7200)
    with pytest.raises(AggregationError) as caught:
        asyncio.run(svc.tab(STAFF, _ticket_of(out), CODE, REG, "ls-1", "main_tab"))
    assert (caught.value.status, caught.value.reason) == (410, "search_expired")


def test_rate_limit(monkeypatch):
    svc, _ = _service(monkeypatch, _catalog(), None)
    monkeypatch.setattr(staff_search_service._config, "staff_rate_limit_per_minute", 2)
    svc.check_rate(STAFF)
    svc.check_rate(STAFF)
    with pytest.raises(AggregationError) as caught:
        svc.check_rate(STAFF)
    assert caught.value.status == 429
    svc.check_rate(_identity([], subject="someone-else"))   # per user
    monkeypatch.setattr(staff_search_service._config, "staff_rate_limit_per_minute", 0)
    svc.check_rate(STAFF)                                     # off
