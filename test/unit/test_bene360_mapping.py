"""A registry record -> bene-360 registers / tables, and the response around them."""
from datetime import datetime, timezone

import pytest
from conftest import DISABILITY_RECORD, FARMER_RECORD, NSR_RECORD

from openg2p_aggregation_layer import bene360

FID = "7615076397"
NOW = datetime(2026, 10, 8, 9, 30, tzinfo=timezone.utc)


def _tables(entry, mnemonic):
    return [t for t in entry["tables"] if t["tableMnemonic"] == mnemonic]


def test_register_identifiers_and_filtered_attributes(three):
    entry = three.get("FARMER_REGISTRY")
    [farmer] = bene360.map_record(entry, FARMER_RECORD, ["person"], FID)
    assert farmer["registerMnemonic"] == "FARMER"
    assert farmer["registerName"] == "Farmer"
    assert farmer["internalRecordId"] == "f-0001"
    assert farmer["functionalRecordId"] == "7615076397"
    assert farmer["foundationalId"] == FID
    assert farmer["recordStatus"] == "ACTIVE"
    assert farmer["lastApprovedAt"] == "2026-07-01T10:00:00Z"
    # Grouped by the block (consent scope) the values came from; only the
    # listed paths, and the empty phone is not data.
    assert farmer["attributes"] == {"person": {
        "name": {"given": "Mary", "family": "Bell"},
        "ids": [{"value": "7615076397"}]}}
    assert farmer["tables"] == []


def test_tables_and_nested_tables(three):
    entry = three.get("FARMER_REGISTRY")
    [farmer] = bene360.map_record(entry, FARMER_RECORD, ["person", "land", "household"], FID)
    lands = _tables(farmer, "LAND")
    assert [land["internalRecordId"] for land in lands] == ["L1", "L2"]
    assert lands[0]["linkInternalRecordId"] == "f-0001"
    assert lands[0]["attributes"] == {"land_id": "L1", "size": 2.5, "unit": "ha"}
    crops = lands[0]["tables"]
    assert [c["internalRecordId"] for c in crops] == ["C1", "C2"]
    assert crops[0]["linkInternalRecordId"] == "L1"
    assert crops[0]["attributes"] == {"commodity": "MAIZE", "season": "long"}
    assert crops[1]["attributes"] == {"commodity": "TEFF"}
    assert lands[1]["tables"] == []
    [household] = _tables(farmer, "HOUSEHOLD")
    # No id path configured: a positional id, unique within the response.
    assert household["internalRecordId"] == "HOUSEHOLD-1"
    assert household["attributes"] == {"size": 5}


def test_ungranted_blocks_are_never_read(three):
    entry = three.get("FARMER_REGISTRY")
    # The registry should have clamped, but the mapping does not trust that.
    [farmer] = bene360.map_record(entry, FARMER_RECORD, ["land"], FID)
    assert farmer["attributes"] == {}
    assert _tables(farmer, "LAND") and not _tables(farmer, "HOUSEHOLD")
    assert bene360.map_record(entry, FARMER_RECORD, [], FID) == []


def test_table_under_table_by_join(three):
    entry = three.get("NATIONAL_SOCIAL_REGISTRY")
    [social] = bene360.map_record(entry, NSR_RECORD, ["socio", "assets", "valuations"], FID)
    assert social["attributes"] == {"socio": NSR_RECORD["socio"]}   # '*'
    assert social["recordStatus"] == "UNKNOWN"                       # nothing configured
    assets = _tables(social, "ASSET")
    assert [a["attributes"] for a in assets] == [{"asset_id": "A1", "kind": "cow"},
                                                 {"asset_id": "A2", "kind": "plough"}]
    assert [v["attributes"]["amount"] for v in assets[0]["tables"]] == [300]
    assert assets[0]["tables"][0]["linkInternalRecordId"] == "A1"
    assert [v["attributes"]["amount"] for v in assets[1]["tables"]] == [40]
    # A row whose key matches no parent is kept, under its root register.
    orphans = _tables(social, "VALUATION")
    assert [v["attributes"]["amount"] for v in orphans] == [1]


def test_default_record_status(three):
    [case] = bene360.map_record(three.get("DISABILITY_REGISTRY"), DISABILITY_RECORD,
                                ["case"], FID)
    assert case["recordStatus"] == "ACTIVE"
    assert case["attributes"] == {"case": {"disability_type": "MOBILITY"}}


