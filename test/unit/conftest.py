"""Shared fixtures for the unit tests.

    pip install -e backend pytest jsonschema
    pytest test/unit

No database, Consent Manager, registry or broker is needed.
"""
import json
import pathlib
import sys

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[2]
FIXTURES = ROOT / "test" / "fixtures"
sys.path.insert(0, str(ROOT / "backend" / "src"))


@pytest.fixture(scope="session")
def fixtures() -> pathlib.Path:
    return FIXTURES


@pytest.fixture(scope="session")
def three(fixtures):
    from openg2p_aggregation_layer.registry_catalog import load_catalog

    return load_catalog(str(fixtures / "catalogs" / "three-registries.yaml"))


@pytest.fixture(scope="session")
def four(fixtures):
    from openg2p_aggregation_layer.registry_catalog import load_catalog

    return load_catalog(str(fixtures / "catalogs" / "four-registries.yaml"))


def _validator(fixtures, name):
    from jsonschema import Draft202012Validator, FormatChecker

    schema = json.loads((fixtures / "bene360" / name).read_text(encoding="utf-8"))
    Draft202012Validator.check_schema(schema)
    return Draft202012Validator(schema, format_checker=FormatChecker())


@pytest.fixture(scope="session")
def request_schema(fixtures):
    return _validator(fixtures, "request.schema.json")


@pytest.fixture(scope="session")
def response_schema(fixtures):
    return _validator(fixtures, "response.schema.json")


def assert_valid(validator, instance):
    errors = sorted(validator.iter_errors(instance), key=lambda e: list(e.path))
    assert not errors, "\n".join("%s: %s" % (list(e.path), e.message) for e in errors)


# A record as a registry's outgest template would render it for the three
# fixture registries. Empty values are deliberate: they must not leak through.
FARMER_RECORD = {
    "person": {
        "record_id": "f-0001",
        "status": "ACTIVE",
        "approved_at": "2026-07-01T10:00:00Z",
        "ids": [{"type": "UIN", "value": "7615076397"}, {"type": "X", "value": ""}],
        "name": {"given": "Mary", "family": "Bell", "prefix": "Ms"},
        "phone": [""],
        "religion": "not-allowed",
    },
    "land": [
        {"land_id": "L1", "size": 2.5, "unit": "ha", "owner_notes": "secret",
         "crops": [{"crop_id": "C1", "commodity": "MAIZE", "season": "long", "price": 9},
                   {"crop_id": "C2", "commodity": "TEFF", "season": ""}]},
        {"land_id": "L2", "size": 1, "unit": "ha", "crops": []},
    ],
    "household": {"size": 5, "income": 100},
}
DISABILITY_RECORD = {"case": {"disability_type": "MOBILITY", "severity": "MODERATE",
                              "ids": [{"value": "7615076397"}]}}
NSR_RECORD = {
    "socio": {"income_level": "LOW", "pmt_band": 2,
              "member_identifier": [{"identifier_type": "foundational_id",
                                     "identifier_value": "7615076397"}]},
    "assets": [{"asset_id": "A1", "kind": "cow", "serial": "x"},
               {"asset_id": "A2", "kind": "plough"}],
    "valuations": [{"asset_ref": "A1", "amount": 300},
                   {"asset_ref": "A2", "amount": 40},
                   {"asset_ref": "A9", "amount": 1}],
}
WORKER_RECORD = {
    "employment": {"worker_no": "W-77", "state": "EMPLOYED", "occupation": "mason",
                   "national_id": {"kind": "national_id", "number": "7615076397"},
                   "employer": {"name": "BuildCo", "tax_id": "T-1"}, "salary": 1000},
    "contracts": [{"contract_no": "K1", "start_date": "2026-01-01", "rate": 5}],
}
