"""Checks on the partner's consent before any registry is called.

The consent is a compact JWS signed by the partner (its signature is verified
separately, against the partner's PM key). Claims (contract §1)::

    {"jti", "aud", "subject_id": {"type", "value"}, "purpose": {...},
     "grants": [{"data_controller", "data_scopes"}], "validity": {...}, ...}

A legacy consent with ``data_controller`` + ``data_scopes`` is one grant. The
composite only checks what it needs to decide whom to call; CM remains the
authority (each registry validates its own grant).

A consent the exchange Consent Manager holds (``message.consent_id``, or looked
up when the partner sends no consent) is never seen here: the CM checks it and
answers its grants, and ``scope_gaps_for`` applies the use case's scope rules.
"""

import base64
import json
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Set


class ConsentError(Exception):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code
        self.message = message


def decode_claims(consent_jws: str) -> Dict[str, Any]:
    parts = (consent_jws or "").split(".")
    if len(parts) != 3 or not parts[1]:
        raise ConsentError("consent_malformed", "consent_jws must be a compact JWS (header.payload.signature)")
    try:
        padded = parts[1] + "=" * (-len(parts[1]) % 4)
        claims = json.loads(base64.urlsafe_b64decode(padded))
    except Exception:
        raise ConsentError("consent_malformed", "consent payload is not base64url JSON") from None
    if not isinstance(claims, dict):
        raise ConsentError("consent_malformed", "consent payload must be a JSON object")
    return claims


def granted_controllers(claims: Dict[str, Any]) -> Set[str]:
    grants = claims.get("grants")
    if grants is not None:
        if not isinstance(grants, list) or not grants:
            raise ConsentError("consent_malformed", "'grants' must be a non-empty list")
        out = set()
        for g in grants:
            if not isinstance(g, dict) or not g.get("data_controller"):
                raise ConsentError("consent_malformed", "each grant needs a data_controller")
            out.add(str(g["data_controller"]))
        return out
    if claims.get("data_controller"):
        return {str(claims["data_controller"])}
    raise ConsentError("consent_malformed", "consent has neither 'grants' nor 'data_controller'")


def granted_scopes(claims: Dict[str, Any]) -> Dict[str, Set[str]]:
    """{controller: scope IDs} the consent grants (a legacy consent is one grant)."""
    grants = claims.get("grants")
    if grants is None and claims.get("data_controller"):
        grants = [{"data_controller": claims["data_controller"], "data_scopes": claims.get("data_scopes")}]
    out: Dict[str, Set[str]] = {}
    for g in grants if isinstance(grants, list) else []:
        if isinstance(g, dict) and g.get("data_controller"):
            scopes = g.get("data_scopes")
            out.setdefault(str(g["data_controller"]), set()).update(
                str(x) for x in (scopes if isinstance(scopes, list) else []))
    return out


def scope_gaps(claims: Dict[str, Any], sources) -> Dict[str, List[str]]:
    """{source_id: required scopes the consent does not grant}, for sources that
    declare scopes and whose registry the consent does grant (a registry with no
    grant at all is the grant check's business)."""
    return scope_gaps_for(granted_scopes(claims), sources)


def scope_gaps_for(granted: Dict[str, Set[str]], sources) -> Dict[str, List[str]]:
    """As scope_gaps, from {controller: granted scope IDs} (e.g. a Consent Manager's answer)."""
    gaps: Dict[str, List[str]] = {}
    for src in sources:
        if src.scopes and src.controller in granted:
            missing = sorted(set(src.scopes) - granted[src.controller])
            if missing:
                gaps[src.id] = missing
    return gaps


def requested_scopes(claims: Dict[str, Any], sources) -> List[str]:
    """The scopes to ask for: each registry's declared scopes (required and optional) —
    unless one of its sources declares none (it takes the grant as it is), in which
    case all the scopes the consent grants for that registry."""
    granted = granted_scopes(claims)
    declared: Dict[str, Set[str]] = {}
    takes_grant: Set[str] = set()
    for src in sources:
        declared.setdefault(src.controller, set()).update(src.scopes, src.optional_scopes)
        if not (src.scopes or src.optional_scopes):
            takes_grant.add(src.controller)
    out: Set[str] = set()
    for controller, scopes in declared.items():
        out |= granted.get(controller, set()) if controller in takes_grant else scopes
    return sorted(out)


def declared_scopes(sources) -> Optional[List[str]]:
    """Every scope the use case's sources name (required and optional), or None when a
    source names none (it takes its registry's grant as it is, so no list can be sent)."""
    out: Set[str] = set()
    for src in sources:
        if not (src.scopes or src.optional_scopes):
            return None
        out.update(src.scopes, src.optional_scopes)
    return sorted(out)


def _parse_ts(value) -> Optional[datetime]:
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        raise ConsentError("consent_malformed", f"bad timestamp {value!r} in consent validity") from None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def check_consent(
    claims: Dict[str, Any],
    subject: Dict[str, str],
    sources,  # iterable of SourceSpec
    now: Optional[datetime] = None,
) -> Dict[str, bool]:
    """Returns {source_id: granted}. Raises ConsentError when the request must fail."""
    consent_subject = claims.get("subject_id")
    if isinstance(consent_subject, str):
        consent_subject = {"type": None, "value": consent_subject}
    if not isinstance(consent_subject, dict) or not consent_subject.get("value"):
        raise ConsentError("consent_malformed", "consent names no subject_id")
    if str(consent_subject.get("value")) != str(subject["value"]) or (
        consent_subject.get("type") is not None and str(consent_subject.get("type")) != str(subject["type"])
    ):
        raise ConsentError("consent_subject_mismatch", "the consent's subject is not the subject of this request")

    validity = claims.get("validity") or {}
    if isinstance(validity, dict):
        now = now or datetime.now(timezone.utc)
        valid_from, valid_until = _parse_ts(validity.get("valid_from")), _parse_ts(validity.get("valid_until"))
        if valid_from and now < valid_from:
            raise ConsentError("consent_not_yet_valid", "the consent is not valid yet")
        if valid_until and now > valid_until:
            raise ConsentError("consent_expired", "the consent has expired")

    controllers = granted_controllers(claims)
    granted: Dict[str, bool] = {}
    missing_mandatory = []
    for src in sources:
        ok = src.controller in controllers
        granted[src.id] = ok
        if not ok and src.requirement == "mandatory":
            missing_mandatory.append(src.controller)
    if missing_mandatory:
        raise ConsentError(
            "consent_grant_missing",
            f"the consent has no grant for {sorted(set(missing_mandatory))}, needed by this use case",
        )
    return granted
