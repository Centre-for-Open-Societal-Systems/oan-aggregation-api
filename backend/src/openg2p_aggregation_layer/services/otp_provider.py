"""Where the OTP actually comes from — selected by config, not by code.

Two providers, one interface:

``fayda`` (default)
    Fayda OTP semantics, in-process, via ``utils/fayda_otp``. A random six-digit
    code bound to ``(transactionID, individualId)``, stored only as a salted
    hash, compared in constant time. This is a port of the OAN Gen 1 mock
    (``g2p_ati_consent_mgt/utils/mock_fayda_otp_api.py``) rather than a call to
    it — see that module for why the rules live here instead of behind HTTP.

``internal``
    The generic equivalent: a random code with no Fayda framing, logged rather
    than delivered. Kept because it is the smallest thing that exercises the
    flow, and because it makes the seam visible — two implementations, no
    caller changes.

Switching is a config change. A real Fayda deployment plugs in by implementing
``issue`` and ``verify`` against its endpoint; nothing above this file moves.

Neither provider *delivers* anything. There is no SMS gateway in this stack, so
both write the code to the service log and say plainly in that line that no
message was sent.
"""
import hashlib
import hmac
import logging
import secrets
from datetime import datetime, timedelta, timezone
from typing import Protocol

from ..config import Settings
from ..utils import fayda_otp

_config = Settings.get_config()
_logger = logging.getLogger(_config.logging_default_logger_name)


class OtpError(Exception):
    """Carries an HTTP status so the controller does not have to map reasons."""

    def __init__(self, status: int, reason: str, detail: str = ""):
        self.status, self.reason, self.detail = status, reason, detail
        super().__init__(detail or reason)


class OtpProvider(Protocol):
    name: str

    async def issue(self, request, destination: str) -> None: ...
    async def verify(self, request, code: str) -> None: ...


def _check_window(request) -> None:
    """Shared preconditions: not already spent, not expired, attempts left."""
    if request.otp_verified_at is not None:
        raise OtpError(409, "already_verified", "this OTP has already been used")

    expires = request.otp_expires_at
    if expires is not None and expires.tzinfo is None:
        expires = expires.replace(tzinfo=timezone.utc)
    if expires is None or expires < datetime.now(timezone.utc):
        raise OtpError(410, "otp_expired", "the OTP has expired; start a new request")

    if (request.otp_attempts or 0) >= _config.otp_max_attempts:
        raise OtpError(429, "too_many_attempts", "attempt limit reached; start a new request")


def _count_failure(request) -> OtpError:
    request.otp_attempts = (request.otp_attempts or 0) + 1
    left = max(0, _config.otp_max_attempts - request.otp_attempts)
    return OtpError(401, "otp_invalid", "incorrect OTP; %d attempt(s) remaining" % left)


async def _publish(request, code: str, individual_id: str, id_type: str) -> None:
    """Drop the code in the S3/MinIO bucket, when that is switched on.

    Kept out of both providers' bodies so the two cannot drift, and imported
    here rather than at module scope because services/__init__ imports this
    module while building the package namespace.
    """
    try:
        from .otp_publisher import OtpPublisher
        await OtpPublisher.get_component().publish(
            request, code, individual_id=individual_id, id_type=id_type or None)
    except Exception as exc:  # noqa: BLE001 - a convenience copy is never fatal
        _logger.warning("OTP publish skipped: %s", exc)


# ── Fayda ───────────────────────────────────────────────────────────────────

