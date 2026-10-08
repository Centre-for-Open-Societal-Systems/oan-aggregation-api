"""Calls a registry's existing DCI search on the aggregator's behalf.

The partner makes one request; this is what turns it into one call per
registry. Crucially it uses each registry's **unchanged**
``POST /dci/registry/sync/search`` — the same endpoint the partner would have
called itself, with the same two signatures and the same consent check. No
registry is modified, and consent enforcement stays on for every internal hop.

Two signatures per call, as in the sync path:

1. a **consent JWS** — the partner-signed consent object. Here the aggregator
   signs it with the CM key, because on this hop the aggregator *is* the
   partner. Its public half must be registered in Partner Management and it
   needs a CM binding per registry, exactly as any partner does. See
   ``scripts/register-aggregator.py``.
2. a **detached envelope signature** over the canonical ``header`` + ``message``.

So the registry validates the aggregator's consent by calling CM's own
``/validate`` — the aggregator does not get to mark its own homework.
"""
import logging
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional, Tuple

import httpx
from openg2p_fastapi_common.service import BaseService

from ..config import Settings
from ..utils import b64url_encode, canonical_bytes
from .crypto_service import CryptoService

_config = Settings.get_config()
_logger = logging.getLogger(_config.logging_default_logger_name)


class RegistryError(Exception):
    """A registry could not be reached, or rejected the aggregator's request.

    Carries the DCI reason code where there is one, so a partial result can
    explain itself per registry instead of collapsing to 'something failed'.
    """

    def __init__(self, registry: str, reason: str, detail: str = ""):
        self.registry, self.reason, self.detail = registry, reason, detail
        super().__init__("%s: %s %s" % (registry, reason, detail))


class RegistryClient(BaseService):
    def __init__(self, name="", **kwargs):
        super().__init__(name if name else "RegistryClient", **kwargs)
        self.crypto = CryptoService.get_component()

    # ── signing ─────────────────────────────────────────────────────────────

    def _jws(self, payload: Dict[str, Any]) -> Tuple[str, str]:
        """Return (compact_jws, detached_signature) for one payload.

        The detached form is ``header..signature`` — the middle segment dropped,
        which is what the DCI envelope carries, because the payload travels as
        the readable body next to it.
        """
        header = {"alg": self.crypto.algorithm, "kid": self.crypto.kid, "typ": "JWT"}
        signing_input = (b64url_encode(canonical_bytes(header)) + "."
                         + b64url_encode(canonical_bytes(payload)))
        signature = self.crypto.sign(signing_input.encode("ascii"))
        parts = signing_input.split(".")
        return signing_input + "." + signature, parts[0] + ".." + signature

    def _consent_object(self, cfg: Dict[str, Any], subject_id_type: str,
                        subject_id_value: str, scopes: List[str],
                        purpose: Dict[str, Any]) -> str:
        """Mint the consent object this registry's CM binding will accept.

        ``aud`` selects the binding and ``data_controller`` must match that
        binding's registry — the check that stops one registry's consent being
        spent at another.
        """
        now = datetime.now(timezone.utc)
        # Shape must match schemas.verification.ConsentObject exactly: validity
        # is a NESTED object, not two flat fields. A mismatch here is reported
        # as malformed_object, which reads like a signing problem and is not.
        claims = {
            "@context": "https://openg2p.org/contexts/consent_object.jsonld",
            "@type": "ConsentObject",
            "jti": uuid.uuid4().hex,
            "iss": _config.aggregator_issuer,
            "aud": cfg["audience"],
            "data_controller": cfg["controller_id"],
            "subject_id": {"type": subject_id_type, "value": subject_id_value},
            "purpose": purpose or {"code": _config.aggregator_purpose_code,
                                   "text": "aggregated partner fetch"},
            "data_scopes": scopes,
            "fetch_type": "oneshot",
            "validity": {
                "valid_from": now.isoformat(),
                "valid_until": (now + timedelta(
                    seconds=_config.aggregator_consent_validity_sec)).isoformat(),
            },
            "issued_at": now.isoformat(),
        }
        jws, _detached = self._jws(claims)
        return jws

    # ── the call ────────────────────────────────────────────────────────────

    async def search(self, registry: str, cfg: Dict[str, Any], subject_id_type: str,
                     subject_id_value: str, query_value: str, scopes: List[str],
                     purpose: Dict[str, Any]) -> List[Dict[str, Any]]:
        """Fetch this registry's records, clamped to ``scopes`` by the registry.

        Returns ``reg_records``. Raises RegistryError on anything else, so a
        caller can record one registry's failure without losing the others.
        """
        consent_jws = self._consent_object(
            cfg, subject_id_type, subject_id_value, scopes, purpose)
        now = datetime.now(timezone.utc)
        reference_id = "agg-" + uuid.uuid4().hex[:8]

        header = {
            "version": "1.0.0",
            "message_id": uuid.uuid4().hex,
            "message_ts": now.isoformat(),
            "action": "search",
            "sender_id": _config.aggregator_sender_id,
            "receiver_id": cfg.get("receiver_id") or registry,
            "total_count": 1,
            "is_msg_encrypted": False,
        }
        message = {
            "transaction_id": uuid.uuid4().hex[:12],
            "search_request": [{
                "reference_id": reference_id,
                "timestamp": now.isoformat(),
                "search_criteria": {
                    "version": "1.0.0",
                    "reg_type": cfg["reg_type"],
                    "reg_record_type": cfg["reg_record_type"],
                    "query_type": "idtype-value",
                    "query": {"type": "idtype-value",
                              "value": {"id_type": cfg.get("id_type", "functional_id"),
                                        "id_value": query_value}},
                    "pagination": {"page_size": _config.aggregator_page_size,
                                   "page_number": 1},
                    "authorize": {"consent_jws": consent_jws},
                },
            }],
        }
        _jws, signature = self._jws({"header": header, "message": message})
        body = {"signature": signature, "header": header, "message": message}

        url = cfg["url"].rstrip("/") + "/dci/registry/sync/search"
        try:
            async with httpx.AsyncClient(timeout=_config.aggregator_registry_timeout) as client:
                response = await client.post(url, json=body)
        except Exception as exc:  # network / timeout
            _logger.exception("Registry '%s' unreachable at %s", registry, url)
            raise RegistryError(registry, "unreachable", str(exc)) from exc

        if response.status_code != 200:
            raise RegistryError(registry, "http_error",
                                "HTTP %s from %s" % (response.status_code, url))
        try:
            payload = response.json()
        except Exception as exc:
            raise RegistryError(registry, "bad_response", "non-JSON body") from exc

        # The registry answers 200 with a rjct status rather than an HTTP error,
        # so the DCI status is the real outcome.
        resp_header = payload.get("header") or {}
        if resp_header.get("status") == "rjct":
            raise RegistryError(
                registry,
                resp_header.get("status_reason_code") or "rjct",
                resp_header.get("status_reason_message") or "",
            )

        items = ((payload.get("message") or {}).get("search_response")) or []
        records: List[Dict[str, Any]] = []
        for item in items:
            if item.get("status") == "rjct":
                raise RegistryError(registry,
                                    item.get("status_reason_code") or "rjct",
                                    item.get("status_reason_message") or "")
            records.extend(((item.get("data") or {}).get("reg_records")) or [])
        return records
