"""Fayda OTP semantics, in-process.

A port of the OAN Gen 1 mock —
``OAN_Registries/g2p_ati_consent_mgt/utils/mock_fayda_otp_api.py`` — which
stands in for Ethiopia's Fayda ID service and is shaped after MOSIP IDA. That
mock is an HTTP server the Odoo consent module calls; this is the same rules as
a library the Consent Manager calls directly.

Why in-process rather than another service:

- **State survives.** The original keeps transactions in a module-level dict, so
  a restart loses every one of them and a verify then answers
  ``404 Unknown transactionID``. Here the transaction IS the aggregation row, so
  it is as durable as the request it belongs to.
- **Nothing to deploy, nothing to reach.** No container, no port, no network
  path to get wrong, and no second place for the demo to break.
- **The code never crosses a wire.** The original returns the OTP to nobody and
  prints it to its own stdout; here it is generated, hashed and compared without
  leaving the process.

What is kept, because it is the part that matters:

- a **random six-digit** code, not a constant
- the code is bound to ``(transactionID, individualId)`` — verifying with a
  different individual is refused even when the code is right
- the masked-mobile reply shape, so a UI can say where the code went
- Fayda's own reason strings, so behaviour is recognisable against the Gen 1
  implementation

What is deliberately NOT kept: client id/secret checks, and the ``env`` /
``domainUri`` handshake. Those authenticate the *caller* of an HTTP service.
There is no wire here, so they would be theatre.

Swapping to a real Fayda means implementing the same two calls over HTTP against
the deployment's endpoint; nothing above ``otp_provider`` changes.
"""
import hashlib
import hmac
import secrets
from typing import Optional

__all__ = [
    "FaydaError",
    "generate_otp",
    "hash_otp",
    "verify_otp",
    "mask_phone_number",
    "transaction_binding",
]

# Fayda's own messages, kept verbatim so behaviour reads the same as Gen 1.
ERR_UNKNOWN_TRANSACTION = "Unknown transactionID"
ERR_SUBJECT_MISMATCH = "individualId does not match the original request"
ERR_INVALID_OTP = "Invalid OTP"
ERR_MISSING_OTP = "Missing OTP value"

DEFAULT_MASKED_MOBILE = "09xxxxxx55"


class FaydaError(Exception):
    """A Fayda-shaped refusal. ``reason`` is the machine-readable code and
    ``message`` is the string Fayda itself would have returned."""

    def __init__(self, reason: str, message: str):
        self.reason, self.message = reason, message
        super().__init__(message)


def generate_otp(length: int = 6) -> str:
    """A random code, zero-padded.

    ``secrets`` rather than ``random``: the original uses ``random.randint``,
    which is seeded predictably and is not meant for anything a person could
    gain from guessing. The visible behaviour is identical.
    """
    length = max(4, min(10, length))
    return "".join(secrets.choice("0123456789") for _ in range(length))


def transaction_binding(transaction_id: str, individual_id: str) -> str:
    """What the OTP is bound to.

    Mirrors the original's check that a verify names the same individual the
    transaction was opened for. Folding both into the hashed material means a
    code for one farmer cannot be spent on another even if the comparison above
    it were ever skipped.
    """
    return "%s|%s" % (transaction_id or "", individual_id or "")


def hash_otp(code: str, transaction_id: str, individual_id: str, salt: str) -> str:
    """``sha256(code + binding + salt)``.

    The code itself is never stored, exactly as ``AuthContext`` keeps only
    ``id_token_hash``. The binding is inside the hash, so a stored row cannot be
    replayed against a different transaction or individual.
    """
    material = "%s|%s|%s" % (code, transaction_binding(transaction_id, individual_id), salt)
    return hashlib.sha256(material.encode("utf-8")).hexdigest()


def verify_otp(*, code: str, stored_hash: Optional[str], transaction_id: str,
               individual_id: str, expected_individual_id: Optional[str],
               salt: str) -> None:
    """Raise FaydaError unless ``code`` is the live one for this transaction.

    The checks run in the same order as the Gen 1 service, so the same request
    fails for the same stated reason.
    """
    if not code:
        raise FaydaError("otp_missing", ERR_MISSING_OTP)
    if not stored_hash:
        raise FaydaError("otp_transaction_unknown", ERR_UNKNOWN_TRANSACTION)
    if expected_individual_id is not None and individual_id != expected_individual_id:
        raise FaydaError("otp_subject_mismatch", ERR_SUBJECT_MISMATCH)

    candidate = hash_otp(code, transaction_id, individual_id, salt)
    if not hmac.compare_digest(candidate, stored_hash):
        # Constant-time: a wrong code must not be narrowed by timing.
        raise FaydaError("otp_invalid", ERR_INVALID_OTP)


def mask_phone_number(phone_number: str) -> str:
    """Fayda's masking, digit for digit: first two, last two, the rest hidden."""
    digits = "".join(ch for ch in str(phone_number or "") if ch.isdigit())
    if len(digits) < 4:
        return DEFAULT_MASKED_MOBILE
    hidden = max(len(digits) - 4, 2)
    return "%s%s%s" % (digits[:2], "x" * hidden, digits[-2:])
