import base64
import hashlib
import json
import re
from datetime import timedelta
from typing import Any


def canonical_bytes(obj: Any) -> bytes:
    """Deterministic JSON serialisation for signing/hashing.

    Stable key ordering and no insignificant whitespace, so the same logical
    document always produces the same bytes on signer and verifier.
    """
    return json.dumps(
        obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False, default=str
    ).encode("utf-8")


def sha256_hex(data: bytes) -> str:
    return "sha256:" + hashlib.sha256(data).hexdigest()


def b64url_encode(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def b64url_decode(value: str) -> bytes:
    padding = "=" * (-len(value) % 4)
    return base64.urlsafe_b64decode(value + padding)


_DURATION_RE = re.compile(
    r"^P(?:(?P<years>\d+)Y)?(?:(?P<months>\d+)M)?(?:(?P<weeks>\d+)W)?"
    r"(?:(?P<days>\d+)D)?(?:T(?:(?P<hours>\d+)H)?(?:(?P<minutes>\d+)M)?(?:(?P<seconds>\d+)S)?)?$"
)


def iso_duration_to_timedelta(duration: str) -> timedelta:
    """Parse a (subset of) ISO-8601 duration into a timedelta.

    Years are approximated as 365 days and months as 30 days — adequate for
    enforcing a coarse validity ceiling.
    """
    match = _DURATION_RE.match(duration.strip())
    if not match:
        raise ValueError(f"Invalid ISO-8601 duration: {duration!r}")
    parts = {k: int(v) for k, v in match.groupdict().items() if v}
    return timedelta(
        days=parts.get("years", 0) * 365
        + parts.get("months", 0) * 30
        + parts.get("weeks", 0) * 7
        + parts.get("days", 0),
        hours=parts.get("hours", 0),
        minutes=parts.get("minutes", 0),
        seconds=parts.get("seconds", 0),
    )
