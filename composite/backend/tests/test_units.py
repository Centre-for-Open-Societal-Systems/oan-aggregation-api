"""Templates, mapping and derived values, consent checks, DCI status mapping, rate limiting."""

import os
from datetime import datetime, timedelta, timezone

import pytest
import yaml
from conftest import FAN, FARMER_ID, FARMER_RECORD, USE_CASES, dci_response
from openg2p_agri_composite.core import dci
from openg2p_agri_composite.core.consent import ConsentError, check_consent, decode_claims
from openg2p_agri_composite.core.expr import ExpressionError, JsonPath, evaluate, parse_derived, set_path
from openg2p_agri_composite.core.loader import compile_use_case
from openg2p_agri_composite.core.models import SourceSpec
from openg2p_agri_composite.core.ratelimit import RateLimiter
from openg2p_agri_composite.core.templates import TemplateRenderError, compile_template, render_query


@pytest.fixture(scope="module")
def loan():
    with open(os.path.join(USE_CASES, "loan-profile.yaml")) as fh:
        return compile_use_case(yaml.safe_load(fh), "/tmp/l.yaml", ["farmer-registry", "crop-sown-registry"])


def ctx(subject, params=None, farmer_records=None):
    return {
        "subject": subject,
        "parameters": {"crop_year": None, "season": None, **(params or {})},
        "sources": {"farmer": {"status": "ok", "records": farmer_records or []}},
        "request_id": "r1", "today": "2026-10-01", "use_case": "loan-profile@1",
    }


# ── templates ────────────────────────────────────────────────────────────────

def test_farmer_template_picks_the_id_field(loan):
    q = render_query(loan.templates["farmer"], ctx({"type": "FAYDA_FAN", "value": FAN}))
    assert q["query_type"] == "expression"
    assert q["query"]["value"]["expression"]["query"] == {"foundational_id": {"$eq": FAN}}
    q = render_query(loan.templates["farmer"], ctx({"type": "FARMER_ID", "value": FARMER_ID}))
    assert q["query"]["value"]["expression"]["query"] == {"functional_record_id": {"$eq": FARMER_ID}}


def test_crop_templates_read_farmer_id_and_filters(loan):
    c = ctx({"type": "FAYDA_FAN", "value": FAN}, {"crop_year": 2019, "season": "SEASON_BELG"}, [FARMER_RECORD])
    q = render_query(loan.templates["season_summaries"], c)["query"]["value"]["expression"]["query"]
    assert q == {"subject_id": FARMER_ID, "aggregate_type": "FARMER_SEASON_SUMMARY", "crop_year": 2019, "season": "SEASON_BELG"}
    q = render_query(loan.templates["crop_seasons"], ctx({"type": "FAYDA_FAN", "value": FAN}, None, [FARMER_RECORD]))
    assert q["query"]["value"]["expression"]["query"] == {"subject_id": FARMER_ID}
    assert q["pagination"] == {"page_size": 100, "page_number": 1}


def test_template_injection_is_escaped(loan):
    evil = 'x"}, "other": {"$ne": "'
    q = render_query(loan.templates["farmer"], ctx({"type": "FAYDA_FAN", "value": evil}))
    assert q["query"]["value"]["expression"]["query"] == {"foundational_id": {"$eq": evil}}


def test_sandbox_blocks_unsafe_access():
    # Private attributes are hidden by the sandbox (rendered empty, never reached).
    t = compile_template('{"query": {"type": "x", "value": "{{ "".__class__.__mro__ }}{{ subject.__init__.__globals__ }}"}}')
    assert render_query(t, {"subject": {}})["query"]["value"] == ""
    t = compile_template('{% set _ = parameters.update({"a": 1}) %}{"query": {"type": "x", "value": 1}}')
    with pytest.raises(TemplateRenderError):
        render_query(t, {"parameters": {}})


def test_template_output_keys_checked():
    t = compile_template('{"query": {"type": "x", "value": 1}, "reg_type": "Other"}')
    with pytest.raises(TemplateRenderError, match="unexpected keys"):
        render_query(t, {})


# ── mapping and derived ──────────────────────────────────────────────────────

