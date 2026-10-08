"""Publishes the issued OTP as a JSON object in an S3 bucket.

This mirrors the OAN Gen 1 / A2C arrangement, where the Odoo consent module
drops each generated OTP into the ``a2c-webhook`` bucket under ``otp/`` and a
tester reads it from there. The A2C Postman collection only ever *reads* that
bucket (both helpers are ``noauth``); the writing is done server-side, which is
what this module does for the aggregator.

The payload and the key are byte-compatible with theirs::

    otp/2026-09-17T10-43-25_2f30905a.json
    {"transactionID": "...", "otp": "869609", "individualId": "56789",
     "individualIdType": "FIN", "timestamp": "2026-09-17T10:43:25.865210"}

so a collection pointed at this bucket behaves exactly as it does against
A2C's - list the prefix, take the last key, read ``otp`` and ``transactionID``.

**This is a test harness, not a delivery channel.** Anything that can read the
bucket can read a live OTP next to the individual it belongs to, which is the
whole point when there is no SMS gateway and equally the reason it must stay
off wherever real subjects exist. It is disabled by default, gated on
``otp_publish_enabled``, and the code is the same plaintext the service log
already carries under ``otp_debug_enabled``.

Failure is never fatal: if the bucket is unreachable the OTP has still been
issued and hashed, so the flow continues and only a warning is logged. Losing
the convenience copy must not lose the request.
"""
import asyncio
import json
import logging
import uuid
from datetime import datetime, timezone
from typing import Optional

from openg2p_fastapi_common.service import BaseService

from ..config import Settings

_config = Settings.get_config()
_logger = logging.getLogger(_config.logging_default_logger_name)


class OtpPublisher(BaseService):
    """Writes one object per issued OTP. No-op unless configured."""

    def __init__(self, name="", **kwargs):
        super().__init__(name if name else "OtpPublisher", **kwargs)
        self._client = None
        self._warned = False

    # ── wiring ──────────────────────────────────────────────────────────────

    @property
    def enabled(self) -> bool:
        return bool(_config.otp_publish_enabled and _config.otp_publish_bucket)

    def _get_client(self):
        """Build the S3 client once, lazily.

        Lazy because boto3 is only needed when publishing is switched on - an
        operator who leaves this off should not have to have it installed.
        """
        if self._client is not None:
            return self._client
        try:
            import boto3
            from botocore.config import Config
        except ImportError:
            self._warn_once(
                "otp_publish_enabled=true but boto3 is not installed; "
                "no OTP will be published. pip install boto3")
            return None

        kwargs = {
            "region_name": _config.otp_publish_region,
            # Fail fast: this sits in the OTP issue path, and a slow bucket
            # must not hold up the partner's ack.
            "config": Config(retries={"max_attempts": 2, "mode": "standard"},
                             connect_timeout=3, read_timeout=5),
        }
        # An endpoint_url is what points this at MinIO (or any S3-compatible
        # store) instead of AWS. Left empty, boto3 resolves AWS as usual.
        if _config.otp_publish_endpoint_url:
            kwargs["endpoint_url"] = _config.otp_publish_endpoint_url
        if _config.otp_publish_access_key:
            kwargs["aws_access_key_id"] = _config.otp_publish_access_key
            kwargs["aws_secret_access_key"] = _config.otp_publish_secret_key
        self._client = boto3.client("s3", **kwargs)
        return self._client

    def _warn_once(self, message: str) -> None:
        if not self._warned:
            _logger.warning(message)
            self._warned = True

    def ensure_bucket(self) -> None:
        """Create the bucket if it is missing. Safe to call repeatedly.

        Only meaningful against a local store; on AWS the caller will not
        usually hold CreateBucket, so a failure here is logged and ignored -
        the put below is what actually matters.
        """
        client = self._get_client()
        if client is None:
            return
        bucket = _config.otp_publish_bucket
        try:
            client.head_bucket(Bucket=bucket)
        except Exception:
            try:
                client.create_bucket(Bucket=bucket)
                _logger.info("OTP publisher: created bucket '%s'", bucket)
            except Exception as exc:  # noqa: BLE001
                _logger.warning("OTP publisher: bucket '%s' unavailable: %s", bucket, exc)

    # ── the write ───────────────────────────────────────────────────────────

    @staticmethod
    def _key(now: datetime) -> str:
        """``otp/2026-09-17T10-43-25_2f30905a.json`` - A2C's convention.

        The timestamp leads so that a lexicographic sort is a chronological
        one: their Postman helper takes the LAST key as the newest, and that
        only holds while the names sort that way.
        """
        stamp = now.strftime("%Y-%m-%dT%H-%M-%S")
        prefix = (_config.otp_publish_prefix or "otp/").strip("/")
        return "%s/%s_%s.json" % (prefix, stamp, uuid.uuid4().hex[:8])

    async def publish(self, request, code: str, individual_id: Optional[str] = None,
                      id_type: Optional[str] = None) -> Optional[str]:
        """Write the OTP object. Returns the key, or None if nothing was written."""
        if not self.enabled:
            return None
        client = self._get_client()
        if client is None:
            return None

        now = datetime.now(timezone.utc)
        payload = {
            # Upper-cased hex, as the Gen 1 service formats it.
            "transactionID": (request.otp_reference or request.correlation_id
                              or request.id or "").replace("-", "").upper(),
            "otp": code,
            "individualId": individual_id or request.otp_destination
                            or request.subject_id_value,
            "individualIdType": id_type or _config.fayda_identifier_type,
            "timestamp": now.isoformat(),
            # Ours, not A2C's - lets a reader tie the file back to the request
            # it belongs to without guessing. Extra keys are ignored by their
            # collection, which reads only otp and transactionID.
            "aggregationId": request.id,
        }
        key = self._key(now)
        body = json.dumps(payload, indent=2).encode("utf-8")

        try:
            # boto3 is synchronous; keep it off the event loop.
            await asyncio.to_thread(
                client.put_object,
                Bucket=_config.otp_publish_bucket,
                Key=key,
                Body=body,
                ContentType="application/json",
            )
        except Exception as exc:  # noqa: BLE001 - never fail the OTP over this
            _logger.warning("OTP publisher: could not write %s: %s", key, exc)
            return None

        _logger.info("OTP publisher: wrote %s to bucket '%s'",
                     key, _config.otp_publish_bucket)
        return key
