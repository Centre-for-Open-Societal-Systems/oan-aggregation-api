"""Keycloak-based caller authentication.

Mirrors the OpenG2P AWE service: bearer tokens are verified against the Keycloak
JWKS and roles are read from both ``realm_access`` and every
``resource_access.*`` client block. Service-to-service callers
(client-credentials tokens) authenticate the same way and are detected by the
absence of an ``email`` claim. Only the staff routes take a token; the
partner routes are trusted through the consent object the CM validates.
"""
import logging
from dataclasses import dataclass, field
from typing import List, Optional

import jwt
from fastapi import Header, HTTPException, status

from .config import Settings

_config = Settings.get_config()
_logger = logging.getLogger(_config.logging_default_logger_name)

# Lazily-built, cached JWKS client (PyJWKClient caches keys internally).
_jwk_client: Optional[jwt.PyJWKClient] = None


@dataclass
class CallerIdentity:
    subject: str
    subject_id_type: Optional[str]
    subject_id_value: Optional[str]
    name: Optional[str]
    roles: List[str] = field(default_factory=list)
    is_service_account: bool = False
    raw_claims: dict = field(default_factory=dict)


def _get_jwk_client() -> jwt.PyJWKClient:
    global _jwk_client
    if _jwk_client is None:
        _jwk_client = jwt.PyJWKClient(_config.auth_jwks_url)
    return _jwk_client


def _verify_token(token: str) -> dict:
    # Dev mode — no signature verification.
    if not _config.auth_enabled or not _config.auth_issuer:
        _logger.warning(
            "auth disabled or auth_issuer unset — bearer token decoded WITHOUT "
            "signature verification (dev only)."
        )
        try:
            return jwt.decode(token, options={"verify_signature": False})
        except jwt.PyJWTError as exc:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail=f"Invalid bearer token: {exc}",
            )
    try:
        signing_key = _get_jwk_client().get_signing_key_from_jwt(token)
        return jwt.decode(
            token,
            signing_key.key,
            algorithms=_config.auth_algorithms,
            issuer=_config.auth_issuer,
            audience=_config.auth_audience or None,
            options={"verify_aud": bool(_config.auth_audience)},
        )
    except jwt.PyJWTError as exc:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail=f"Invalid or expired token: {exc}",
        )
    except Exception as exc:  # JWKS unreachable
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=f"Could not reach Keycloak JWKS: {exc}",
        )


def _extract_roles(claims: dict) -> List[str]:
    """Union of realm-scoped and every client-scoped role on the token."""
    roles: set[str] = set()
    roles.update((claims.get("realm_access") or {}).get("roles") or [])
    for entry in (claims.get("resource_access") or {}).values():
        if isinstance(entry, dict):
            roles.update(entry.get("roles") or [])
    return sorted(roles)


def _identity_from_claims(claims: dict) -> CallerIdentity:
    sub = claims.get("sub") or claims.get("preferred_username")
    subject_value = (
        claims.get("subject_id_value")
        or claims.get("preferred_username")
        or claims.get("sub")
    )
    return CallerIdentity(
        subject=sub,
        subject_id_type=claims.get("subject_id_type") or _config.subject_default_id_type,
        subject_id_value=subject_value,
        name=(claims.get("name") or None),
        roles=_extract_roles(claims),
        is_service_account="email" not in claims,
        raw_claims=claims,
    )


async def current_identity(
    authorization: Optional[str] = Header(default=None, alias="Authorization"),
) -> CallerIdentity:
    """Require a valid Keycloak bearer token; return the caller's identity."""
    if not _config.auth_enabled:
        # Dev convenience — synthesise an admin-ish identity.
        return CallerIdentity(
            subject="dev", subject_id_type=_config.subject_default_id_type,
            subject_id_value="dev", name="dev",
            roles=[], is_service_account=True, raw_claims={},
        )
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED, detail="Missing bearer token"
        )
    claims = _verify_token(authorization[len("Bearer ") :])
    if not (claims.get("sub") or claims.get("preferred_username")):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED, detail="Token missing subject"
        )
    return _identity_from_claims(claims)


# ── Partner realm (``authorize.consent_id``) ─────────────────────────────────
# A stored consent is presented by its id, so the caller must prove it is the
# partner the consent was given to: a token from the CM's partner realm whose
# ``partner_id`` claim names that partner. Verified with that realm's keys,
# never the staff realm's.

_partner_jwk_client: Optional[jwt.PyJWKClient] = None


def _get_partner_jwk_client() -> jwt.PyJWKClient:
    global _partner_jwk_client
    if _partner_jwk_client is None:
        url = _config.partner_auth_jwks_url or (
            _config.partner_auth_issuer.rstrip("/") + "/protocol/openid-connect/certs")
        _partner_jwk_client = jwt.PyJWKClient(url)
    return _partner_jwk_client


def partner_from_token(authorization: Optional[str]) -> str:
    """The ``partner_id`` of a valid partner-realm bearer token.

    Raises HTTPException: 401 without / with a bad token, 403 without the
    claim, 503 if partner tokens are not configured or the JWKS is unreachable.
    """
    if not _config.partner_auth_issuer:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="consent_id needs partner authentication, which is not configured")
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED,
                            detail="consent_id needs the partner's bearer token")
    token = authorization[len("Bearer "):]
    try:
        key = _get_partner_jwk_client().get_signing_key_from_jwt(token)
    except jwt.PyJWKClientConnectionError as exc:
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                            detail=f"Could not reach the partner realm JWKS: {exc}")
    except jwt.PyJWTError as exc:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED,
                            detail=f"Invalid bearer token: {exc}")
    try:
        claims = jwt.decode(
            token, key.key, algorithms=_config.auth_algorithms,
            issuer=_config.partner_auth_issuer,
            audience=_config.partner_auth_audience or None,
            options={"verify_aud": bool(_config.partner_auth_audience), "require": ["exp"]},
        )
    except jwt.PyJWTError as exc:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED,
                            detail=f"Invalid or expired token: {exc}")
    value = claims.get(_config.partner_auth_partner_claim)
    if isinstance(value, list):
        value = value[0] if len(value) == 1 else None
    if not isinstance(value, str) or not value.strip():
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN,
                            detail=f"Token has no '{_config.partner_auth_partner_claim}' claim")
    return value.strip()
