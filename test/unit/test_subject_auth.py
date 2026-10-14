"""The subject routes accept the CM farmer token (Fayda / eSignet login on the
CM beneficiary API) next to the staff-realm token; staff routes do not.

The CM JWKS is stubbed with a local Ed25519 key, as the CM signs with one."""
import asyncio
import time
from types import SimpleNamespace

import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import ed25519
from fastapi import HTTPException

from openg2p_aggregation_layer import auth
from openg2p_aggregation_layer.config import Settings

_config = Settings.get_config()
CM_ISS = "https://cm-bene.test/consent/v1/beneficiary"
FIN = "2222444466668888"
_cm_key = ed25519.Ed25519PrivateKey.generate()


class _StubJwks:
    def __init__(self, key):
        self.key = key

    def get_signing_key_from_jwt(self, _token):
        return SimpleNamespace(key=self.key.public_key())


@pytest.fixture(autouse=True)
def farmer_issuer(monkeypatch):
    monkeypatch.setattr(_config, "auth_enabled", True)
    monkeypatch.setattr(_config, "auth_issuer", "")  # staff realm: dev decode
    monkeypatch.setattr(_config, "subject_auth_issuer", CM_ISS)
    monkeypatch.setattr(_config, "subject_auth_required", False)
    monkeypatch.setattr(auth, "_subject_jwk_client", _StubJwks(_cm_key))


def farmer_token(key=_cm_key, **override):
    now = int(time.time())
    claims = {"iss": CM_ISS, "aud": "coss-beneficiary", "sub": "pairwise-1",
              "subject_id_type": "national_id", "subject_id_value": FIN,
              "user_type": "beneficiary", "iat": now, "exp": now + 300}
    claims.update({k: v for k, v in override.items()})
    claims = {k: v for k, v in claims.items() if v is not None}
    return jwt.encode(claims, key, algorithm="EdDSA")


def staff_token(**claims):
    now = int(time.time())
    return jwt.encode({"iss": "https://kc.test/realms/staff", "sub": "u1",
                       "preferred_username": FIN, "iat": now, "exp": now + 300, **claims},
                      "k" * 32, algorithm="HS256")


def subject(token):
    async def run():
        who = await auth.current_subject_caller("Bearer " + token)
        return await auth.get_current_subject(who)
    return asyncio.run(run())


def test_farmer_token_names_the_subject():
    assert subject(farmer_token()) == {"subject_id_type": "national_id",
                                       "subject_id_value": FIN}


@pytest.mark.parametrize("override", [
    {"exp": int(time.time()) - 5},
    {"aud": "someone-else"},
    {"user_type": None},
    {"subject_id_value": None},
])
def test_bad_farmer_token_is_401(override):
    with pytest.raises(HTTPException) as exc:
        subject(farmer_token(**override))
    assert exc.value.status_code == 401


def test_farmer_token_signed_by_another_key_is_401():
    with pytest.raises(HTTPException) as exc:
        subject(farmer_token(key=ed25519.Ed25519PrivateKey.generate()))
    assert exc.value.status_code == 401


def test_staff_realm_token_still_works_unless_required(monkeypatch):
    assert subject(staff_token())["subject_id_value"] == FIN
    monkeypatch.setattr(_config, "subject_auth_required", True)
    with pytest.raises(HTTPException) as exc:
        subject(staff_token())
    assert exc.value.status_code == 401
    assert subject(farmer_token())["subject_id_value"] == FIN


def test_issuer_unset_means_old_behaviour(monkeypatch):
    monkeypatch.setattr(_config, "subject_auth_issuer", "")
    assert not auth._is_farmer_token(farmer_token())


def test_staff_routes_do_not_take_the_farmer_path():
    # current_identity (staff search, queue) has no farmer branch: the farmer
    # token goes to the staff realm check, and carries no staff role.
    who = asyncio.run(auth.current_identity("Bearer " + farmer_token()))
    assert who.roles == []
