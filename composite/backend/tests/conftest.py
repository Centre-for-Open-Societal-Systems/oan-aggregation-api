"""Shared fixtures: local test keys, a Partner Management stub, registry stubs.

No network: PM and the registries are httpx.MockTransport handlers.
"""

import copy
import json
import os
import shutil
import uuid
from datetime import datetime, timedelta, timezone

import httpx
import orjson
import pytest
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives.serialization import pkcs12
from cryptography.x509.oid import NameOID
from jwt import PyJWS

HERE = os.path.dirname(__file__)
USE_CASES = os.path.abspath(os.path.join(HERE, "..", "..", "use-cases"))

FR_URL = "http://fr.test/dci/registry/sync/search"
CSR_URL = "http://csr.test/dci/registry/sync/search"
PM_URL = "http://pm.test"
# What the loan-profile use case's sources name (required and optional), per registry:
# the default test consent grants all of them.
SCOPES = {
    "farmer-registry": [f"farmer-registry.{n}" for n in (
        "farmer_identifiers", "personal_details", "land", "main_crops", "household_location", "land_location")],
    "crop-sown-registry": [f"crop-sown-registry.{n}" for n in ("farmer_reference", "crop_season", "measures", "location")],
}
AUDIT_URL = "http://audit.test"

FAN = "123456789012"
FARMER_ID = "FR-0007"


def canonical(payload) -> bytes:
    return orjson.dumps(payload, option=orjson.OPT_SORT_KEYS)


class Key:
    def __init__(self, kid):
        self.kid = kid
        self.private = ec.generate_private_key(ec.SECP256R1())
        self.public_pem = self.private.public_key().public_bytes(
            serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo
        ).decode()

    def sign_detached(self, payload) -> str:
        full = PyJWS().encode(canonical(payload), self.private, algorithm="ES256", headers={"kid": self.kid})
        a, _b, c = full.split(".")
        return f"{a}..{c}"

    def sign_compact(self, claims) -> str:
        return PyJWS().encode(json.dumps(claims).encode(), self.private, algorithm="ES256", headers={"kid": self.kid})


def make_p12(path, password=b"secret"):
    key = ec.generate_private_key(ec.SECP256R1())
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "agri-composite-test")])
    now = datetime.now(timezone.utc)
    cert = (
        x509.CertificateBuilder().subject_name(name).issuer_name(name).public_key(key.public_key())
        .serial_number(x509.random_serial_number()).not_valid_before(now - timedelta(days=1))
        .not_valid_after(now + timedelta(days=30)).sign(key, hashes.SHA256())
    )
    data = pkcs12.serialize_key_and_certificates(
        b"agri-composite", key, cert, None, serialization.BestAvailableEncryption(password)
    )
    with open(path, "wb") as fh:
        fh.write(data)
    return key, cert


@pytest.fixture
def partner_key():
    return Key("bank-a-key-1")


@pytest.fixture
def composite_p12(tmp_path):
    path = tmp_path / "composite.p12"
    key, cert = make_p12(path)
    pub = key.public_key().public_bytes(serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo).decode()
    return {"path": str(path), "password": "secret", "public_pem": pub, "public_key": key.public_key()}


@pytest.fixture
def pm_keys(partner_key):
    """reference_id -> list of key dicts served by the PM stub."""
    return {"PARTNER_BANK_A": [{"kid": partner_key.kid, "algorithm": "ES256", "public_key": partner_key.public_pem}]}


@pytest.fixture
def pm_transport(pm_keys):
    def handler(request: httpx.Request):
        ref = request.url.path.rsplit("/", 1)[-1]
        if ref in pm_keys:
            return httpx.Response(200, json={"keys": pm_keys[ref]})
        return httpx.Response(404, json={"detail": "not found"})

    return httpx.MockTransport(handler)


def use_case_policy(partner_id="PARTNER_BANK_A", use_case="loan-profile@1", version=1, **extra):
    """A PM use-case policy (one per partner and use case), as PM's policy API returns it."""
    return {
        "id": str(uuid.uuid4()), "partner_id": partner_id, "data_controller": None, "use_case": use_case,
        "version": version, "status": "active", "allowed_data_scopes": [],
        "allowed_purposes": [], "allowed_subject_id_types": [], "allowed_signing_algs": ["EdDSA", "ES256"],
        "max_validity_duration": None, "fetch_type": "oneshot", "max_fetch_frequency": None, "data_life": None,
        "effective_from": "2026-10-01T00:00:00+00:00", **extra,
    }


class PolicyStub:
    """PM's internal policy API: GET /policies/{partner_id}?use_case= (or ?data_controller=) and GET /policies?use_case=."""

    def __init__(self):
        self.policies = {"PARTNER_BANK_A": [use_case_policy()]}  # PM partner ID -> active policies
        self.calls = []
        self.down = False
        self.max_age = 60

    def handles(self, request: httpx.Request) -> bool:
        return str(request.url).startswith(f"{PM_URL}/policies")

    def __call__(self, request: httpx.Request):
        self.calls.append(str(request.url))
        if self.down:
            raise httpx.ConnectError("PM down")
        headers = {"Cache-Control": f"public, max-age={self.max_age}"}
        params = request.url.params
        path = request.url.path.rstrip("/")
        if path == "/policies":
            use_case = params.get("use_case")
            found = [p for items in self.policies.values() for p in items
                     if not p["data_controller"] and p.get("use_case") == use_case]
            return httpx.Response(200, json={"policies": found}, headers=headers)
        partner = path.rsplit("/", 1)[-1]
        found = self.policies.get(partner, [])
        if "data_controller" in params:
            found = [p for p in found if p["data_controller"] == params["data_controller"]]
        if "use_case" in params:
            found = [p for p in found if not p["data_controller"] and p.get("use_case") == params["use_case"]]
        if not found:
            return httpx.Response(404, json={"error": {"code": "PM-POLICY-404", "message": "no active policy"}})
        return httpx.Response(200, json={"partner_id": partner, "policies": found}, headers=headers)


