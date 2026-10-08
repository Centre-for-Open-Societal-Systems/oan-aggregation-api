"""A fourth registry, added purely through the catalog, is queried and mapped.

``four-registries.yaml`` is ``three-registries.yaml`` plus WORKER_REGISTRY. No
code knows that name: the real RegistryClient builds each DCI hop from the
catalog entry, the fan-out walks the catalog, and the mapping places the
record by the catalog. Only the network is faked (httpx.MockTransport), with a
registry that clamps to the hop's consent scopes as a real one does.
"""
import asyncio
import json
from types import SimpleNamespace

import httpx
import jwt
import pytest
from conftest import (
    DISABILITY_RECORD,
    FARMER_RECORD,
    NSR_RECORD,
    WORKER_RECORD,
    assert_valid,
)

from openg2p_aggregation_layer import bene360

FID = "7615076397"
RECORDS = {
    "farmer.registry.test": FARMER_RECORD,
    "disability.registry.test": DISABILITY_RECORD,
    "nsr.registry.test": NSR_RECORD,
    "worker.registry.test": WORKER_RECORD,
}


@pytest.fixture
def service(four, monkeypatch):
    from openg2p_aggregation_layer.services import aggregator_service
    from openg2p_aggregation_layer.services.crypto_service import CryptoService
    from openg2p_aggregation_layer.services.registry_client import RegistryClient

    CryptoService()                 # ephemeral key: nothing verifies it here
    client = RegistryClient()
    calls = []

    def registry(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        criteria = body["message"]["search_request"][0]["search_criteria"]
        claims = jwt.decode(criteria["authorize"]["consent_jws"],
                            options={"verify_signature": False})
        calls.append({"url": str(request.url), "header": body["header"],
                      "criteria": criteria, "claims": claims,
                      "signature": body.get("signature")})
        # A registry clamps its records to the hop's top-level scopes.
        records = [{k: v for k, v in r.items() if k in claims["data_scopes"]}
                   for r in [RECORDS[request.url.host]] + decoys.get(request.url.host, [])]
        return httpx.Response(200, json={
            "header": {"action": "on-search", "status": "succ"},
            "message": {"search_response": [{"status": "succ",
                                             "data": {"reg_records": records}}]}})

    decoys = {}

    real_client = httpx.AsyncClient
    monkeypatch.setattr(httpx, "AsyncClient", lambda **kw: real_client(
        transport=httpx.MockTransport(registry), **kw))
    monkeypatch.setattr(aggregator_service, "get_catalog", lambda: four)

    svc = object.__new__(aggregator_service.AggregatorService)
    svc.registries = client

    async def active_grant(request, registry_code):
        return SimpleNamespace(id="grant-" + registry_code)

    svc._active_grant = active_grant
    svc.calls = calls
    svc.decoys = decoys
    return svc


def _row(scopes):
    return SimpleNamespace(
        id="agg-4", correlation_id="c" * 32, transaction_id="t-1", reference_id="r-1",
        query={"@context": bene360.CONTEXT_URL, "foundationalId": FID,
               "timeframe": "Timeframe-Short", "sections": ["REGISTRIES"]},
        requested_scopes=scopes, subject_id_type="national_id", subject_id_value=FID,
        purpose={"code": "loan_origination"}, partner_audience="partner-x",
        partner_id="p-1", lawful_basis="consent", otp_required=True)


def test_fourth_registry_needs_no_code(four, service, response_schema):
    request = _row(four.scope_ids())
    outcomes = asyncio.run(service._query_registries(request))
    assert sorted(outcomes) == sorted(four.codes())
    assert all(o["status"] == "ok" for o in outcomes.values())

    # The hop to the new registry is built entirely from its catalog entry.
    [hop] = [c for c in service.calls if "worker.registry.test" in c["url"]]
    assert hop["url"] == "http://worker.registry.test/dci/registry/sync/search"
    assert hop["header"]["receiver_id"] == "worker-registry"
    assert hop["criteria"]["reg_type"] == "Worker"
    assert hop["criteria"]["reg_record_type"] == "WorkerRecord"
    assert hop["criteria"]["query"]["value"] == {"id_type": "national_id", "id_value": FID}
    assert hop["claims"]["aud"] == "agg-worker"
    assert hop["claims"]["data_controller"] == "worker_registry"
    assert hop["claims"]["data_scopes"] == ["contracts", "employment"]
    assert hop["claims"]["subject_id"] == {"type": "national_id", "value": FID}
    assert hop["signature"] and ".." in hop["signature"]   # detached JWS

    envelope = service._build_envelope(request, outcomes)
    assert envelope["header"]["action"] == "on-search"
    assert envelope["header"]["status"] == "succ"
    data = envelope["message"]["search_response"][0]["data"]
    assert data["reg_type"] == bene360.DCI_REG_TYPE
    [response] = data["reg_records"]
    assert_valid(response_schema, response)

    worker = next(r for r in response["registries"] if r["registryCode"] == "WORKER_REGISTRY")
    assert worker["registryName"] == "Worker-Registry"
    [register] = worker["registers"]
    assert register["registerMnemonic"] == "WORKER"
    assert register["functionalRecordId"] == "W-77"
    assert register["recordStatus"] == "EMPLOYED"
    # Field-level filter: salary and the employer's tax id never leave.
    assert register["attributes"] == {"employment": {"employer": {"name": "BuildCo"},
                                                     "occupation": "mason"}}
    [contract] = register["tables"]
    assert contract["tableMnemonic"] == "CONTRACT"
    assert contract["attributes"] == {"contract_no": "K1", "start_date": "2026-01-01"}
    assert response["beneficiary"]["matchedRegistryCount"] == 4
    assert response["meta"]["sourceSystemsQueried"] == four.codes()
    assert response["meta"]["warnings"] == []


def test_registry_outside_the_consent_is_not_called(four, service, response_schema):
    scopes = [s for s in four.scope_ids() if not s.startswith("WORKER_REGISTRY.")]
    request = _row(scopes)
    outcomes = asyncio.run(service._query_registries(request))
    assert outcomes["WORKER_REGISTRY"] == {"status": "not_consented"}
    assert not [c for c in service.calls if "worker.registry.test" in c["url"]]
    [response] = service._build_envelope(request, outcomes)["message"][
        "search_response"][0]["data"]["reg_records"]
    assert_valid(response_schema, response)
    assert {"system": "WORKER_REGISTRY", "code": "NOT_CONSENTED",
            "message": bene360._WARNINGS["not_consented"][1]} in response["meta"]["warnings"]


def test_a_record_that_only_contains_the_id_is_discarded(four, service, response_schema):
    # The partner API matches search_text by substring: a record whose
    # identifier merely contains the ID (or is another type) comes back too.
    service.decoys["worker.registry.test"] = [
        {"employment": {"worker_no": "W-99", "occupation": "thief",
                        "national_id": {"kind": "national_id", "number": FID + "1"}}},
        {"employment": {"worker_no": "W-98", "occupation": "thief",
                        "national_id": {"kind": "passport", "number": FID}}},
    ]
    request = _row(four.scope_ids())
    outcomes = asyncio.run(service._query_registries(request))
    worker = outcomes["WORKER_REGISTRY"]
    assert worker["records"] == 1 and worker["discarded"] == 2
    [register] = worker["membership"]["registers"]
    assert register["functionalRecordId"] == "W-77"

    [response] = service._build_envelope(request, outcomes)["message"][
        "search_response"][0]["data"]["reg_records"]
    assert_valid(response_schema, response)
    assert "thief" not in json.dumps(response)
    assert {"system": "WORKER_REGISTRY", "code": "IDENTIFIER_MISMATCH",
            "message": "2 record(s) matched the search but not the foundational ID "
                       "exactly; discarded"} in response["meta"]["warnings"]


def test_identifier_block_is_fetched_for_the_check_but_not_released(four, service):
    # Only the contracts block is consented; the identifier lives in employment.
    request = _row(["WORKER_REGISTRY.contracts"])
    request.query["registryFilter"] = ["WORKER_REGISTRY"]
    outcomes = asyncio.run(service._query_registries(request))
    [hop] = [c for c in service.calls if "worker.registry.test" in c["url"]]
    assert hop["claims"]["data_scopes"] == ["contracts", "employment"]
    worker = outcomes["WORKER_REGISTRY"]
    assert worker["scopes"] == ["contracts"] and worker["discarded"] == 0
    [register] = worker["membership"]["registers"]
    assert "employment" not in register.get("attributes", {})
    assert "mason" not in json.dumps(worker["membership"])
    assert [t["tableMnemonic"] for t in register["tables"]] == ["CONTRACT"]
