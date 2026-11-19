"""Signing and verification through openg2p-fastapi-common's crypto helper.

* Partner keys: the ``partner-mgmt`` backend (PartnerMgmtKeyStore — PM key API,
  per-pod cache with soft/hard/negative TTL, unknown-kid refresh, single-flight).
  Keys are looked up as ``PARTNER_<SENDER_ID>`` (upper-case, '-' → '_'), as the
  registries do.
* Own key: a PKCS#12 (.p12) file, or a PEM private key (the chart's generated
  key; turned into a temporary .p12 at start-up); detached JWS over
  ``{header, message}`` (canonical JSON, sorted keys), the DCI convention. Its
  public key is served as a JWKS (``/.well-known/jwks.json``) for Partner
  Management to read.
"""

import base64
import hashlib
import json
import logging
import os
import secrets
import tempfile
from typing import Any, Dict, Optional

from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec, ed25519, rsa
from cryptography.hazmat.primitives.serialization import pkcs12
from jwt.algorithms import ECAlgorithm, OKPAlgorithm, RSAAlgorithm
from openg2p_fastapi_common.crypto import CryptoFactory, PartnerMgmtKeyStore

_logger = logging.getLogger("agri_composite.crypto")


def partner_reference_id(sender_id: str) -> str:
    return f"PARTNER_{(sender_id or '').replace('-', '_').upper()}"


def load_p12_key(path: str, password: str):
    with open(path, "rb") as fh:
        key, _cert, _ = pkcs12.load_key_and_certificates(fh.read(), password.encode() if password else None)
    return key


def algorithm_for_key(key) -> Optional[str]:
    """JWS algorithm matching the key type (EdDSA / ES256 / RS256)."""
    if isinstance(key, ed25519.Ed25519PrivateKey):
        return "EdDSA"
    if isinstance(key, ec.EllipticCurvePrivateKey):
        return "ES256"
    if isinstance(key, rsa.RSAPrivateKey):
        return "RS256"
    return None


def algorithm_for_p12(path: str, password: str) -> Optional[str]:
    """JWS algorithm matching the .p12's key type (EdDSA / ES256 / RS256)."""
    return algorithm_for_key(load_p12_key(path, password))


def p12_from_pem(pem_path: str, out_dir: Optional[str] = None):
    """A temporary .p12 (random password) holding the PEM private key: (path, password).

    The crypto helper reads a .p12 only; the chart generates a PEM key.
    """
    with open(pem_path, "rb") as fh:
        key = serialization.load_pem_private_key(fh.read(), password=None)
    password = secrets.token_urlsafe(24)
    data = pkcs12.serialize_key_and_certificates(
        b"composite", key, None, None, serialization.BestAvailableEncryption(password.encode())
    )
    fd, path = tempfile.mkstemp(suffix=".p12", dir=out_dir)
    with os.fdopen(fd, "wb") as fh:
        fh.write(data)
    return path, password


def public_jwk(key, kid: str, algorithm: str) -> Dict[str, Any]:
    """The public half of a private key as a JWK (kid, alg, use=sig)."""
    pub = key.public_key()
    if isinstance(pub, ec.EllipticCurvePublicKey):
        jwk = json.loads(ECAlgorithm.to_jwk(pub))
    elif isinstance(pub, rsa.RSAPublicKey):
        jwk = json.loads(RSAAlgorithm.to_jwk(pub))
    elif isinstance(pub, ed25519.Ed25519PublicKey):
        jwk = json.loads(OKPAlgorithm.to_jwk(pub))
    else:
        raise ValueError("unsupported key type")
    return {**jwk, "kid": kid, "alg": algorithm, "use": "sig"}


def key_thumbprint(key) -> str:
    """A short, stable label for a key without a kid: SHA-256 of its public key (SPKI)."""
    der = key.public_key().public_bytes(
        serialization.Encoding.DER, serialization.PublicFormat.SubjectPublicKeyInfo
    )
    return "composite-" + base64.urlsafe_b64encode(hashlib.sha256(der).digest()).decode().rstrip("=")[:16]