@pytest.fixture
def use_cases_dir(tmp_path):
    d = tmp_path / "use-cases"
    d.mkdir()
    shutil.copy(os.path.join(USE_CASES, "loan-profile.yaml"), d / "loan-profile.yaml")
    return d


# ── sample registry records (shapes from the FR template / CSR domain service) ──

FARMER_RECORD = {
    "farmer_personal_details": {
        "member_identifier": [
            {"@type": "Identifier", "identifier_type": "UIN", "identifier_value": FAN},
            {"@type": "Identifier", "identifier_type": "FARMER_ID", "identifier_value": FARMER_ID},
        ],
        "demographic_info": {
            "name": {"@type": "Name", "given_name": "Almaz", "second_name": "", "surname": "Bekele"},
            "sex": "female",
            "birth_date": "1985-03-02",
        },
    },
    "family_details": {"place": {"name": "Kebele 04", "geo": {"latitude": 9.03, "longitude": 38.74}}},
    "farm_details": [
        {"land_size": 1.25, "measurement": "Hectare", "land_tenure": "Owned"},
        {"land_size": 0.5, "measurement": "Hectare", "land_tenure": "Crop share"},
    ],
    "main_crops": ["CROP_BREAD_WHEAT", "CROP_TEFF"],
}

CROP_SEASONS = [
    {"@type": "spdci-extensions-agri:CropSeason", "farmer_reference": {"farmer_id": FARMER_ID},
     "crop_season": {"crop": "CROP_BREAD_WHEAT", "crop_year": 2019, "season": "SEASON_MEHER", "plot_id": "P1"},
     "measures": {"area_sown_ha": 0.75}},
    {"@type": "spdci-extensions-agri:CropSeason", "farmer_reference": {"farmer_id": FARMER_ID},
     "crop_season": {"crop": "CROP_TEFF", "crop_year": 2019, "season": "SEASON_MEHER", "plot_id": "P2"},
     "measures": {"area_sown_ha": 0.4}},
]

SEASON_SUMMARY = [
    {"@type": "spdci-extensions-agri:ActivityAggregate", "farmer_reference": {"farmer_id": FARMER_ID},
     "crop_season": {"aggregate_type": "FARMER_SEASON_SUMMARY", "crop_year": 2019, "season": "SEASON_MEHER"},
     "measures": {"area_sown_ha": 1.15, "by_crop": {"CROP_BREAD_WHEAT": {"area_sown_ha": 0.75}}}},
]


def dci_response(request_body, records=None, status="succ", reason=None, item_status="succ"):
    header = {
        "version": "1.0.0", "message_id": request_body["header"]["message_id"],
        "message_ts": request_body["header"]["message_ts"], "action": "search",
        "status": status, "sender_id": request_body["header"]["receiver_id"],
        "receiver_id": request_body["header"]["sender_id"], "meta": {},
    }
    if status == "rjct":
        header.update(status_reason_code="REQ-VAL-001", status_reason_message=reason)
        items = []
    else:
        item = {"reference_id": request_body["message"]["search_request"][0]["reference_id"],
                "timestamp": "2026-10-01T10:00:00", "status": item_status}
        if item_status == "rjct":
            item.update(status_reason_code="rjct.search_criteria.invalid", status_reason_message=reason)
        else:
            item["data"] = {"reg_records": copy.deepcopy(records or [])}
        items = [item]
    return {"signature": "sig", "header": header,
            "message": {"transaction_id": request_body["message"]["transaction_id"],
                        "correlation_id": uuid.uuid4().hex, "search_response": items}}


class RegistryStub:
    """Records every call; default answers are the sample records."""

    def __init__(self):
        self.calls = []  # (url, body)
        self.handlers = {}  # (url, reg_record_type or None) -> callable(body) -> httpx.Response

    def __call__(self, request: httpx.Request):
        body = json.loads(request.content)
        url = str(request.url)
        self.calls.append((url, body))
        crit = body["message"]["search_request"][0]["search_criteria"]
        for key in ((url, crit["reg_record_type"]), (url, None)):
            if key in self.handlers:
                return self.handlers[key](body)
        if url == FR_URL:
            q = crit["query"]["value"]["expression"]["query"]
            value = next(iter(q.values()))["$eq"]
            found = value in (FAN, FARMER_ID)
            return httpx.Response(200, json=dci_response(body, [FARMER_RECORD] if found else []))
        if url == CSR_URL:
            if crit["reg_record_type"].endswith("ActivityAggregate"):
                return httpx.Response(200, json=dci_response(body, SEASON_SUMMARY))
            return httpx.Response(200, json=dci_response(body, CROP_SEASONS))
        return httpx.Response(404)

    def calls_to(self, url):
        return [b for u, b in self.calls if u == url]


@pytest.fixture
def registries():
    return RegistryStub()
