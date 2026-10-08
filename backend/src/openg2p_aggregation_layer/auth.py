"""Keycloak-based caller authentication and role authorization.

Mirrors the OpenG2P AWE service: bearer tokens are verified against the Keycloak
JWKS, roles are read from both ``realm_access`` and every ``resource_access.*``
client block, and ``require_role`` gates admin endpoints. Service-to-service
callers (client-credentials tokens) authenticate the same way and are detected
by the absence of an ``email`` claim.
"""
import logging
from dataclasses import dataclass, field
from typing import Dict, List, Optional

import jwt
from fastapi import Depends, Header, HTTPException, status

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
            roles=[_config.auth_admin_role], is_service_account=True, raw_claims={},
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


def require_role(role: str):
    """Dependency factory — gate an endpoint on a single Keycloak role."""

    async def _checker(
        identity: CallerIdentity = Depends(current_identity),
    ) -> CallerIdentity:
        if not _config.auth_enabled:
            return identity
        if role not in identity.roles:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN, detail=f"Role '{role}' required"
            )
        return identity

    return _checker


async def get_current_subject(
    identity: CallerIdentity = Depends(current_identity),
) -> Dict[str, str]:
    """Subject identity for the ``/my/*`` endpoints, scoped to the caller."""
    if not identity.subject_id_value:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Token does not identify a subject",
        )
    return {
        "subject_id_type": identity.subject_id_type,
        "subject_id_value": identity.subject_id_value,
    }
