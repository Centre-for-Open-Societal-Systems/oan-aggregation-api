"""Staff tokens open no-consent registry access, so they are verified or
refused: with auth on, an unset issuer closes the staff routes (503) instead of
decoding tokens without checking their signature."""
import time
from types import SimpleNamespace

import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import ec
from fastapi import HTTPException

from openg2p_aggregation_layer import auth
from openg2p_aggregation_layer.config import Settings

_config = Settings.get_config()
ISSUER = "http://kc.test/realms/staff"
KEY = ec.generate_private_key(ec.SECP256R1())


def unsigned_staff_token():
    claims = {"sub": "attacker", "realm_access": {"roles": ["AGGREGATION_STAFF_VIEWER"]}}
    return "Bearer " + jwt.encode(claims, None, algorithm="none")


def signed_staff_token(**over):
    now = int(time.time())
    claims = {"iss": ISSUER, "sub": "staff-1", "iat": now, "exp": now + 300,
              "realm_access": {"roles": ["AGGREGATION_STAFF_VIEWER"]}, **over}
    return "Bearer " + jwt.encode(claims, KEY, algorithm="ES256")


def identity(header):
    import asyncio
    return asyncio.run(auth.current_identity(authorization=header))


def test_unset_issuer_refuses_every_token(monkeypatch):
    monkeypatch.setattr(_config, "auth_enabled", True)
    monkeypatch.setattr(_config, "auth_issuer", "")
    assert auth.auth_config_problem()
    for header in (unsigned_staff_token(), signed_staff_token()):
        with pytest.raises(HTTPException) as exc:
            identity(header)
        assert exc.value.status_code == 503


@pytest.fixture
def staff_realm(monkeypatch):
    monkeypatch.setattr(_config, "auth_enabled", True)
    monkeypatch.setattr(_config, "auth_issuer", ISSUER)
    monkeypatch.setattr(_config, "auth_audience", "")
    monkeypatch.setattr(auth, "_jwk_client", SimpleNamespace(
        get_signing_key_from_jwt=lambda t: SimpleNamespace(key=KEY.public_key())))


def test_signed_token_from_the_issuer_is_accepted(staff_realm):
    who = identity(signed_staff_token())
    assert who.subject == "staff-1" and "AGGREGATION_STAFF_VIEWER" in who.roles


@pytest.mark.parametrize("header", ["unsigned", "other-issuer", "expired"])
def test_unverified_tokens_are_refused(staff_realm, header):
    header = {"unsigned": unsigned_staff_token(),
              "other-issuer": signed_staff_token(iss="http://kc.test/realms/partner"),
              "expired": signed_staff_token(exp=1)}[header]
    with pytest.raises(HTTPException) as exc:
        identity(header)
    assert exc.value.status_code == 401


def test_auth_off_is_the_dev_identity_with_no_roles(monkeypatch):
    monkeypatch.setattr(_config, "auth_enabled", False)
    assert auth.auth_config_problem() is None
    assert identity(None).roles == []   # the staff routes stay closed (no role)