DATA = {"sources": {"f": {"records": [{"land": [{"s": 1.5}, {"s": 2}, {"s": None}], "name": "A",
                                       "ids": [{"t": "UIN", "v": "1"}, {"t": "FARMER_ID", "v": "FR-1"}]}]},
                    "empty": {"records": []}}}


def test_jsonpath_single_vs_multi():
    assert JsonPath("$.sources.f.records[0].name").value(DATA) == "A"
    assert JsonPath("$.sources.f.records[0].missing").value(DATA) is None
    assert JsonPath("$.sources.f.records[0].land[*].s").value(DATA) == [1.5, 2, None]
    assert JsonPath("$.sources.empty.records").value(DATA) == []
    assert JsonPath('$.sources.f.records[0].ids[?(@.t == "FARMER_ID")].v').value(DATA) == ["FR-1"]
    with pytest.raises(ExpressionError):
        JsonPath("$..[")


@pytest.mark.parametrize("expr, expected", [
    ("sum($.sources.f.records[0].land[*].s)", 3.5),
    ("count($.sources.f.records[0].land)", 3),
    ("count($.sources.empty.records)", 0),
    ("min($.sources.f.records[0].land[*].s)", 1.5),
    ("max($.sources.f.records[0].land[*].s, 7)", 7),
    ("first($.sources.f.records[0].land[*].s)", 1.5),
    ("first($.sources.empty.records)", None),
    ("round(sum($.sources.f.records[0].land[*].s, 0.333), 2)", 3.83),
    ("sum($.sources.empty.records[*].x)", 0),
    ("max($.sources.empty.records[*].x)", None),
    ("round($.sources.f.records[0].name)", None),
    ('first($.sources.f.records[0].ids[?(@.t == "FARMER_ID")].v)', "FR-1"),
])
def test_derived(expr, expected):
    assert evaluate(parse_derived(expr), DATA) == expected


@pytest.mark.parametrize("expr", ["", "sum(", "sum()", "count($.a, $.b)", "__import__('os')", "sum($.a) + 1", "open($.a)"])
def test_derived_rejects(expr):
    with pytest.raises(ExpressionError):
        parse_derived(expr)


def test_set_path():
    out = {}
    set_path(out, "a.b.c", 1)
    set_path(out, "a.d", 2)
    assert out == {"a": {"b": {"c": 1}, "d": 2}}


# ── consent ──────────────────────────────────────────────────────────────────

def _sources():
    return [
        SourceSpec(id="farmer", controller="farmer-registry", requirement="mandatory",
                   dci={"reg_type": "Farmer", "reg_record_type": "x", "query_template": "t"}),
        SourceSpec(id="crops", controller="crop-sown-registry", requirement="optional",
                   dci={"reg_type": "CropSown", "reg_record_type": "x", "query_template": "t"}),
    ]


def _claims(**kw):
    c = {"subject_id": {"type": "FAYDA_FAN", "value": FAN},
         "grants": [{"data_controller": "farmer-registry", "data_scopes": []},
                    {"data_controller": "crop-sown-registry", "data_scopes": []}]}
    c.update(kw)
    return c


SUBJECT = {"type": "FAYDA_FAN", "value": FAN}


def test_consent_all_granted():
    assert check_consent(_claims(), SUBJECT, _sources()) == {"farmer": True, "crops": True}


def test_consent_subject_mismatch():
    with pytest.raises(ConsentError) as e:
        check_consent(_claims(subject_id={"type": "FAYDA_FAN", "value": "1"}), SUBJECT, _sources())
    assert e.value.code == "consent_subject_mismatch"
    with pytest.raises(ConsentError):
        check_consent(_claims(subject_id={"type": "FARMER_ID", "value": FAN}), SUBJECT, _sources())


def test_consent_missing_grants():
    only_fr = _claims(grants=[{"data_controller": "farmer-registry", "data_scopes": []}])
    assert check_consent(only_fr, SUBJECT, _sources()) == {"farmer": True, "crops": False}
    only_csr = _claims(grants=[{"data_controller": "crop-sown-registry", "data_scopes": []}])
    with pytest.raises(ConsentError) as e:
        check_consent(only_csr, SUBJECT, _sources())
    assert e.value.code == "consent_grant_missing"


