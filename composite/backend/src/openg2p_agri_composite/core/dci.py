"""DCI sync search: building the composite's request to a registry, and
classifying the registry's answer into a source status.

Status per source: ok | no_record | denied | unavailable | error.
"""

import uuid
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple

OK, NO_RECORD, DENIED, UNAVAILABLE, ERROR = "ok", "no_record", "denied", "unavailable", "error"
STATUSES = (OK, NO_RECORD, DENIED, UNAVAILABLE, ERROR)
FAILED = (DENIED, UNAVAILABLE, ERROR)

# A rejected search whose reason reads like an authorisation decision is
# reported as denied (the registry's consent / subject checks), not as an error.
_DENIAL_MARKERS = (
    "consent",
    "not permitted",
    "not authori",
    "unauthori",
    "forbidden",
    "denied",
    "subject is not",
    "controller_not_granted",
    "subject_mismatch",
)


def now_ts() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def build_search(
    *,
    request_id: str,
    source_id: str,
    sender_id: str,
    receiver_id: str,
    on_behalf_of: str,
    use_case: str,
    reg_type: str,
    reg_record_type: str,
    rendered: Dict[str, Any],
    consent_jws: Optional[str],
) -> Dict[str, Any]:
    """The {header, message} of a DCI sync search (the caller adds the signature)."""
    criteria: Dict[str, Any] = {
        "version": "1.0.0",
        "reg_type": reg_type,
        "reg_record_type": reg_record_type,
        **rendered,
    }
    if consent_jws:
        # The partner's consent unchanged (passthrough) or this registry's consent
        # token (exchange mode); each registry validates it with its own CM.
        criteria["authorize"] = {"consent_jws": consent_jws}
    ts = now_ts()
    return {
        "header": {
            "version": "1.0.0",
            "message_id": str(uuid.uuid4()),
            "message_ts": ts,
            "action": "search",
            "sender_id": sender_id,
            "receiver_id": receiver_id,
            "total_count": 1,
            "is_msg_encrypted": False,
            # Logged by the registry, never trusted for access.
            "meta": {"on_behalf_of": on_behalf_of, "request_id": request_id, "use_case": use_case},
        },
        "message": {
            "transaction_id": request_id,
            "search_request": [
                {
                    "reference_id": f"{request_id}-{source_id}"[:99],
                    "timestamp": ts,
                    "search_criteria": criteria,
                    "locale": "eng",
                }
            ],
        },
    }


def _looks_denied(*texts: Optional[str]) -> bool:
    joined = " ".join(t for t in texts if t).lower()
    return any(m in joined for m in _DENIAL_MARKERS)


def classify_http(status_code: int) -> Optional[Tuple[str, str]]:
    """Status for a non-200 HTTP answer, or None when the body should be read."""
    if status_code == 200:
        return None
    if status_code in (401, 403):
        return DENIED, f"registry refused the request (HTTP {status_code})"
    if status_code in (408, 429) or status_code >= 500:
        return UNAVAILABLE, f"registry unavailable (HTTP {status_code})"
    return ERROR, f"registry rejected the request (HTTP {status_code})"


def classify_body(body: Any) -> Tuple[str, List[Dict[str, Any]], Optional[str]]:
    """(status, records, detail) from a DCI search response envelope."""
    if not isinstance(body, dict):
        return ERROR, [], "registry response is not a JSON object"
    header = body.get("header") or {}
    message = body.get("message") or {}
    if header.get("status") == "rjct":
        reason = header.get("status_reason_message") or header.get("status_reason_code") or "rejected"
        if _looks_denied(header.get("status_reason_message"), header.get("status_reason_code")):
            return DENIED, [], _short(reason)
        return ERROR, [], _short(reason)
    items = message.get("search_response") if isinstance(message, dict) else None
    if not isinstance(items, list) or not items:
        return ERROR, [], "registry returned no search response"
    records: List[Dict[str, Any]] = []
    for item in items:
        if not isinstance(item, dict):
            return ERROR, [], "malformed search response item"
        if item.get("status") == "rjct":
            reason = item.get("status_reason_message") or item.get("status_reason_code") or "rejected"
            if _looks_denied(item.get("status_reason_message"), item.get("status_reason_code")):
                return DENIED, [], _short(reason)
            return ERROR, [], _short(reason)
        if item.get("status") not in ("succ", None):
            return ERROR, [], f"unexpected search status '{item.get('status')}'"
        data = item.get("data") or {}
        reg_records = data.get("reg_records") if isinstance(data, dict) else None
        if isinstance(reg_records, list):
            records.extend(r for r in reg_records if isinstance(r, dict))
    if not records:
        return NO_RECORD, [], None
    return OK, records, None


def _short(text: str, limit: int = 300) -> str:
    text = str(text)
    return text if len(text) <= limit else text[: limit - 1] + "…"
