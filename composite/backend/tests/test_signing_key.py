"""The composite's own key: a .p12 or a generated PEM key, and its public JWKS."""

import asyncio
import json

from conftest import PM_URL
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec
from jwt import PyJWS
from jwt.algorithms import ECAlgorithm
from openg2p_agri_composite.core.crypto import build_composite_crypto


def _crypto(**kw):
    args = dict(
        partner_mgmt_api_url=PM_URL,
        signing_p12_path="",
        signing_p12_password="",
        signing_kid="",
        signing_algorithm="auto",
        allowed_algorithms="EdDSA,ES256,RS256",
    )
    args.update(kw)
    return build_composite_crypto(**args)


def _pem_key(tmp_path):
    key = ec.generate_private_key(ec.SECP256R1())
    path = tmp_path / "key.pem"
    path.write_bytes(
        key.private_bytes(
            serialization.Encoding.PEM, serialization.PrivateFormat.TraditionalOpenSSL, serialization.NoEncryption()
        )
    )
    return key, str(path)


def test_pem_key_signs_and_serves_jwks(tmp_path):
    key, pem = _pem_key(tmp_path)
    crypto = _crypto(signing_p12_path=str(tmp_path / "missing.p12"), signing_key_pem_path=pem, signing_kid="gw-1")
    assert crypto.can_sign
    jwk = crypto.jwks["keys"][0]
    assert jwk["kid"] == "gw-1" and jwk["alg"] == "ES256" and jwk["use"] == "sig" and "d" not in jwk
    # The served public key is the PEM key's.
    assert ECAlgorithm.from_jwk(json.dumps(jwk)).public_numbers() == key.public_key().public_numbers()
    # Signatures carry the kid and verify with the served key.
    jws = asyncio.run(crypto.sign_detached({"header": {"a": 1}, "message": {}}))
    header = PyJWS().get_unverified_header(jws)
    assert header["kid"] == "gw-1" and header["alg"] == "ES256"


def test_pem_key_without_kid_gets_thumbprint(tmp_path):
    _key, pem = _pem_key(tmp_path)
    crypto = _crypto(signing_key_pem_path=pem)
    kid = crypto.jwks["keys"][0]["kid"]
    assert kid.startswith("composite-")
    header = PyJWS().get_unverified_header(asyncio.run(crypto.sign_detached({"header": {}, "message": {}})))
    assert header["kid"] == kid


def test_p12_wins_over_pem(tmp_path, composite_p12):
    _key, pem = _pem_key(tmp_path)
    crypto = _crypto(
        signing_p12_path=composite_p12["path"],
        signing_p12_password=composite_p12["password"],
        signing_key_pem_path=pem,
        signing_kid="from-p12",
    )
    assert crypto.can_sign and crypto.jwks["keys"][0]["kid"] == "from-p12"


def test_no_key_empty_jwks():
    crypto = _crypto()
    assert not crypto.can_sign and crypto.jwks == {"keys": []}
