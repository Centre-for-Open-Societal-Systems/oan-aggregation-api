"""Orchestration for the cross-registry fetch, sync or async, with no state.

The partner makes ONE call - a Beneficiary-360 request for one beneficiary -
covering any number of registries in the registry catalog, and gets ONE
Beneficiary-360 response back inside a signed DCI ``on-search``:

``POST /dci/registry/sync/search`` (the default)
    The on-search is the HTTP response.
``POST /dci/registry/async/search``
    An ack (202) straight away; the same on-search is POSTed to
    ``header.sender_uri`` as soon as the registries have answered.

This service keeps nothing: no database, no queue. Everything it needs to
answer a call is in the call, and every consent decision is the Consent
Manager's, asked again on every call.

The consent comes FIRST, outside this service:

1. the partner raises a consent request in the CM; the CM sends the subject a
   one-time code;
2. the subject gives the partner the code, the partner confirms it with the
   CM, and the CM records the subject's consent for this partner;
3. the partner calls this service with a consent object it signed for that
   subject (``authorize.consent_jws``).

Where each check happens, and why:

``prepare``
    The partner's consent object is validated by the CM's
    ``/consent/v1/validate`` - the same PDP the registries call. That buys
    signature verification against Partner Management, the policy ceiling, the
    replay window and the subject-grant check (the consent from steps 1-2). No
    consent from the subject is a 403 (``no_subject_consent``); nothing is
    raised or parked here. The query's ``foundationalId`` must be the consent's
    subject: a consent for one person never fetches another.

``fan-out``
    One call per registry, in parallel, to its unchanged
    ``/dci/registry/sync/search``, searching by the foundational ID with the
    catalog's ``id_type``. Each hop carries a consent object signed with this
    service's own key, which the registry validates with the CM against this
    service's binding (lawful basis ``legitimate_interest``). A registry
    failing is that registry's outcome, never the whole request's.

``answer``
    Each registry's record is projected to the catalog's allowed fields and
    mapped onto a bene-360 response (``bene360.py``), signed, and returned
    (sync) or POSTed to the callback (async). On the async path the CM is
    asked once more, right before the callback, whether the consent is still
    active; a withdrawn consent gets a rejection instead of the data. There is
    one callback attempt and no retry: nothing is kept to retry from.
"""
import asyncio
import json
import logging
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Set

import httpx
from openg2p_fastapi_common.service import BaseService

from .. import bene360
from ..config import Settings
from ..registry_catalog import SCOPE_SEPARATOR, get_catalog
from .cm_client import CMClient, CMError
from .registry_client import RegistryClient, RegistryError

_config = Settings.get_config()
_logger = logging.getLogger(_config.logging_default_logger_name)
_audit = logging.getLogger(_config.logging_default_logger_name + ".audit")

#: Namespace for the deterministic on-search ``message_id``.
_MESSAGE_NS = uuid.UUID("6f0f5b1e-6a4e-5a0b-9d2a-0c1f8a3e7b40")

_NO_SUBJECT_CONSENT_HINT = (
    "the subject has not consented to this partner. Raise a consent request "
    "in the Consent Manager (POST /consent/v1/consent-requests), have the "
    "subject confirm it with the one-time code, then call again with a new "
    "consent object")


class AggregationError(Exception):
    def __init__(self, status: int, reason: str, detail: str = ""):
        self.status, self.reason, self.detail = status, reason, detail
        super().__init__(detail or reason)


@dataclass
class Aggregation:
    """One partner call, held in memory for as long as it is being answered."""

    correlation_id: str
    query: Dict[str, Any]
    requested_scopes: List[str]
    subject_id_type: str
    subject_id_value: str
    purpose: Dict[str, Any]
    partner_audience: Optional[str]
    lawful_basis: str
    cm_consent_id: Optional[str]
    transaction_id: Optional[str] = None
    reference_id: Optional[str] = None
    callback_url: Optional[str] = None
    mode: str = "sync"
    started: float = field(default_factory=time.monotonic)

    @property
    def id(self) -> str:
        return self.correlation_id