def test_consent_legacy_and_malformed():
    legacy = _claims(grants=None, data_controller="farmer-registry", data_scopes=["a"])
    del legacy["grants"]
    assert check_consent(legacy, SUBJECT, _sources())["crops"] is False
    with pytest.raises(ConsentError):
        check_consent(_claims(grants=[]), SUBJECT, _sources())
    with pytest.raises(ConsentError):
        decode_claims("abc")
    with pytest.raises(ConsentError):
        decode_claims("a.!!!.c")


def test_consent_validity():
    now = datetime.now(timezone.utc)
    expired = _claims(validity={"valid_from": (now - timedelta(days=9)).isoformat(),
                                "valid_until": (now - timedelta(days=1)).isoformat()})
    with pytest.raises(ConsentError) as e:
        check_consent(expired, SUBJECT, _sources())
    assert e.value.code == "consent_expired"


# ── DCI status mapping ───────────────────────────────────────────────────────

REQ = {"header": {"message_id": "m", "message_ts": "t", "sender_id": "agri-composite", "receiver_id": "fr"},
       "message": {"transaction_id": "tx", "search_request": [{"reference_id": "r"}]}}


@pytest.mark.parametrize("code, status", [(401, "denied"), (403, "denied"), (429, "unavailable"), (500, "unavailable"),
                                          (503, "unavailable"), (400, "error"), (404, "error"), (422, "error")])
def test_classify_http(code, status):
    assert dci.classify_http(code)[0] == status
    assert dci.classify_http(200) is None


def test_classify_body():
    assert dci.classify_body(dci_response(REQ, [{"a": 1}]))[:2] == ("ok", [{"a": 1}])
    assert dci.classify_body(dci_response(REQ, []))[0] == "no_record"
    assert dci.classify_body(dci_response(REQ, status="rjct", reason="Consent denied: missing_consent"))[0] == "denied"
    assert dci.classify_body(dci_response(REQ, status="rjct", reason="SYS-ERR-001"))[0] == "error"
    assert dci.classify_body(dci_response(REQ, item_status="rjct",
                                          reason="The consent's subject is not the person searched"))[0] == "denied"
    assert dci.classify_body(dci_response(REQ, item_status="rjct", reason="Field 'x' cannot be filtered here"))[0] == "error"
    assert dci.classify_body({"header": {}, "message": {"search_response": []}})[0] == "error"
    assert dci.classify_body("nope")[0] == "error"


def test_build_search_shape():
    env = dci.build_search(request_id="rid", source_id="farmer", sender_id="agri-composite", receiver_id="farmer-registry",
                           on_behalf_of="bank-a", use_case="loan-profile@1", reg_type="Farmer",
                           reg_record_type="spdci-extensions-dci:Farmer",
                           rendered={"query_type": "expression", "query": {"type": "expression", "value": {}}},
                           consent_jws="h.p.s")
    crit = env["message"]["search_request"][0]["search_criteria"]
    assert crit["authorize"] == {"consent_jws": "h.p.s"} and crit["reg_type"] == "Farmer"
    assert env["header"]["meta"]["on_behalf_of"] == "bank-a"
    assert env["message"]["search_request"][0]["reference_id"] == "rid-farmer"


# ── rate limiting ────────────────────────────────────────────────────────────

def test_token_bucket():
    now = [0.0]
    rl = RateLimiter(clock=lambda: now[0])
    assert [rl.allow("p", "u@1", 2, 60) for _ in range(3)] == [True, True, False]
    assert rl.allow("other", "u@1", 2, 60) is True  # per partner
    now[0] += 30  # half the period refills one token
    assert rl.allow("p", "u@1", 2, 60) is True
    assert rl.allow("p", "u@1", 2, 60) is False


def _redis_limiters(n, now):
    import fakeredis

    server = fakeredis.FakeServer()
    return server, [
        RateLimiter(redis_client=fakeredis.FakeAsyncRedis(server=server), redis_clock=lambda: now[0])
        for _ in range(n)
    ]


