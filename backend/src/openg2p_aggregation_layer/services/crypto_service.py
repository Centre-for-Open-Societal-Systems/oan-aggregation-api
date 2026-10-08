import logging
import os

from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec, ed25519, padding, rsa
from cryptography.hazmat.primitives.asymmetric.ec import ECDSA
from cryptography.hazmat.primitives.serialization import pkcs12
from openg2p_fastapi_common.service import BaseService

from ..config import Settings
from ..utils.canonical import b64url_encode

_config = Settings.get_config()
_logger = logging.getLogger(_config.logging_default_logger_name)

ALG_EDDSA = "EdDSA"
ALG_ES256 = "ES256"
ALG_RS256 = "RS256"


class CryptoService(BaseService):
    """Holds this service's own signing key and signs with it.

    Everything this service signs - the consent object and DCI envelope on
    every registry hop, and the on-search it POSTs to the partner - is signed
    here. The public half is registered in Partner Management
    (scripts/register-aggregator.py), which is where registries and partners
    verify it; nothing is verified with this key.
    """

    def __init__(self, name="", **kwargs):
        super().__init__(name, **kwargs)
        self._cert = None
        self._private_key = self._load_signing_key()
        self.kid = _config.signing_kid
        self.algorithm = self._algorithm_for_key(self._private_key)
        if _config.signing_is_demo:
            _logger.warning(
                "================================================================\n"
                "  Signing receipts with the PUBLIC BUNDLED DEMO key (kid=%s).\n"
                "  This key ships in the chart and is NOT secret — anyone can\n"
                "  forge receipts. REPLACE IT FOR PRODUCTION: set the Signing Key\n"
                "  Source to 'existing' (or 'inline') with your own .p12.\n"
                "================================================================",
                self.kid,
            )

    # ── aggregation-layer signing key ───────────────────────────────────────────────────────

    def _load_signing_key(self):
        # Preferred: a PKCS#12 (.p12) keystore holding the private key + cert.
        p12_path = _config.signing_p12_path.strip()
        if p12_path and not os.path.exists(p12_path):
            # Configured but not mounted — don't crash-loop. Fall through to the
            # PEM / ephemeral fallback with a loud error so it's obvious in logs.
            _logger.error(
                "signing_p12_path is set to %s but no file exists there "
                "(is the signing-key Secret mounted?). Falling back to PEM/ephemeral.",
                p12_path,
            )
            p12_path = ""
        if p12_path:
            with open(p12_path, "rb") as fh:
                data = fh.read()
            password = (
                _config.signing_p12_password.encode("utf-8")
                if _config.signing_p12_password
                else None
            )
            key, cert, _ = pkcs12.load_key_and_certificates(data, password)
            if key is None:
                raise ValueError("No private key found in the PKCS#12 keystore")
            self._cert = cert
            _logger.info(
                "Loaded aggregation-layer signing key from PKCS#12 keystore %s", p12_path
            )
            return key
        # Fallback: a PEM private key string.
        pem = _config.signing_private_key_pem.strip()
        if pem:
            return serialization.load_pem_private_key(pem.encode("utf-8"), password=None)
        # Dev only: an ephemeral key.
        _logger.warning(
            "No aggregation-layer signing key configured (signing_p12_path / "
            "signing_private_key_pem) — generating an EPHEMERAL Ed25519 key. "
            "Registries and partners will reject what it signs. Configure a "
            "persistent key for production."
        )
        return ed25519.Ed25519PrivateKey.generate()

    @staticmethod
    def _algorithm_for_key(key) -> str:
        if isinstance(key, ed25519.Ed25519PrivateKey):
            return ALG_EDDSA
        if isinstance(key, ec.EllipticCurvePrivateKey):
            return ALG_ES256
        if isinstance(key, rsa.RSAPrivateKey):
            return ALG_RS256
        return _config.signing_algorithm

    def sign(self, message: bytes) -> str:
        """Sign bytes with this service's private key; return base64url signature."""
        key = self._private_key
        if isinstance(key, ed25519.Ed25519PrivateKey):
            sig = key.sign(message)
        elif isinstance(key, ec.EllipticCurvePrivateKey):
            sig = key.sign(message, ECDSA(hashes.SHA256()))
        elif isinstance(key, rsa.RSAPrivateKey):
            sig = key.sign(message, padding.PKCS1v15(), hashes.SHA256())
        else:
            raise ValueError(f"Unsupported aggregation-layer signing key type: {type(key)}")
        return b64url_encode(sig)