def test_no_data_means_no_membership(three):
    entry = three.get("DISABILITY_REGISTRY")
    assert bene360.map_registry("DISABILITY_REGISTRY", entry, [], ["case"], FID) is None
    assert bene360.map_registry("DISABILITY_REGISTRY", entry, [{"case": {}}], ["case"],
                                FID) is None


def _query(**extra):
    query = {"@context": bene360.CONTEXT_URL, "foundationalId": FID,
             "timeframe": "Timeframe-Medium"}
    query.update(extra)
    return query


def _membership(three, code, record, scopes):
    return bene360.map_registry(code, three.get(code), [record], scopes, FID)


def test_response_reports_every_registry_that_gave_nothing(three):
    query = _query(registryFilter=["FARMER_REGISTRY", "DISABILITY_REGISTRY",
                                   "NATIONAL_SOCIAL_REGISTRY", "UNKNOWN_REGISTRY"],
                   correlationId="corr-1")
    outcomes = {
        "FARMER_REGISTRY": {"status": "ok", "membership": _membership(
            three, "FARMER_REGISTRY", FARMER_RECORD, ["person"])},
        "DISABILITY_REGISTRY": {"status": "error", "reason": "unreachable",
                                "detail": "connect timeout"},
        "NATIONAL_SOCIAL_REGISTRY": {"status": "not_consented"},
    }
    response = bene360.build_response(catalog=three, query=query, outcomes=outcomes,
                                      response_id="urn:openg2p:aggregation:1",
                                      generated_at=NOW)
    assert response["@type"] == "Beneficiary360Response"
    assert response["beneficiary"] == {"foundationalId": FID, "matchedRegistryCount": 1}
    assert [r["registryCode"] for r in response["registries"]] == ["FARMER_REGISTRY"]
    assert response["programs"] == [] and response["bridgeProcessing"] == []
    meta = response["meta"]
    assert meta["generatedAt"] == "2026-10-08T09:30:00Z"
    assert meta["correlationId"] == "corr-1"
    assert meta["requestParameters"] == {"foundationalId": FID,
                                         "timeframe": "Timeframe-Medium"}
    assert meta["sourceSystemsQueried"] == ["FARMER_REGISTRY", "DISABILITY_REGISTRY"]
    assert meta["resolvedTimeframe"]["start"] == "2025-10-08"
    assert meta["resolvedTimeframe"]["end"] == "2026-10-08"
    # Only a registry that answered covered anything.
    assert [p["system"] for p in meta["resolvedTimeframe"]["perSourceSystem"]] == \
        ["FARMER_REGISTRY"]
    codes = {(w["system"], w["code"]) for w in meta["warnings"]}
    assert codes == {("UNKNOWN_REGISTRY", "REGISTRY_NOT_CONFIGURED"),
                     ("DISABILITY_REGISTRY", "unreachable"),
                     ("NATIONAL_SOCIAL_REGISTRY", "NOT_CONSENTED"),
                     ("PBMS", "SECTION_NOT_SUPPORTED"),
                     ("G2P_BRIDGE", "SECTION_NOT_SUPPORTED")}


def test_sections_and_dates(three):
    query = _query(sections=["REGISTRIES"], timeframe="Timeframe-Long",
                   asOfDate="2026-01-31")
    response = bene360.build_response(catalog=three, query=query, outcomes={},
                                      response_id="urn:x:1", generated_at=NOW)
    assert "programs" not in response and "bridgeProcessing" not in response
    resolved = response["meta"]["resolvedTimeframe"]
    assert "start" not in resolved and resolved["end"] == "2026-01-31"
    warnings = {w["code"] for w in response["meta"]["warnings"]}
    assert "AS_OF_DATE_NOT_APPLIED" in warnings
    assert "SECTION_NOT_SUPPORTED" not in warnings
    assert response["meta"]["requestParameters"]["sections"] == ["REGISTRIES"]


@pytest.mark.parametrize("query, expected", [
    ({"sections": ["PROGRAMS"]}, []),
    ({"registryFilter": ["DISABILITY_REGISTRY", "X"]}, ["DISABILITY_REGISTRY"]),
    ({}, ["FARMER_REGISTRY", "DISABILITY_REGISTRY", "NATIONAL_SOCIAL_REGISTRY"]),
])
def test_planned_registries(three, query, expected):
    assert bene360.planned_registries(three, query) == expected
