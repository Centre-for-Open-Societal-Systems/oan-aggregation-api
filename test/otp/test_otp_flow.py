"""OTP issue + validation, for both providers, without a database or a CM.

The providers only read and write ``otp_*`` attributes on the row they are
given, so a plain object stands in for the aggregation row.

    PYTHONPATH=backend/src aggregation_layer_otp_debug_enabled=true \
        python test/otp/test_otp_flow.py
"""
import asyncio
import os
import sys
import uuid
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

os.environ.setdefault("aggregation_layer_otp_debug_enabled", "true")
os.environ.setdefault("aggregation_layer_otp_max_attempts", "3")

from openg2p_aggregation_layer.services.otp_provider import (  # noqa: E402
    FaydaOtpProvider,
    InternalOtpProvider,
    OtpError,
)

FAILURES = []


def row(subject="7615076397"):
    return SimpleNamespace(
        id=str(uuid.uuid4()), correlation_id=uuid.uuid4().hex,
        subject_id_value=subject, otp_hash=None, otp_reference=None,
        otp_expires_at=None, otp_attempts=0, otp_channel=None,
        otp_destination=None, otp_provider=None, otp_debug_code=None,
        otp_verified_at=None)


def wrong(code):
    return "0" * len(code) if code != "0" * len(code) else "1" * len(code)


async def expect_error(name, coro, reason):
    try:
        await coro
    except OtpError as exc:
        ok = exc.reason == reason
        print("  %-38s %s (%s)" % (name, "PASS" if ok else "FAIL", exc.reason))
        if not ok:
            FAILURES.append(name)
        return
    print("  %-38s FAIL (no error)" % name)
    FAILURES.append(name)


def check(name, cond):
    print("  %-38s %s" % (name, "PASS" if cond else "FAIL"))
    if not cond:
        FAILURES.append(name)


async def run(provider):
    print("[%s]" % provider.name)

    r = row()
    await provider.issue(r, r.subject_id_value)
    code = r.otp_debug_code
    check("issue stores hash, not code", bool(r.otp_hash) and code not in r.otp_hash)
    await expect_error("wrong code rejected", provider.verify(r, wrong(code)), "otp_invalid")
    check("attempt counted", r.otp_attempts == 1)
    await provider.verify(r, code)
    check("right code accepted", r.otp_verified_at is not None)
    check("debug plaintext cleared once spent", r.otp_debug_code is None)
    await expect_error("same OTP cannot be reused", provider.verify(r, code), "already_verified")

    r = row()
    await provider.issue(r, r.subject_id_value)
    for _ in range(3):
        try:
            await provider.verify(r, wrong(r.otp_debug_code))
        except OtpError:
            pass
    await expect_error("locked after max attempts",
                       provider.verify(r, r.otp_debug_code), "too_many_attempts")

    r = row()
    await provider.issue(r, r.subject_id_value)
    r.otp_expires_at = datetime.now(timezone.utc) - timedelta(seconds=1)
    await expect_error("expired OTP rejected", provider.verify(r, r.otp_debug_code), "otp_expired")

    if provider.name == "fayda":
        # Code bound to one subject cannot be validated as another.
        r = row("7615076397")
        await provider.issue(r, r.subject_id_value)
        r.subject_id_value = "1111111111"
        try:
            await provider.verify(r, r.otp_debug_code)
            check("other subject cannot use the OTP", False)
        except OtpError as exc:
            check("other subject cannot use the OTP", r.otp_verified_at is None
                  and exc.reason in ("otp_subject_mismatch", "otp_invalid"))


async def main():
    await run(FaydaOtpProvider())
    await run(InternalOtpProvider())
    print("\n%s" % ("ALL PASS" if not FAILURES else "FAILED: %s" % FAILURES))
    return 1 if FAILURES else 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