class CompositeCrypto:
    def __init__(self, helper, can_sign: bool, jwks: Optional[Dict[str, Any]] = None):
        self.helper = helper
        self.can_sign = can_sign
        # The composite's public key(s) as a JWKS; empty when it cannot sign.
        self.jwks = jwks or {"keys": []}

    async def sign_detached(self, payload: Dict[str, Any]) -> str:
        return await self.helper.create_jwt_token(payload=payload, include_payload=False)

    async def verify_detached(self, signature: str, payload: Dict[str, Any], sender_id: str) -> bool:
        try:
            return bool(await self.helper.verify_jwt(signature, payload=payload, km_ref_id=partner_reference_id(sender_id)))
        except Exception:
            _logger.exception("Signature verification error for sender '%s'", sender_id)
            return False

    async def verify_compact(self, jws: str, sender_id: str) -> bool:
        try:
            return bool(await self.helper.verify_jwt(jws, payload=None, km_ref_id=partner_reference_id(sender_id)))
        except Exception:
            _logger.exception("Consent signature verification error for partner '%s'", sender_id)
            return False


def build_composite_crypto(
    *,
    partner_mgmt_api_url: str,
    signing_p12_path: str,
    signing_p12_password: str,
    signing_kid: str,
    signing_key_pem_path: str = "",
    signing_algorithm: str,
    allowed_algorithms: str,
    soft_ttl: int = 300,
    hard_ttl: int = 21600,
    negative_ttl: int = 30,
    refresh_cooldown: int = 10,
    fetch_timeout: float = 3.0,
    pm_transport=None,
) -> CompositeCrypto:
    store = PartnerMgmtKeyStore(
        api_url=partner_mgmt_api_url or "",
        soft_ttl=soft_ttl,
        hard_ttl=hard_ttl,
        negative_ttl=negative_ttl,
        refresh_cooldown=refresh_cooldown,
        timeout=fetch_timeout,
        transport=pm_transport,
    )
    allowed = [a.strip() for a in (allowed_algorithms or "").split(",") if a.strip()]
    alg = (signing_algorithm or "").strip()
    can_sign = False
    jwks = None
    # A .p12 (set up by hand) wins over a PEM key (generated by the chart).
    if not (signing_p12_path and os.path.isfile(signing_p12_path)) and signing_key_pem_path and os.path.isfile(
        signing_key_pem_path
    ):
        try:
            signing_p12_path, signing_p12_password = p12_from_pem(signing_key_pem_path)
        except Exception as e:
            _logger.error("Cannot load the composite signing key %s: %s", signing_key_pem_path, e)
            signing_p12_path = ""
    if signing_p12_path:
        try:
            key = load_p12_key(signing_p12_path, signing_p12_password)
            detected = algorithm_for_key(key)
            if alg in ("", "auto"):
                alg = detected or "RS256"
            elif detected and detected != alg:
                _logger.warning("signing_algorithm %s does not match the .p12 key (%s); using %s", alg, detected, detected)
                alg = detected
            # Without a kid the crypto helper uses the certificate's thumbprint; a PEM key has
            # no certificate, so use the public key's.
            if not (signing_kid or "").strip() and _p12_has_no_cert(signing_p12_path, signing_p12_password):
                signing_kid = key_thumbprint(key)
            can_sign = True
        except Exception as e:
            _logger.error("Cannot load the composite signing key %s: %s", signing_p12_path, e)
    else:
        _logger.error("No composite signing key configured (signing_p12_path / signing_key_pem_path); "
                      "queries will fail until one is set")
    if alg in ("", "auto"):
        alg = "RS256"
    if alg not in allowed:
        allowed.append(alg)
    helper = CryptoFactory.build(
        name="agri-composite-crypto",
        backend="partner-mgmt",
        partner_key_store=store,
        signing_key_path=signing_p12_path or "",
        signing_key_password=signing_p12_password or "",
        signing_key_kid=signing_kid or "",
        signing_algorithm=alg,
        allowed_algorithms=allowed,
    )
    if can_sign:
        try:
            kid = (signing_kid or "").strip() or _cert_kid(signing_p12_path, signing_p12_password)
            jwks = {"keys": [public_jwk(key, kid, alg)]}
        except Exception:
            _logger.exception("Cannot render the composite's public key as a JWK")
    return CompositeCrypto(helper, can_sign, jwks)


def _p12_cert(path: str, password: str):
    with open(path, "rb") as fh:
        _key, cert, _ = pkcs12.load_key_and_certificates(fh.read(), password.encode() if password else None)
    return cert


def _p12_has_no_cert(path: str, password: str) -> bool:
    return _p12_cert(path, password) is None


def _cert_kid(path: str, password: str) -> str:
    """The kid the crypto helper uses without one: the certificate's SHA-256 thumbprint."""
    cert = _p12_cert(path, password)
    return base64.urlsafe_b64encode(cert.fingerprint(hashes.SHA256())).decode().rstrip("=")
