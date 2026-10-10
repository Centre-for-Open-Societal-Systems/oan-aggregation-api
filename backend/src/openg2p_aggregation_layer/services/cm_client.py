"""The Consent Manager, reached over HTTP and nothing else.

Inside consent-management the aggregator read and wrote CM tables directly:
it looked up ``Partner`` rows, read the ``ConsentArtefact`` a decision minted,
created consent requests through ``LifecycleService`` and wrote ``AuthContext``
+ ``ConsentArtefact`` rows itself to record the subject's grant. None of that is
possible from a separate service, and none of it should be: consent records are
the CM's, and a second writer is a second way for them to be wrong.

Every one of those touches is now a call below, and every call is a generic
CM API that any partner-facing service could make - the CM carries no code for
this service (docs/CM-API-CONTRACT.md). What the CM used to record for the
aggregator, the per-registry grant, is now this service's own table
(models/grant.py). Nothing else in this service imports anything about the CM.

Failure handling is deliberately plain: an unreachable CM is a 503 to whoever
called us, never a guessed answer. In particular nothing here fails open - a
missing policy is reported as missing, and the caller decides (the OTP check
keeps the factor).
"""
import base64
import json
import logging
import time
from typing import Any, Dict, List, Optional
from urllib.parse import quote

import httpx
from openg2p_fastapi_common.service import BaseService

from ..config import Settings

_config = Settings.get_config()
_logger = logging.getLogger(_config.logging_default_logger_name)


class CMError(Exception):
    """The CM refused, or could not be reached. ``status`` is what we return."""

    def __init__(self, status: int, reason: str, detail: str = ""):
        self.status, self.reason, self.detail = status, reason, detail
        super().__init__("%s %s" % (reason, detail))