class FaydaOtpProvider:
    """Fayda's rules, applied here rather than called over HTTP.

    The Gen 1 service keeps its transactions in a module-level dict, so a
    restart loses them all and a verify then answers `Unknown transactionID`.
    Here the transaction IS the aggregation row, so it is exactly as durable as
    the request it belongs to.
    """

    name = "fayda"

    @staticmethod
    def _individual_id(subject_value: str) -> str:
        """The Fayda number this subject is known by.

        Fayda keys on the individual's Fayda/FIN number, not a Keycloak
        username, so a demo subject has to be mapped. An unmapped subject passes
        through unchanged — which is what a real deployment wants, where the
        subject already IS that number.
        """
        mapped = _config.fayda_individual_ids.get(subject_value)
        return str(mapped) if mapped else subject_value

    async def issue(self, request, destination: str) -> None:
        # The transaction IS the row, whichever row it is: an aggregation
        # carries a correlation_id, a consent request does not.
        transaction_id = getattr(request, "correlation_id", None) or request.id
        individual_id = self._individual_id(destination)
        code = fayda_otp.generate_otp(_config.otp_length)

        request.otp_hash = fayda_otp.hash_otp(
            code, transaction_id, individual_id, _config.otp_salt)
        request.otp_reference = transaction_id
        request.otp_expires_at = datetime.now(timezone.utc) + timedelta(
            seconds=_config.otp_ttl_sec)
        request.otp_attempts = 0
        request.otp_channel = _config.fayda_otp_channel
        # Store what the code was bound TO. verify() re-derives the hash from
        # this, so a verify naming a different individual cannot match.
        request.otp_destination = individual_id
        request.otp_provider = self.name
        # See models/aggregation.py: plaintext only under the debug flag.
        request.otp_debug_code = code if _config.otp_debug_enabled else None

        await _publish(request, code, individual_id, _config.fayda_identifier_type)

        masked = fayda_otp.mask_phone_number(_config.fayda_demo_phone)
        _logger.warning(
            "[OTP] %s for individualId '%s' (transaction %s, masked %s) - generated "
            "in-process with Fayda rules; NO message was delivered. Wire a real "
            "Fayda or an SMS gateway before any non-demo use.",
            code, individual_id, transaction_id, masked,
        )

    async def verify(self, request, code: str) -> None:
        _check_window(request)
        transaction_id = (request.otp_reference
                          or getattr(request, "correlation_id", None) or request.id)
        individual_id = self._individual_id(request.subject_id_value)
        try:
            fayda_otp.verify_otp(
                code=code,
                stored_hash=request.otp_hash,
                transaction_id=transaction_id,
                individual_id=individual_id,
                expected_individual_id=request.otp_destination,
                salt=_config.otp_salt,
            )
        except fayda_otp.FaydaError as exc:
            if exc.reason == "otp_transaction_unknown":
                raise OtpError(409, exc.reason, exc.message) from exc
            if exc.reason == "otp_subject_mismatch":
                # Our bug, not the subject's: the verify named a different
                # individual than the one the transaction was opened for.
                _logger.error("Aggregation %s: %s (opened for '%s', verified as '%s')",
                              request.id, exc.message, request.otp_destination,
                              individual_id)
                raise OtpError(500, exc.reason, exc.message) from exc
            if exc.reason == "otp_missing":
                raise OtpError(400, exc.reason, exc.message) from exc
            raise _count_failure(request) from exc

        request.otp_verified_at = datetime.now(timezone.utc)
        request.otp_debug_code = None   # spent


# ── internal ────────────────────────────────────────────────────────────────

class InternalOtpProvider:
    """A random code with no Fayda framing. The simplest thing that works."""

    name = "internal"

    @staticmethod
    def _hash(code: str) -> str:
        return hashlib.sha256((code + _config.otp_salt).encode("utf-8")).hexdigest()

    async def issue(self, request, destination: str) -> None:
        digits = max(4, min(10, _config.otp_length))
        code = "".join(secrets.choice("0123456789") for _ in range(digits))

        request.otp_hash = self._hash(code)
        request.otp_expires_at = datetime.now(timezone.utc) + timedelta(
            seconds=_config.otp_ttl_sec)
        request.otp_attempts = 0
        request.otp_channel = "log"
        request.otp_destination = destination
        request.otp_provider = self.name
        request.otp_debug_code = code if _config.otp_debug_enabled else None

        await _publish(request, code, destination, "")

        _logger.warning(
            "[OTP] %s for subject '%s' (request %s) - the internal provider only "
            "logs the code; no message was delivered.",
            code, destination, request.id,
        )

    async def verify(self, request, code: str) -> None:
        if not request.otp_hash:
            raise OtpError(409, "no_otp_issued", "no OTP has been issued for this request")
        _check_window(request)
        if not hmac.compare_digest(self._hash(code or ""), request.otp_hash):
            raise _count_failure(request)
        request.otp_verified_at = datetime.now(timezone.utc)
        request.otp_debug_code = None   # spent


def build_otp_provider() -> OtpProvider:
    choice = (_config.otp_provider or "fayda").strip().lower()
    if choice in ("fayda", "fayda-local", "oan-fayda"):
        _logger.info("OTP provider: Fayda (in-process, %d-digit, bound to "
                     "transaction + individualId)", _config.otp_length)
        return FaydaOtpProvider()
    if choice != "internal":
        _logger.warning("Unknown otp_provider '%s'; falling back to internal.", choice)
    _logger.info("OTP provider: internal (codes are logged, not delivered)")
    return InternalOtpProvider()