class AggregatorService(BaseService):
    def __init__(self, name="", **kwargs):
        super().__init__(name if name else "AggregatorService", **kwargs)
        self.cm = CMClient.get_component()
        self.registries = RegistryClient.get_component()
        # Async answers in flight. Held so the event loop does not drop them;
        # each removes itself when done. Lost on a restart: there is no store.
        self._tasks: Set[asyncio.Task] = set()

    # ── entry points ────────────────────────────────────────────────────────

    async def search_sync(self, *, consent_jws: Optional[str] = None,
                          consent_id: Optional[str] = None,
                          partner_id: Optional[str] = None, query: Dict[str, Any],
                          transaction_id: Optional[str], reference_id: Optional[str],
                          purpose: Optional[Dict[str, Any]]) -> Dict[str, Any]:
        """Validate, fetch, answer: the signed on-search envelope."""
        aggregation = await self.prepare(
            consent_jws=consent_jws, consent_id=consent_id, partner_id=partner_id,
            query=query, transaction_id=transaction_id,
            reference_id=reference_id, purpose=purpose, mode="sync")
        outcomes = await self._query_registries(aggregation)
        refusal = await self._final_consent_check(aggregation)
        if refusal is not None:
            status_code, code, text = refusal
            self._log(aggregation, outcomes, code)
            raise AggregationError(status_code, code, text)
        self._log(aggregation, outcomes, "answered")
        return self._build_envelope(aggregation, outcomes)

    async def search_async(self, *, consent_jws: Optional[str] = None,
                           consent_id: Optional[str] = None,
                           partner_id: Optional[str] = None, query: Dict[str, Any],
                           callback_url: Optional[str], transaction_id: Optional[str],
                           reference_id: Optional[str],
                           purpose: Optional[Dict[str, Any]]) -> Aggregation:
        """Validate now, answer on the callback. Returns what the ack is built from.

        Everything that can refuse the call (callback URL, query, consent) is
        checked before the ack, so a 202 means the registries are being asked.
        """
        if not callback_url:
            raise AggregationError(
                400, "no_callback",
                "header.sender_uri must carry the callback URL for on-search")
        if not callback_url.lower().startswith(("http://", "https://")):
            raise AggregationError(400, "bad_callback", "sender_uri must be an http(s) URL")
        aggregation = await self.prepare(
            consent_jws=consent_jws, consent_id=consent_id, partner_id=partner_id,
            query=query, transaction_id=transaction_id,
            reference_id=reference_id, purpose=purpose, mode="async")
        aggregation.callback_url = callback_url
        task = asyncio.get_running_loop().create_task(self._answer_on_callback(aggregation))
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)
        return aggregation

    # ── 1. prepare ──────────────────────────────────────────────────────────

    async def prepare(self, *, consent_jws: Optional[str] = None,
                      consent_id: Optional[str] = None,
                      partner_id: Optional[str] = None, query: Dict[str, Any],
                      transaction_id: Optional[str], reference_id: Optional[str],
                      purpose: Optional[Dict[str, Any]], mode: str) -> Aggregation:
        """Every check made before a registry is called.

        ``query`` is the partner's bene-360 request (already schema-checked).
        """
        catalog = get_catalog()
        if "REGISTRIES" not in bene360.requested_sections(query):
            raise AggregationError(
                422, "section_not_supported",
                "only REGISTRIES can be answered; PROGRAMS, DISBURSEMENTS and "
                "BRIDGE_PROCESSING have no source connected")
        registries = bene360.planned_registries(catalog, query)
        if not registries:
            raise AggregationError(
                422, "no_registry",
                "registryFilter names no registry in this service's catalog "
                "(GET /aggregation/v1/registries)")
        # Ask the CM only about the blocks of the registries in play. The CM
        # answers consented ∩ policy ∩ requested, so a scope the partner's
        # object carries for a registry it filtered out is never released.
        requestable = catalog.scope_ids(registries)

        try:
            if consent_id:
                decision = await self.cm.validate_stored(
                    consent_id, partner_id, requestable, purpose=purpose)
            else:
                decision = await self.cm.validate(consent_jws, requestable, purpose=purpose)
        except CMError as exc:
            raise AggregationError(exc.status, exc.reason, exc.detail) from exc
        if decision.get("decision") != "permit":
            reason = str(decision.get("reason_code") or "deny")
            detail = decision.get("detail") or "consent did not permit this request"
            if reason == "no_subject_consent":
                detail = _NO_SUBJECT_CONSENT_HINT
            raise AggregationError(403, reason, detail)

        granted = self._granted_scopes(requestable, decision.get("effective_data_scopes"))
        subject = decision.get("subject_id") or {}
        self._check_subject(query, subject.get("value"), catalog.same_identifier)
        consented_purpose = self._consented_purpose(decision, consent_jws, purpose)

        # Who the partner is. consent_id: the partner's token said so and the
        # CM permitted only because the consent was given to that partner.
        # consent_jws: the CM looked the binding up by the object's ``aud`` and
        # verified the signature against that partner's key, so after a
        # permit ``aud`` IS the partner.
        audience = partner_id if consent_id else self.cm.decode_claims(consent_jws).get("aud")
        return Aggregation(
            correlation_id=uuid.uuid4().hex,
            query=query,
            requested_scopes=granted,
            subject_id_type=subject.get("type", ""),
            subject_id_value=subject.get("value", ""),
            # What the subject consented to, never the caller's word for it:
            # it is what the registries are told the data is for.
            purpose=consented_purpose,
            partner_audience=audience,
            lawful_basis=decision.get("lawful_basis") or "consent",
            # The CM record this permit was minted as. The CM revokes it with
            # the subject's consent, so its status is how a withdrawal is seen.
            cm_consent_id=decision.get("consent_id"),
            transaction_id=transaction_id,
            reference_id=reference_id,
            mode=mode,
        )

    def _consented_purpose(self, decision: Dict[str, Any], consent_jws: Optional[str],
                           requested: Optional[Dict[str, Any]]) -> Dict[str, Any]:
        """The purpose the subject consented to, from the CM's permit (or, for
        a consent object the CM just permitted, its signed claims). A
        ``requested`` purpose must name the same code; left out, the consent's
        purpose is used (as the API promises)."""
        consented = decision.get("purpose")
        if not consented and consent_jws:
            consented = self.cm.decode_claims(consent_jws).get("purpose")
        consented = consented if isinstance(consented, dict) else {}
        wanted = (requested or {}).get("code")
        if wanted is not None:
            if not consented.get("code"):
                raise AggregationError(
                    403, "purpose_unverified",
                    "the Consent Manager did not confirm the consent's purpose")
            if consented.get("code") != wanted:
                raise AggregationError(
                    403, "purpose_mismatch",
                    "the consent is for purpose '%s', not '%s'" % (consented.get("code"), wanted))
        return dict(consented)

    @staticmethod
    def _granted_scopes(requestable: List[str], effective: Optional[List[str]]) -> List[str]:
        """The requested scope ids the CM's permit covers; never empty.

        The CM answers a consent object once: its first /validate records the
        permit, and every later /validate of the same object returns that
        record, whatever scopes are asked for. So a permit naming only scopes
        outside ``requestable`` means the object already carried a seek for
        other registries - say so, rather than claim the consent lacks the
        scopes.
        """
        permitted = set(effective or [])
        granted = [scope for scope in requestable if scope in permitted]
        if granted:
            return granted
        asked = sorted({scope.split(SCOPE_SEPARATOR, 1)[0] for scope in requestable})
        if permitted:
            used_for = sorted({scope.split(SCOPE_SEPARATOR, 1)[0] for scope in permitted})
            raise AggregationError(
                409, "consent_object_reused",
                "this consent object already carried a seek for %s; the Consent "
                "Manager answers a consent object once, so it cannot be used for %s. "
                "Sign a new consent object for this seek"
                % (", ".join(used_for), ", ".join(asked)))
        raise AggregationError(
            403, "no_scope_permitted",
            "the consent permits none of the scopes of %s" % ", ".join(asked))

    @staticmethod
    def _check_subject(query: Dict[str, Any], subject_value: Optional[str],
                       same=None) -> None:
        """The beneficiary asked about must be the one who consented.

        The registries are searched by ``foundationalId``, and on the hop the
        CM checks this service's binding, not the subject. So this is the only
        place that stops a partner holding one person's consent from fetching
        another person's record. ``same`` compares two spellings of one ID
        (``RegistryCatalog.same_identifier``); without it they must be equal.
        """
        asked = query.get("foundationalId")
        same = same or (lambda a, b: bool(a) and a == b)
        if not subject_value or not same(asked, subject_value):
            raise AggregationError(
                403, "subject_mismatch",
                "foundationalId must be the consent object's subject_id.value")

    # ── 2. fan out ──────────────────────────────────────────────────────────

    @staticmethod
    def _query_of(aggregation) -> Dict[str, Any]:
        query = dict(aggregation.query or {})
        query.setdefault("foundationalId", aggregation.subject_id_value)
        query.setdefault("timeframe", "Timeframe-Short")
        return query

    async def _query_registries(self, aggregation) -> Dict[str, Dict[str, Any]]:
        """One call per registry, in parallel, the outcome recorded per registry.

        A partner asking across three registries should not lose two because
        one is down, so a RegistryError becomes that registry's outcome. A
        registry the query covers but the consent does not is never called.
        """
        catalog = get_catalog()
        query = self._query_of(aggregation)
        granted = catalog.split_scope_ids(aggregation.requested_scopes)
        planned = bene360.planned_registries(catalog, query)
        results = await asyncio.gather(*[
            self._query_one(aggregation, registry, granted.get(registry), query["foundationalId"])
            for registry in planned])
        return dict(zip(planned, results))

    async def _query_one(self, aggregation, registry: str, scopes: Optional[List[str]],
                         foundational_id: str) -> Dict[str, Any]:
        if not scopes:
            return {"status": "not_consented"}
        entry = get_catalog().get(registry)
        try:
            records = await self.registries.search(
                registry, entry, aggregation.subject_id_type, aggregation.subject_id_value,
                entry.search_value(foundational_id), entry.hop_scopes(scopes),
                aggregation.purpose)
        except RegistryError as exc:
            _logger.warning("Aggregation %s: %s failed - %s %s",
                            aggregation.id, registry, exc.reason, exc.detail)
            return {"status": "error", "reason": exc.reason, "detail": exc.detail}
        except Exception as exc:  # noqa: BLE001 - one registry must not sink the rest
            _logger.exception("Aggregation %s: %s failed unexpectedly", aggregation.id, registry)
            return {"status": "error", "reason": "internal_error", "detail": str(exc)}

        # The registry's search is a substring match, so a record that only
        # mentions the ID (a phone number, another person's ID) can come back.
        # Only an exact identifier match is this beneficiary.
        matched = [r for r in records if entry.identifies(r, foundational_id)]
        if len(matched) < len(records):
            _logger.warning("Aggregation %s: %s returned %d record(s) not identified "
                            "by the foundational ID; discarded", aggregation.id,
                            registry, len(records) - len(matched))
        # The registry clamped to whole blocks; this is where each block is cut
        # down to the catalog's allowed fields and placed on its register or
        # table. Only granted scopes are read, so a block fetched for the
        # identifier check alone never leaves.
        return {
            "status": "ok", "records": len(matched), "scopes": scopes,
            "discarded": len(records) - len(matched),
            "membership": bene360.map_registry(
                registry, entry, matched, scopes, foundational_id)}

    # ── 3. answer on the callback (async) ───────────────────────────────────

    async def _answer_on_callback(self, aggregation: Aggregation) -> None:
        try:
            outcomes = await self._query_registries(aggregation)
            refusal = await self._final_consent_check(aggregation)
            if refusal is None:
                body = self._build_envelope(aggregation, outcomes)
                outcome = "delivered"
            else:
                _, outcome, text = refusal
                body = self._rejection(aggregation, outcome, text)
            status_code = await self._post_callback(aggregation, body)
            if not (status_code and 200 <= status_code < 300):
                outcome = "callback_failed"
            self._log(aggregation, outcomes, outcome, callback_status=status_code)
        except Exception:  # noqa: BLE001 - a background task has no caller to raise to
            _logger.exception("Aggregation %s: async answer failed", aggregation.id)

    async def _final_consent_check(self, aggregation) -> Optional[tuple]:
        """Last chance to honour a withdrawal, on both delivery paths: the data
        is fetched but not yet released. ``(http status, code, text)`` when it
        must not be released, else None. A CM that cannot answer counts as
        "no": data is never released on an unanswered check."""
        try:
            still_active = await self.consent_still_active(aggregation)
        except CMError as exc:
            _logger.warning("Aggregation %s: consent not checkable before release - %s %s",
                            aggregation.id, exc.reason, exc.detail)
            return (503, "consent_check_failed",
                    "the Consent Manager could not confirm the consent")
        if not still_active:
            return 403, "consent_withdrawn", "the consent is no longer active"
        return None

    async def consent_still_active(self, aggregation) -> bool:
        """Is the consent this answer stands on still active in the CM?

        A non-consent basis stands on no consent. A consent-basis answer with no
        CM record to check is treated as withdrawn. Raises CMError when the CM
        cannot say.
        """
        if (aggregation.lawful_basis or "consent") != "consent":
            return True
        if not aggregation.cm_consent_id:
            return False
        return (await self.cm.consent_status(aggregation.cm_consent_id)) == "active"

    async def _post_callback(self, aggregation: Aggregation,
                             body: Dict[str, Any]) -> Optional[int]:
        """One HTTP POST. None means it did not complete at all."""
        try:
            async with httpx.AsyncClient(timeout=_config.aggregator_callback_timeout) as client:
                response = await client.post(aggregation.callback_url, json=body)
            _logger.info("Aggregation %s: callback %s -> HTTP %s",
                         aggregation.id, aggregation.callback_url, response.status_code)
            return response.status_code
        except Exception as exc:  # noqa: BLE001
            _logger.warning("Aggregation %s: callback to %s failed: %s",
                            aggregation.id, aggregation.callback_url, exc)
            return None

    # ── the envelope ────────────────────────────────────────────────────────

    def _header(self, aggregation, *, status: str, completed: int) -> Dict[str, Any]:
        consent_basis = (aggregation.lawful_basis or "consent") == "consent"
        return {
            "version": "1.0.0",
            "message_id": uuid.uuid5(_MESSAGE_NS, aggregation.correlation_id).hex,
            "message_ts": datetime.now(timezone.utc).isoformat(),
            "action": "on-search",
            "status": status,
            "total_count": 1,
            "completed_count": completed,
            "sender_id": _config.aggregator_sender_id,
            "receiver_id": aggregation.partner_audience,
            "is_msg_encrypted": False,
            # This service's own facts about the release. They stay out of the
            # bene-360 response, whose schema admits no extra properties.
            "meta": {
                "consent_enforcement": "enabled" if consent_basis else "not_applicable",
                "lawful_basis": aggregation.lawful_basis,
            },
        }

    def _signed(self, header: Dict[str, Any], message: Dict[str, Any]) -> Dict[str, Any]:
        _jws, signature = self.registries._jws({"header": header, "message": message})
        return {"signature": signature, "header": header, "message": message}

    def _build_envelope(self, aggregation, outcomes: Dict[str, Dict[str, Any]]) -> Dict[str, Any]:
        """The standard DCI on-search body carrying the bene-360 response, signed."""
        now = datetime.now(timezone.utc)
        any_ok = any(o.get("status") == "ok" for o in outcomes.values())
        record = bene360.build_response(
            catalog=get_catalog(), query=self._query_of(aggregation), outcomes=outcomes,
            response_id="urn:openg2p:aggregation:%s" % aggregation.correlation_id,
            generated_at=now)

        header = self._header(aggregation, status="succ" if any_ok else "rjct",
                              completed=1 if any_ok else 0)
        if not any_ok:
            header["status_reason_code"] = "AGG-VAL-001"
            header["status_reason_message"] = (
                "no registry could be queried; see meta.warnings in the record")
        message = {
            "transaction_id": aggregation.transaction_id or aggregation.correlation_id,
            "correlation_id": aggregation.correlation_id,
            "search_response": [{
                "reference_id": aggregation.reference_id or aggregation.correlation_id,
                "timestamp": now.isoformat(),
                "status": "succ" if any_ok else "rjct",
                "data": {
                    "version": "1.0.0",
                    "reg_type": bene360.DCI_REG_TYPE,
                    "reg_record_type": bene360.DCI_REG_RECORD_TYPE,
                    # One record: the bene-360 response. It carries its own
                    # warnings, so even a rejected search explains itself.
                    "reg_records": [record],
                },
                "pagination": {"page_size": 1, "page_number": 1, "total_count": 1},
                "locale": "en",
            }],
        }
        return self._signed(header, message)

    def _rejection(self, aggregation, code: str, text: str) -> Dict[str, Any]:
        """A signed on-search that carries no data, only why."""
        header = self._header(aggregation, status="rjct", completed=0)
        header["status_reason_code"] = code
        header["status_reason_message"] = text
        message = {
            "transaction_id": aggregation.transaction_id or aggregation.correlation_id,
            "correlation_id": aggregation.correlation_id,
            "search_response": [{
                "reference_id": aggregation.reference_id or aggregation.correlation_id,
                "timestamp": datetime.now(timezone.utc).isoformat(),
                "status": "rjct",
                "status_reason_code": code,
                "status_reason_message": text,
                "locale": "en",
            }],
        }
        return self._signed(header, message)

    # ── audit ───────────────────────────────────────────────────────────────

    @staticmethod
    def _log(aggregation, outcomes: Dict[str, Dict[str, Any]], outcome: str,
             **extra: Any) -> None:
        """One JSON line per partner call: who, whom (masked), where, what came back."""
        _audit.info(json.dumps({
            "event": "aggregation",
            "correlation_id": aggregation.correlation_id,
            "transaction_id": aggregation.transaction_id,
            "mode": aggregation.mode,
            "partner": aggregation.partner_audience,
            "subject": _masked(aggregation.subject_id_value),
            "lawful_basis": aggregation.lawful_basis,
            "cm_consent_id": aggregation.cm_consent_id,
            "scopes": aggregation.requested_scopes,
            "registries": {code: {k: v for k, v in o.items() if k in ("status", "records", "reason")}
                           for code, o in outcomes.items()},
            "outcome": outcome,
            "duration_ms": int((time.monotonic() - aggregation.started) * 1000),
            **extra,
        }, default=str))


def _masked(value: Optional[str]) -> str:
    """The ID as it goes to the log: last four characters only."""
    value = value or ""
    return ("*" * max(len(value) - 4, 0)) + value[-4:]