class CMClient(BaseService):
    def __init__(self, name="", **kwargs):
        super().__init__(name if name else "CMClient", **kwargs)
        self._token: Optional[str] = None
        self._token_expires_at = 0.0
        # audience -> CM partner id, filled from the CM partner list on a miss.
        self._partner_ids: Dict[str, str] = {}

    # ── service-to-service auth ─────────────────────────────────────────────

    async def _bearer(self) -> Optional[str]:
        """Client-credentials token for the ``aggregation-layer`` Keycloak client.

        Cached until 30s before it expires. With neither a token URL nor a
        static token configured, calls go without one - which only works
        against a CM running with auth disabled (dev).
        """
        if _config.cm_static_token:
            return _config.cm_static_token
        if not _config.cm_token_url:
            return None
        if self._token and time.time() < self._token_expires_at - 30:
            return self._token
        try:
            async with httpx.AsyncClient(timeout=_config.cm_timeout) as client:
                response = await client.post(_config.cm_token_url, data={
                    "grant_type": "client_credentials",
                    "client_id": _config.cm_client_id,
                    "client_secret": _config.cm_client_secret,
                })
            response.raise_for_status()
            payload = response.json()
        except Exception as exc:  # noqa: BLE001
            raise CMError(503, "cm_auth_failed",
                          "could not get a service token: %s" % exc) from exc
        self._token = payload.get("access_token")
        self._token_expires_at = time.time() + int(payload.get("expires_in") or 60)
        return self._token

    async def _call(self, method: str, path: str, *, body: Any = None,
                    allow_404: bool = False) -> Optional[Any]:
        url = _config.cm_base_url.rstrip("/") + path
        headers = {}
        token = await self._bearer()
        if token:
            headers["Authorization"] = "Bearer " + token
        try:
            async with httpx.AsyncClient(timeout=_config.cm_timeout) as client:
                response = await client.request(method, url, json=body, headers=headers)
        except Exception as exc:  # noqa: BLE001
            _logger.error("CM unreachable: %s %s - %s", method, url, exc)
            raise CMError(503, "cm_unavailable", str(exc)) from exc

        if response.status_code == 404 and allow_404:
            return None
        if response.status_code >= 400:
            try:
                detail = response.json()
            except Exception:  # noqa: BLE001
                detail = response.text
            if isinstance(detail, dict):
                detail = detail.get("detail") or detail.get("error") or detail
            _logger.warning("CM %s %s -> HTTP %s: %s",
                            method, path, response.status_code, detail)
            raise CMError(response.status_code, "cm_rejected", str(detail))
        return response.json() if response.content else None

    # ── existing CM APIs ────────────────────────────────────────────────────

    async def get_policy(self, partner_id: str) -> Optional[Dict[str, Any]]:
        """The partner's active policy, or None when it has none."""
        return await self._call(
            "GET", "/consent/v1/partners/%s/policy" % quote(partner_id, safe=""),
            allow_404=True)

    async def create_consent_request(self, *, subject_id: Dict[str, str],
                                     partner_id: str, purpose: Dict[str, Any],
                                     requested_scopes: List[str]) -> Dict[str, Any]:
        """Raise the consent request the subject answers on the CM consent screen."""
        return await self._call("POST", "/consent/v1/consent-requests", body={
            "subject_id": subject_id,
            "partner_id": partner_id,
            "purpose": purpose,
            "requested_scopes": requested_scopes,
        })

    async def get_consent_request(self, request_id: str) -> Optional[Dict[str, Any]]:
        """A consent request as the CM reports it, or None when it is unknown.

        ``status`` is pending | approved | denied | expired. On the consent
        screen's OTP path ``otp_verified_at`` / ``otp_channel`` say how the
        subject authenticated. What they granted is NOT here - see
        ``AggregatorService._release`` for how that is learnt.
        """
        return await self._call(
            "GET", "/consent/v1/consent-requests/%s" % quote(request_id, safe=""),
            allow_404=True)

    async def validate(self, consent_jws: str,
                       requested_scopes: List[str]) -> Dict[str, Any]:
        """The PDP decision for a partner's consent object.

        The same /validate the registries call, so the aggregator is trusted
        exactly as far as any partner is: signature, policy ceiling, replay
        window and the subject-grant (B8) check all run in the CM. A permit
        carries ``consent_id`` (the CM record of this fetch, revoked with the
        subject's consent), ``subject_id``, ``effective_data_scopes`` and
        ``lawful_basis``; it does NOT name the partner.
        """
        return await self._call("POST", "/consent/v1/validate", body={
            "consent_jws": consent_jws,
            "request_context": {"requested_scopes": requested_scopes},
        })

    async def consent_status(self, consent_id: str) -> Optional[str]:
        """``active`` | ``revoked`` | ``expired`` for a CM consent record, or
        None when the CM does not know it. Unauthenticated partner API."""
        result = await self._call(
            "GET", "/consent/v1/consents/%s/status" % quote(consent_id, safe=""),
            allow_404=True)
        return result.get("status") if result else None

    async def partner_id_for(self, audience: Optional[str]) -> Optional[str]:
        """The CM partner id behind an audience, or None when there is none.

        Config first (``cm_partner_ids``), then the CM's partner list, cached
        for the life of the process: a binding's id never changes once made.
        """
        if not audience:
            return None
        mapped = _config.cm_partner_id_map.get(audience)
        if mapped:
            return str(mapped)
        if audience in self._partner_ids:
            return self._partner_ids[audience]
        rows = await self._call("GET", "/consent/v1/partners") or []
        if isinstance(rows, dict):
            rows = rows.get("items") or rows.get("data") or []
        for row in rows:
            if row.get("audience") and row.get("id"):
                self._partner_ids[row["audience"]] = row["id"]
        return self._partner_ids.get(audience)

    # ── local ───────────────────────────────────────────────────────────────

    @staticmethod
    def decode_claims(consent_jws: str) -> Dict[str, Any]:
        """The JWS payload, UNVERIFIED.

        Only ever read after the CM has already validated the same JWS - and
        either permitted it, or denied it for ``no_subject_consent``, the last
        check it runs. Either way the signature, partner, policy and replay
        window have all passed, so ``aud`` names the partner whose key signed
        it. Used to identify the partner and to decide who to ask, never to
        release anything.
        """
        parts = consent_jws.split(".")
        if len(parts) != 3:
            raise ValueError("consent_jws is not a compact JWS")
        padded = parts[1] + "=" * (-len(parts[1]) % 4)
        return json.loads(base64.urlsafe_b64decode(padded))