async def test_redis_token_bucket_allow_deny_refill_and_ttl():
    now = [1000.0]
    server, (rl,) = _redis_limiters(1, now)
    assert [await rl.acquire("p", "u@1", 2, 60) for _ in range(3)] == [True, True, False]
    assert await rl.acquire("other", "u@1", 2, 60) is True  # per partner
    assert await rl.acquire("p", "u@2", 2, 60) is True  # per use case
    now[0] += 30  # half the period refills one token
    assert await rl.acquire("p", "u@1", 2, 60) is True
    assert await rl.acquire("p", "u@1", 2, 60) is False
    ttl = await rl._redis.ttl("agri-composite:ratelimit:p:u@1")
    assert 0 < ttl <= 60  # an idle bucket expires once it would be full again


async def test_redis_bucket_uses_the_redis_server_clock_by_default():
    import fakeredis

    rl = RateLimiter(redis_client=fakeredis.FakeAsyncRedis(server=fakeredis.FakeServer()))
    assert [await rl.acquire("p", "u@1", 2, 3600) for _ in range(3)] == [True, True, False]


async def test_redis_bucket_is_shared_across_limiters():
    # Two limiters on one Redis = two pods: together they get the configured rate, not twice it.
    now = [1000.0]
    _, (pod_a, pod_b) = _redis_limiters(2, now)
    results = [await (pod_a if i % 2 else pod_b).acquire("p", "u@1", 3, 60) for i in range(6)]
    assert results == [True, True, True, False, False, False]


async def test_redis_script_reloaded_after_script_flush():
    now = [1000.0]
    _, (rl,) = _redis_limiters(1, now)
    assert await rl.acquire("p", "u@1", 2, 60) is True
    await rl._redis.script_flush()  # EVALSHA → NOSCRIPT → EVAL
    assert await rl.acquire("p", "u@1", 2, 60) is True
    assert await rl.acquire("p", "u@1", 2, 60) is False


async def test_redis_down_falls_back_to_local_bucket(caplog):
    import logging

    from redis.exceptions import ConnectionError as RedisConnectionError

    class DownRedis:
        calls = 0

        def register_script(self, _):
            async def run(**_):
                DownRedis.calls += 1
                raise RedisConnectionError("connection refused")
            return run

    now = [0.0]
    rl = RateLimiter(clock=lambda: now[0], redis_client=DownRedis(), redis_retry_seconds=60)
    with caplog.at_level(logging.WARNING):
        assert [await rl.acquire("p", "u@1", 2, 60) for _ in range(3)] == [True, True, False]
    assert DownRedis.calls == 1  # not retried on every call while down
    assert "unreachable" in caplog.text


async def test_unreachable_redis_url_falls_back():
    rl = RateLimiter(redis_url="redis://127.0.0.1:1/0", redis_timeout_seconds=0.2)
    assert rl.shared
    assert await rl.acquire("p", "u@1", 1, 60) is True
    assert await rl.acquire("p", "u@1", 1, 60) is False
    await rl.close()


def test_requested_scopes_take_the_whole_grant_for_a_registry_with_an_undeclared_source():
    from openg2p_agri_composite.core.consent import requested_scopes

    dci_spec = {"reg_type": "Farmer", "reg_record_type": "x", "query_template": "{}"}
    s1 = SourceSpec(id="s1", controller="fr", scopes=["fr.a"], dci=dci_spec)
    s2 = SourceSpec(id="s2", controller="fr", dci=dci_spec)
    s3 = SourceSpec(id="s3", controller="csr", scopes=["csr.x"], optional_scopes=["csr.y"], dci=dci_spec)
    claims = {"grants": [{"data_controller": "fr", "data_scopes": ["fr.a", "fr.b", "fr.c"]},
                         {"data_controller": "csr", "data_scopes": ["csr.x", "csr.y", "csr.z"]}]}
    assert requested_scopes(claims, [s1, s2, s3]) == ["csr.x", "csr.y", "fr.a", "fr.b", "fr.c"]
    assert requested_scopes(claims, [s1, s3]) == ["csr.x", "csr.y", "fr.a"]
