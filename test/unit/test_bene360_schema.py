"""Conformance with the published bene-360 JSON Schemas (test/fixtures/bene360).

The request model must accept exactly what ``request.schema.json`` accepts, and
every response this service builds must validate against
``response.schema.json`` - including the degraded ones made of warnings only.
"""
from datetime import datetime, timezone

import pytest
from conftest import DISABILITY_RECORD, FARMER_RECORD, NSR_RECORD, assert_valid
from pydantic import ValidationError

from openg2p_aggregation_layer import bene360
from openg2p_aggregation_layer.bene360 import Beneficiary360Request

FID = "7615076397"
FULL = {
    "@context": bene360.CONTEXT_URL,
    "foundationalId": FID,
    "timeframe": "Timeframe-Medium",
    "asOfDate": "2026-10-08",
    "sections": ["REGISTRIES", "PROGRAMS"],
    "registryFilter": ["FARMER_REGISTRY"],
    "correlationId": "req-1",
}


@pytest.mark.parametrize("body", [
    FULL,
    {"@context": bene360.CONTEXT_URL, "foundationalId": FID, "timeframe": "Timeframe-Short"},
    {"@context": {"@vocab": "https://example.org/"}, "foundationalId": FID,
     "timeframe": "Timeframe-Long"},
])
def test_generated_request_validates(request_schema, body):
    wire = Beneficiary360Request.model_validate(body).wire()
    assert wire == body
    assert_valid(request_schema, wire)


@pytest.mark.parametrize("change", [
    {"consent_jws": "x"},                        # extra property
    {"timeframe": "Timeframe-Year"},
    {"foundationalId": ""},
    {"sections": []},
    {"sections": ["REGISTRIES", "REGISTRIES"]},
    {"sections": ["EVERYTHING"]},
    {"registryFilter": ["A", "A"]},
    {"@context": "https://example.org/other.jsonld"},
])
def test_model_rejects_what_the_schema_rejects(request_schema, change):
    body = dict(FULL, **change)
    assert list(request_schema.iter_errors(body)), "schema should reject %s" % change
    with pytest.raises(ValidationError):
        Beneficiary360Request.model_validate(body)


def test_missing_required_fields_are_rejected(request_schema):
    for key in ("@context", "foundationalId", "timeframe"):
        body = {k: v for k, v in FULL.items() if k != key}
        assert list(request_schema.iter_errors(body))
        with pytest.raises(ValidationError):
            Beneficiary360Request.model_validate(body)


def test_generated_response_validates(three, response_schema):
    query = Beneficiary360Request.model_validate(dict(
        FULL, registryFilter=["FARMER_REGISTRY", "DISABILITY_REGISTRY",
                              "NATIONAL_SOCIAL_REGISTRY", "UNKNOWN"],
        sections=list(bene360.SECTIONS))).wire()
    every = {"FARMER_REGISTRY": ["person", "land", "household"],
             "DISABILITY_REGISTRY": ["case"],
             "NATIONAL_SOCIAL_REGISTRY": ["socio", "assets", "valuations"]}
    records = {"FARMER_REGISTRY": FARMER_RECORD, "DISABILITY_REGISTRY": DISABILITY_RECORD,
               "NATIONAL_SOCIAL_REGISTRY": NSR_RECORD}
    outcomes = {code: {"status": "ok", "membership": bene360.map_registry(
        code, three.get(code), [records[code]], scopes, FID)}
        for code, scopes in every.items()}
    response = bene360.build_response(
        catalog=three, query=query, outcomes=outcomes,
        response_id="urn:openg2p:aggregation:0b7c", generated_at=datetime.now(timezone.utc))
    assert response["beneficiary"]["matchedRegistryCount"] == 3
    assert_valid(response_schema, response)


def test_degraded_response_validates(three, response_schema):
    query = {"@context": bene360.CONTEXT_URL, "foundationalId": FID,
             "timeframe": "Timeframe-Long"}
    outcomes = {"FARMER_REGISTRY": {"status": "error", "reason": "REQ-VAL-001",
                                    "detail": ""},
                "DISABILITY_REGISTRY": {"status": "no_active_grant"},
                "NATIONAL_SOCIAL_REGISTRY": {"status": "ok", "membership": None}}
    response = bene360.build_response(catalog=three, query=query, outcomes=outcomes,
                                      response_id="urn:openg2p:aggregation:1")
    assert response["registries"] == []
    # 2 registries + PROGRAMS + DISBURSEMENTS (PBMS) + BRIDGE_PROCESSING
    assert len(response["meta"]["warnings"]) == 5
    assert_valid(response_schema, response)
