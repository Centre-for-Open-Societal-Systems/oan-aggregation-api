"""Serving one use-case query, end to end. Framework-free (tested directly).

Partner → composite: a signed envelope ``{signature, header, message}``.
Steps: verify the partner's signature (PM key, fail closed) → resolve the use
case → the partner's use-case grant (PM policy, fail closed; the only grant check) → validate input → rate limit →
consent checks (in exchange mode, then consent tokens from the exchange Consent Manager, sent the use
case's purpose and scopes and the policy's limits; it looks the consent up when the partner sends none)
→ run the
sources as a DAG → partial-response rule → mapping and derived values → sign
the response. Nothing is stored; logs carry the request ID, use case, partner,
statuses and timings only.
"""

import asyncio
import logging
import time
import uuid
from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from typing import Any, Callable, Dict, List, Optional, Tuple

import httpx

from . import dci
from .audit import AuditEmitter
from .consent import (
    ConsentError,
    check_consent,
    declared_scopes,
    decode_claims,
    requested_scopes,
    scope_gaps,
    scope_gaps_for,
)
from .crypto import CompositeCrypto
from .expr import evaluate, set_path
from .loader import CompiledUseCase, UseCaseRegistry
from .models import ParameterError, coerce_parameter
from .policies import PartnerPolicyStore, PolicyUnavailable, policy_limits
from .ratelimit import RateLimiter
from .templates import TemplateRenderError, render_query

_logger = logging.getLogger("agri_composite.engine")


class QueryError(Exception):
    def __init__(self, http_status: int, code: str, message: str, sources: Optional[Dict[str, Any]] = None):
        super().__init__(message)
        self.http_status = http_status
        self.code = code
        self.message = message
        self.sources = sources


@dataclass
class SourceResult:
    status: str
    records: List[Dict[str, Any]] = field(default_factory=list)
    detail: Optional[str] = None
    duration_ms: int = 0
    attempts: int = 0
    called: bool = False

    def public(self) -> Dict[str, Any]:
        out = {"status": self.status}
        if self.detail:
            out["detail"] = self.detail
        return out


@dataclass
class EngineSettings:
    composite_partner_id: str = "agri-composite"
    request_max_skew_seconds: int = 300
    default_source_timeout_ms: int = 5000
    default_overall_timeout_ms: int = 10000
    # controller id -> {"url", "partner_id", "receiver_id"}
    registries: Dict[str, Dict[str, str]] = field(default_factory=dict)
    # exchange (default): checked or looked up at the exchange Consent Manager;
    # each registry gets its own controller's consent token.
    # passthrough: the partner's consent goes to each registry unchanged.
    consent_mode: str = "exchange"
    exchange_cm_url: str = ""
    exchange_cm_timeout_seconds: float = 5.0


class CompositeEngine:
    def __init__(
        self,
        settings: EngineSettings,
        registry: UseCaseRegistry,
        crypto: CompositeCrypto,
        client_getter: Callable[[], httpx.AsyncClient],
        audit: Optional[AuditEmitter] = None,
        limiter: Optional[RateLimiter] = None,
        activity=None,
        policies: Optional[PartnerPolicyStore] = None,
    ):
        self.settings = settings
        self.registry = registry
        self.crypto = crypto
        self._client = client_getter
        self.audit = audit or AuditEmitter("", client_getter)
        self.limiter = limiter or RateLimiter()
        # The console's call log (core.activity.ActivityStore); None or disabled → not recorded.
        self.activity = activity
        # Partner use-case grants (PM policies); None → every call is refused (fail closed).
        self.policies = policies

    # ── partner-facing entry point ───────────────────────────────────────────

    async def handle_query(self, use_case_ref: str, body: Any, header_major: Optional[int] = None) -> Tuple[int, Dict[str, Any]]:
        started = time.monotonic()
        request_id = str(uuid.uuid4())
        header = body.get("header") if isinstance(body, dict) else None
        partner_id = header.get("sender_id") if isinstance(header, dict) else None
        in_reply_to = header.get("message_id") if isinstance(header, dict) else None
        compiled: Optional[CompiledUseCase] = None
        subject = None
        statuses: Dict[str, SourceResult] = {}
        found: Dict[str, Any] = {}
        try:
            compiled, subject = await self._authorise(use_case_ref, body, header_major, found)
            message = body["message"]
            parameters = self._validate_parameters(compiled, message.get("parameters"))
            await self._rate_limit(compiled, partner_id)
            granted, preset = await self._check_consent(compiled, message, subject, partner_id)
            receipts = None
            if granted is None:
                # A consent the exchange CM holds (message.consent_id, or looked up when the
                # partner sends none): the exchange CM decides what it grants.
                receipts, granted, preset = await self._exchange_stored_consent(
                    compiled, message, subject, partner_id, request_id, found)
            elif self.settings.consent_mode == "exchange":
                receipts, exchange_preset = await self._exchange_consent(
                    compiled, message, subject, granted, request_id, found)
                preset = {**exchange_preset, **preset}
            consent_used = found.get("consent_id")
            self._emit(compiled, "request", request_id, partner_id, "success",
                       context={"consent_id": consent_used} if consent_used else None)
            statuses = await self.run_sources(
                compiled, subject, parameters, message.get("consent_jws"), partner_id, request_id, granted,
                receipts=receipts, preset=preset,
            )
            self._apply_partial_rule(compiled, statuses)
            data = self.assemble(compiled, subject, parameters, statuses)
            msg: Dict[str, Any] = {
                "use_case": compiled.ref,
                "version": compiled.spec.version,
                "request_id": request_id,
                "subject": subject,
            }
            if consent_used:
                msg["consent_id"] = consent_used
            if compiled.spec.response.source_status:
                msg["sources"] = {sid: r.public() for sid, r in statuses.items()}
            msg["data"] = data
            http_status, outcome, reason = 200, "success", None
            envelope = await self._envelope(partner_id, in_reply_to, msg, status="succ")
        except QueryError as e:
            compiled = compiled or found.get("compiled")
            msg = {"use_case": compiled.ref if compiled else use_case_ref, "request_id": request_id}
            if subject:
                msg["subject"] = subject
            if found.get("consent_id"):
                msg["consent_id"] = found["consent_id"]
            if e.sources is not None:
                msg["sources"] = e.sources
            msg["error"] = {"code": e.code, "message": e.message}
            http_status = e.http_status
            outcome = "denied" if http_status in (401, 403) else "failure"
            reason = e.code
            if compiled is not None and not statuses and http_status < 500:
                self._emit(compiled, "request", request_id, partner_id, outcome, reason,
                           {"consent_id": found["consent_id"]} if found.get("consent_id") else None)
            envelope = await self._envelope(partner_id, in_reply_to, msg, status="rjct", reason=(e.code, e.message))
        duration = int((time.monotonic() - started) * 1000)
        _logger.info(
            "query request_id=%s use_case=%s partner=%s http=%s outcome=%s sources=%s duration_ms=%d",
            request_id,
            compiled.ref if compiled else use_case_ref,
            partner_id,
            http_status,
            reason or outcome,
            {sid: (r.status, r.duration_ms) for sid, r in statuses.items()},
            duration,
        )
        if compiled is not None:
            self._emit(
                compiled, "response", request_id, partner_id, outcome, reason,
                {"http_status": http_status, "duration_ms": duration,
                 "sources": {sid: r.status for sid, r in statuses.items()},
                 **({"consent_id": found["consent_id"]} if found.get("consent_id") else {})},
            )
        if self.activity is not None and found.get("authenticated"):
            # Only calls whose signature verified: an unsigned request could otherwise
            # write rows under any partner's name (and at any rate).
            self.activity.record(
                request_id=request_id, use_case=compiled.ref if compiled else use_case_ref,
                partner_id=partner_id,
                http_status=500 if envelope is None else http_status, outcome=outcome, reason=reason,
                duration_ms=duration, sources={sid: r.status for sid, r in statuses.items()},
                consent_id=found.get("consent_id"),
            )
        if envelope is None:  # could not sign
            return 500, {
                "signature": "",
                "header": self._response_header(partner_id, in_reply_to, "rjct", ("signing_unavailable", "the composite cannot sign responses")),
                "message": {"request_id": request_id, "error": {"code": "signing_unavailable", "message": "the composite cannot sign responses; check its signing key"}},
            }
        return http_status, envelope

    # ── steps ────────────────────────────────────────────────────────────────

    async def _authorise(self, use_case_ref, body, header_major, found):
        if not isinstance(body, dict):
            raise QueryError(400, "invalid_envelope", "body must be a JSON object {signature, header, message}")
        signature, header, message = body.get("signature"), body.get("header"), body.get("message")
        if not isinstance(signature, str) or not isinstance(header, dict) or not isinstance(message, dict):
            raise QueryError(400, "invalid_envelope", "body needs signature (string), header and message (objects)")
        sender = header.get("sender_id")
        if not sender or not isinstance(sender, str):
            raise QueryError(400, "invalid_envelope", "header.sender_id is required")
        receiver = header.get("receiver_id")
        if receiver and receiver != self.settings.composite_partner_id:
            raise QueryError(400, "wrong_receiver", f"header.receiver_id must be '{self.settings.composite_partner_id}'")
        self._check_freshness(header.get("message_ts"))

        try:
            compiled = self.registry.get(use_case_ref, header_major)
        except ValueError as e:
            raise QueryError(400, "invalid_use_case", str(e)) from None
        if compiled is None:
            raise QueryError(404, "unknown_use_case", f"no published use case '{use_case_ref}'")
        found["compiled"] = compiled

        # Fail closed: no key, unknown partner or a bad signature all reject.
        if not await self.crypto.verify_detached(signature, {"header": header, "message": message}, sender):
            raise QueryError(401, "signature_invalid", "the request signature does not verify against the sender's Partner Management key")
        found["authenticated"] = True

        found["policy"] = await self._check_grant(sender, compiled)

        subject = message.get("subject")
        if not isinstance(subject, dict) or not isinstance(subject.get("type"), str) or not subject.get("type") \
                or not isinstance(subject.get("value"), str) or not subject.get("value").strip():
            raise QueryError(400, "invalid_input", "message.subject needs type and value (strings)")
        if subject["type"] not in compiled.spec.input.subject.id_types:
            raise QueryError(400, "invalid_input", f"subject type must be one of {compiled.spec.input.subject.id_types}")
        return compiled, {"type": subject["type"], "value": subject["value"].strip()}

    async def _check_grant(self, sender: str, compiled: CompiledUseCase) -> Dict[str, Any]:
        """The sender's Partner Management policy for this use case (``name@major``; fail closed).

        The only grant check: the exchange Consent Manager is sent the policy's limits, never reads it."""
        if self.policies is None:
            raise QueryError(503, "partner_policy_unavailable",
                             "the Composite has no Partner Management policy source configured")
        try:
            policy = await self.policies.use_case_policy(sender, compiled.ref)
        except PolicyUnavailable:
            raise QueryError(503, "partner_policy_unavailable",
                             "Partner Management cannot be read to check the partner's use-case grant; "
                             "try again later") from None
        if policy is None:
            raise QueryError(403, "partner_not_allowed",
                             f"partner '{sender}' is not granted the use case {compiled.ref} in Partner Management")
        return policy

    def _check_freshness(self, message_ts):
        skew = self.settings.request_max_skew_seconds
        if not skew:
            return
        try:
            ts = datetime.fromisoformat(str(message_ts).replace("Z", "+00:00"))
            if ts.tzinfo is None:
                ts = ts.replace(tzinfo=timezone.utc)
        except (TypeError, ValueError):
            raise QueryError(400, "invalid_envelope", "header.message_ts must be an ISO-8601 timestamp") from None
        if abs((datetime.now(timezone.utc) - ts).total_seconds()) > skew:
            raise QueryError(400, "stale_request", f"header.message_ts is more than {skew}s from now")

    def _validate_parameters(self, compiled: CompiledUseCase, given) -> Dict[str, Any]:
        given = given if given is not None else {}
        if not isinstance(given, dict):
            raise QueryError(400, "invalid_input", "message.parameters must be an object")
        specs = compiled.spec.input.parameters
        unknown = sorted(set(given) - set(specs))
        if unknown:
            raise QueryError(400, "invalid_input", f"unknown parameters {unknown}; allowed {sorted(specs)}")
        out = {}
        for name, spec in specs.items():
            value = given.get(name)
            if value is None:
                if spec.required and spec.default is None:
                    raise QueryError(400, "invalid_input", f"parameter '{name}' is required")
                out[name] = spec.default
                continue
            try:
                out[name] = coerce_parameter(name, spec, value)
            except ParameterError as e:
                raise QueryError(400, "invalid_input", str(e)) from None
        return out

    async def _rate_limit(self, compiled: CompiledUseCase, partner_id: str):
        rate = compiled.spec.limits.rate()
        if rate and not await self.limiter.acquire(partner_id, compiled.ref, *rate):
            raise QueryError(429, "rate_limited", f"more than {compiled.spec.limits.rate_per_partner} for {compiled.ref}")

    async def _check_consent(
        self, compiled: CompiledUseCase, message, subject, partner_id
    ) -> Tuple[Dict[str, bool], Dict[str, SourceResult]]:
        """({source_id: the consent grants its registry}, {source_id: preset result}).

        A source that declares scopes needs the consent to grant every one of its
        required scopes: a mandatory one fails the request, an optional one is
        denied (not called)."""
        consent_jws = message.get("consent_jws")
        if consent_jws is not None and not isinstance(consent_jws, str):
            raise QueryError(400, "invalid_input", "message.consent_jws must be a string")
        consent_id = message.get("consent_id")
        if consent_id is not None:
            if not isinstance(consent_id, str) or not consent_id.strip():
                raise QueryError(400, "invalid_input", "message.consent_id must be a non-empty string")
            if consent_jws is not None:
                raise QueryError(400, "invalid_input", "send message.consent_jws or message.consent_id, not both")
        if not compiled.spec.consent.required:
            return {s.id: True for s in compiled.spec.sources}, {}
        if consent_id is not None:
            # A consent the subject gave and the Consent Manager holds: only the exchange CM
            # can turn it into registry consent tokens (single-install support is to do).
            if self.settings.consent_mode != "exchange":
                raise QueryError(400, "consent_id_needs_exchange",
                                 "message.consent_id needs the composite's exchange consent mode")
            return None, {}
        if not consent_jws:
            if self.settings.consent_mode == "exchange":
                # No consent sent: the exchange CM looks up the one it holds (partner, subject, purpose).
                return None, {}
            raise QueryError(403, "consent_required",
                             f"{compiled.ref} needs the subject's consent (message.consent_jws)")
        try:
            claims = decode_claims(consent_jws)
        except ConsentError as e:
            raise QueryError(403, e.code, e.message) from None
        if not await self.crypto.verify_compact(consent_jws, partner_id):
            raise QueryError(403, "consent_signature_invalid", "the consent signature does not verify against the partner's key")
        try:
            granted = check_consent(claims, subject, compiled.spec.sources)
        except ConsentError as e:
            raise QueryError(403, e.code, e.message) from None
        gaps = scope_gaps(claims, compiled.spec.sources)
        mandatory = {sid: missing for sid, missing in gaps.items() if compiled.sources[sid].requirement == "mandatory"}
        if mandatory:
            needed = sorted({s for missing in mandatory.values() for s in missing})
            raise QueryError(403, "consent_scope_missing",
                             f"the consent does not grant {needed}, needed by this use case")
        preset = {
            sid: SourceResult(dci.DENIED, detail=f"not called: the consent does not grant {missing}")
            for sid, missing in gaps.items()
        }
        return granted, preset

    async def _exchange_consent(
        self, compiled: CompiledUseCase, message, subject, granted: Dict[str, bool], request_id: str,
        found: Optional[Dict[str, Any]] = None,
    ) -> Tuple[Dict[str, str], Dict[str, SourceResult]]:
        """Exchange mode: the partner's consent → one consent token per registry.

        Calls the exchange Consent Manager ``/validate`` with the partner's
        consent and ``issue_receipts: true`` (the composite is the caller, as a
        registry is towards its own CM: ``partner_id`` is the composite's
        partner ID, a configured token presenter there). Deny → the request
        is denied. Returns ({controller: consent token JWS}, {source_id: preset
        result}) — an optional source whose controller got no token is
        unavailable; a mandatory one fails the request. ``found["consent_id"]``: the
        consent the CM decided on (when it names one).
        """
        if not compiled.spec.consent.required:
            # No consent to exchange: registries get no consent (the partner's
            # own consent is not presented to a department registry).
            return {}, {}
        base = self._exchange_base()
        # Consent tokens carry only the scopes this use case asks for (CM: granted ∩ requested), so each
        # registry returns no other field; a registry whose sources name none: what the consent grants it.
        scopes = requested_scopes(decode_claims(message["consent_jws"]), compiled.spec.sources)
        if not scopes:
            raise QueryError(403, "consent_scope_missing", "the consent grants no data scope this use case uses")
        body = {
            "consent_jws": message["consent_jws"],
            "partner_id": self.settings.composite_partner_id,
            "issue_receipts": True,
            "request_context": self._request_context(compiled, subject, found, scopes),
        }
        decision = await self._exchange_validate(base, body, request_id, found)
        return self._receipts_and_preset(compiled, decision, granted)

    @staticmethod
    def _request_context(compiled: CompiledUseCase, subject, found: Optional[Dict[str, Any]],
                         scopes: List[str]) -> Dict[str, Any]:
        """The exchange CM's request context ("caller terms"): the subject, the use case's purpose and
        the scopes asked for (the ceiling), the limits of the partner's use-case policy, and the use case
        (``name@major``) as the template label. The CM reads no use case or use-case policy."""
        if not compiled.spec.purpose:
            raise QueryError(500, "use_case_misconfigured",
                             f"{compiled.ref} names no purpose; the exchange Consent Manager needs one")
        return {
            "subject_id": subject,
            "purpose": compiled.spec.purpose,
            "requested_scopes": scopes,
            "limits": policy_limits((found or {}).get("policy") or {}),
            "template": compiled.ref,
        }

    async def _exchange_validate(
        self, base: str, body: Dict[str, Any], request_id: str, found: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        """POST the exchange CM's /validate; a permit decision, or a QueryError.

        ``found["consent_id"]``: the consent the CM decided on, when its answer names one
        (also on a deny, e.g. a revoked consent)."""
        try:
            response = await self._client().post(
                f"{base}/consent/v1/validate", json=body, timeout=self.settings.exchange_cm_timeout_seconds
            )
        except httpx.TransportError as e:
            kind = "timed out" if isinstance(e, httpx.TimeoutException) else "is unreachable"
            _logger.error("request_id=%s: exchange Consent Manager %s: %s", request_id, kind, e)
            raise QueryError(503, "consent_exchange_unavailable", f"the exchange Consent Manager {kind}") from None
        if response.status_code != 200:
            _logger.error("request_id=%s: exchange Consent Manager returned HTTP %s", request_id, response.status_code)
            if response.status_code in (408, 429) or response.status_code >= 500:
                raise QueryError(503, "consent_exchange_unavailable",
                                 f"the exchange Consent Manager is unavailable (HTTP {response.status_code})")
            raise QueryError(502, "consent_exchange_error",
                             f"the exchange Consent Manager rejected the request (HTTP {response.status_code})")
        try:
            decision = response.json()
        except ValueError:
            decision = None
        if not isinstance(decision, dict):
            raise QueryError(502, "consent_exchange_error", "the exchange Consent Manager answer is not a JSON object")
        used = decision.get("consent_id")
        if found is not None and isinstance(used, str) and used:
            found["consent_id"] = used[:200]
        if decision.get("decision") != "permit":
            reason = decision.get("reason_code") or "denied"
            detail = decision.get("detail")
            raise QueryError(403, "consent_denied",
                             f"the exchange Consent Manager denied the consent ({reason}{': ' + str(detail) if detail else ''})")
        return decision

    def _exchange_base(self) -> str:
        base = (self.settings.exchange_cm_url or "").rstrip("/")
        if not base:
            raise QueryError(503, "consent_exchange_unavailable",
                             "consent exchange is on but no exchange Consent Manager URL is configured")
        return base

    def _receipts_and_preset(self, compiled, decision, granted) -> Tuple[Dict[str, str], Dict[str, SourceResult]]:
        raw = decision.get("receipts")
        receipts = {
            str(c): r for c, r in (raw.items() if isinstance(raw, dict) else []) if isinstance(r, str) and r
        }
        preset: Dict[str, SourceResult] = {}
        missing_mandatory = []
        for src in compiled.spec.sources:
            if not granted.get(src.id, True) or src.controller in receipts:
                continue
            if src.requirement == "mandatory":
                missing_mandatory.append(src.controller)
            else:
                preset[src.id] = SourceResult(
                    dci.UNAVAILABLE,
                    detail=f"not called: the exchange Consent Manager issued no consent receipt for {src.controller}",
                )
        if missing_mandatory:
            raise QueryError(403, "consent_receipt_missing",
                             f"the exchange Consent Manager issued no consent receipt for {sorted(set(missing_mandatory))}, "
                             "needed by this use case")
        return receipts, preset

    async def _exchange_stored_consent(
        self, compiled: CompiledUseCase, message, subject, partner_id: str, request_id: str,
        found: Optional[Dict[str, Any]] = None,
    ) -> Tuple[Dict[str, str], Dict[str, bool], Dict[str, SourceResult]]:
        """A consent the exchange CM holds → consent tokens, per-source grant flags, preset results.

        ``message.consent_id`` names it; without one the CM looks it up (the consent this
        partner holds for this subject and the use case's purpose, active and valid now; none → deny
        ``consent_not_found``). The CM checks it (given by the subject, active, in its
        validity, obtained by this partner, about this subject) and answers what it grants per
        registry (``grants``) and the ``consent_id`` it used; the use case's scope rules are
        then applied to that."""
        scopes = declared_scopes(compiled.spec.sources)
        if not scopes:
            # The CM needs the scopes asked for, and a consent it holds is not seen here.
            raise QueryError(500, "use_case_misconfigured",
                             f"{compiled.ref}: a consent the exchange Consent Manager holds needs every source "
                             "to name its data scopes (scopes / optional_scopes)")
        body = {
            "consent_partner_id": partner_id,
            "partner_id": self.settings.composite_partner_id,
            "issue_receipts": True,
            "request_context": self._request_context(compiled, subject, found, scopes),
        }
        if message.get("consent_id") is not None:
            body["consent_id"] = message["consent_id"].strip()
        decision = await self._exchange_validate(self._exchange_base(), body, request_id, found)
        raw = decision.get("grants")
        grants = {
            str(c): {str(x) for x in v} for c, v in (raw.items() if isinstance(raw, dict) else []) if isinstance(v, list)
        }
        granted = {src.id: src.controller in grants for src in compiled.spec.sources}
        missing = sorted({src.controller for src in compiled.spec.sources
                          if src.requirement == "mandatory" and not granted[src.id]})
        if missing:
            raise QueryError(403, "consent_grant_missing",
                             f"the consent grants nothing for {missing}, needed by this use case")
        gaps = scope_gaps_for(grants, compiled.spec.sources)
        mandatory = {sid: m for sid, m in gaps.items() if compiled.sources[sid].requirement == "mandatory"}
        if mandatory:
            needed = sorted({x for m in mandatory.values() for x in m})
            raise QueryError(403, "consent_scope_missing", f"the consent does not grant {needed}, needed by this use case")
        receipts, preset = self._receipts_and_preset(compiled, decision, granted)
        for sid, m in gaps.items():
            preset[sid] = SourceResult(dci.DENIED, detail=f"not called: the consent does not grant {m}")
        return receipts, granted, preset

    # ── running the sources ──────────────────────────────────────────────────

    async def run_sources(
        self,
        compiled: CompiledUseCase,
        subject: Dict[str, str],
        parameters: Dict[str, Any],
        consent_jws: Optional[str],
        partner_id: str,
        request_id: str,
        granted: Optional[Dict[str, bool]] = None,
        receipts: Optional[Dict[str, str]] = None,
        preset: Optional[Dict[str, SourceResult]] = None,
    ) -> Dict[str, SourceResult]:
        """``receipts`` (exchange mode): {controller: consent token JWS} sent to
        each registry instead of ``consent_jws``; ``preset``: results decided
        before any call (e.g. an optional source with no consent token)."""
        spec = compiled.spec
        results: Dict[str, SourceResult] = {}
        for src in spec.sources:
            if granted is not None and not granted.get(src.id, True):
                results[src.id] = SourceResult(dci.DENIED, detail=f"the consent has no grant for {src.controller}")
            elif preset and src.id in preset:
                results[src.id] = preset[src.id]

        async def run_levels():
            for level in compiled.levels:
                todo = []
                for sid in level:
                    if sid in results:
                        continue
                    src = compiled.sources[sid]
                    blocked = next((d for d in src.depends_on if results[d].status != dci.OK), None)
                    if blocked:
                        results[sid] = SourceResult(
                            results[blocked].status, detail=f"not called: dependency '{blocked}' is {results[blocked].status}"
                        )
                        continue
                    todo.append(sid)
                if todo:
                    await asyncio.gather(*(
                        self._call_source(
                            compiled, sid, subject, parameters,
                            consent_jws if receipts is None else receipts.get(compiled.sources[sid].controller),
                            partner_id, request_id, results,
                        )
                        for sid in todo
                    ))

        overall = (spec.execution.overall_timeout_ms or self.settings.default_overall_timeout_ms) / 1000.0
        try:
            await asyncio.wait_for(run_levels(), timeout=overall)
        except asyncio.TimeoutError:
            pass
        for src in spec.sources:
            if src.id not in results:
                results[src.id] = SourceResult(dci.UNAVAILABLE, detail="overall timeout reached")
        return {s.id: results[s.id] for s in spec.sources}

    async def _call_source(self, compiled, sid, subject, parameters, consent_jws, partner_id, request_id, results):
        src = compiled.sources[sid]
        started = time.monotonic()
        result = SourceResult(dci.ERROR)
        try:
            result = await self._call_source_inner(compiled, src, subject, parameters, consent_jws, partner_id, request_id, results)
        except asyncio.CancelledError:
            raise
        except Exception as e:  # never let one source break the request
            _logger.exception("request_id=%s source=%s failed unexpectedly", request_id, sid)
            result = SourceResult(dci.ERROR, detail=f"internal error ({type(e).__name__})", called=True)
        result.duration_ms = int((time.monotonic() - started) * 1000)
        results[sid] = result
        if result.called:
            outcome = "success" if result.status in (dci.OK, dci.NO_RECORD) else ("denied" if result.status == dci.DENIED else "failure")
            self._emit(
                compiled, "source_call", request_id, partner_id, outcome, None if outcome == "success" else result.status,
                {"source": sid, "controller": src.controller, "status": result.status,
                 "attempts": result.attempts, "duration_ms": result.duration_ms},
            )

    async def _call_source_inner(self, compiled, src, subject, parameters, consent_jws, partner_id, request_id, results) -> SourceResult:
        endpoint = self.settings.registries.get(src.controller)
        if not endpoint or not endpoint.get("url"):
            return SourceResult(dci.ERROR, detail=f"no endpoint configured for {src.controller}")
        context = {
            "subject": subject,
            "parameters": parameters,
            "sources": {d: {"status": results[d].status, "records": results[d].records} for d in src.depends_on},
            "request_id": request_id,
            "today": date.today().isoformat(),
            "use_case": compiled.ref,
        }
        try:
            rendered = render_query(compiled.templates[src.id], context)
        except TemplateRenderError as e:
            return SourceResult(dci.ERROR, detail=f"query template: {e}")
        expression = ((rendered.get("query") or {}).get("value") or {}).get("expression") or {}
        named = expression.get("query") if isinstance(expression, dict) else None
        if isinstance(named, dict) and "subject_id" in named and named["subject_id"] in (None, ""):
            # e.g. the farmer record carries no FARMER_ID to query the crop registry by:
            # never send a query without its subject (a registry could read it as "anyone").
            return SourceResult(dci.ERROR, detail="not called: the query has no subject_id (the record it is "
                                                  "read from does not carry it)")
        envelope = dci.build_search(
            request_id=request_id,
            source_id=src.id,
            sender_id=self.settings.composite_partner_id,
            receiver_id=endpoint.get("receiver_id") or src.controller,
            on_behalf_of=partner_id,
            use_case=compiled.ref,
            reg_type=src.dci.reg_type,
            reg_record_type=src.dci.reg_record_type,
            rendered=rendered,
            consent_jws=consent_jws,
        )
        try:
            envelope = {"signature": await self.crypto.sign_detached(envelope), **envelope}
        except Exception as e:
            _logger.error("request_id=%s: cannot sign the request to %s: %s", request_id, src.controller, e)
            return SourceResult(dci.ERROR, detail="the composite could not sign the registry request")

        timeout = (src.timeout_ms or self.settings.default_source_timeout_ms) / 1000.0
        attempts, response, last_error = 0, None, None
        while attempts <= src.retries:
            attempts += 1
            try:
                response = await self._client().post(endpoint["url"], json=envelope, timeout=timeout)
            except httpx.TransportError as e:  # connect/read errors and timeouts
                response, last_error = None, e
            else:
                if response.status_code < 500:
                    break
            if attempts <= src.retries:
                await asyncio.sleep(min(0.1 * attempts, 0.5))

        if response is None:
            kind = "timed out" if isinstance(last_error, httpx.TimeoutException) else "unreachable"
            return SourceResult(dci.UNAVAILABLE, detail=f"registry {kind}", attempts=attempts, called=True)
        http_status = dci.classify_http(response.status_code)
        if http_status:
            return SourceResult(http_status[0], detail=http_status[1], attempts=attempts, called=True)
        try:
            body = response.json()
        except ValueError:
            return SourceResult(dci.ERROR, detail="registry response is not JSON", attempts=attempts, called=True)

        registry_partner = endpoint.get("partner_id")
        if registry_partner:
            sig = body.get("signature") if isinstance(body, dict) else None
            ok = isinstance(sig, str) and await self.crypto.verify_detached(
                sig, {"header": body.get("header"), "message": body.get("message")}, registry_partner
            )
            if not ok:
                return SourceResult(dci.ERROR, detail="registry response signature does not verify", attempts=attempts, called=True)

        status, records, detail = dci.classify_body(body)
        page_size = (rendered.get("pagination") or {}).get("page_size")
        # A full page from a list query (not a one-record lookup) may not be everything.
        if status == dci.OK and isinstance(page_size, int) and page_size > 1 and len(records) >= page_size and not detail:
            detail = f"the first {page_size} records only; the registry may hold more"
        return SourceResult(status, records=records, detail=detail, attempts=attempts, called=True)

    # ── assembly ─────────────────────────────────────────────────────────────

    @staticmethod
    def _apply_partial_rule(compiled: CompiledUseCase, statuses: Dict[str, SourceResult]):
        strict = compiled.spec.execution.partial_response == "denied"
        failed = [
            (sid, r) for sid, r in statuses.items()
            if r.status in dci.FAILED and (strict or compiled.sources[sid].requirement == "mandatory")
        ]
        if not failed:
            return
        kinds = {r.status for _sid, r in failed}
        if dci.DENIED in kinds:
            http, code = 403, "source_denied"
        elif dci.UNAVAILABLE in kinds:
            timeouts = any("timeout" in (r.detail or "") or "timed out" in (r.detail or "") for _s, r in failed)
            http, code = (504 if timeouts else 503), "source_unavailable"
        else:
            http, code = 502, "source_error"
        names = ", ".join(f"{sid} ({r.status})" for sid, r in failed)
        rule = "partial responses are not allowed" if strict else "mandatory source failed"
        raise QueryError(http, code, f"{rule}: {names}", sources={sid: r.public() for sid, r in statuses.items()})

    @staticmethod
    def assemble(compiled: CompiledUseCase, subject, parameters, statuses: Dict[str, SourceResult]) -> Dict[str, Any]:
        ctx = {
            "subject": subject,
            "parameters": parameters,
            "sources": {sid: {"status": r.status, "records": r.records} for sid, r in statuses.items()},
        }
        # A field built from a source that failed (denied, unavailable, error) is null, so that it
        # never reads as "the registry has nothing" — that is [] / 0 from a no_record answer.
        # message.sources says why.
        failed = {sid for sid, r in statuses.items() if r.status in dci.FAILED}

        def from_failed(out: str) -> bool:
            return bool(failed & compiled.field_sources.get(out, frozenset()))

        data: Dict[str, Any] = {}
        for out, path in compiled.mapping:
            if from_failed(out):
                set_path(data, out, None)
                continue
            try:
                set_path(data, out, path.value(ctx))
            except Exception:
                _logger.warning("use_case=%s: mapping '%s' failed", compiled.ref, out)
                set_path(data, out, None)
        ctx["data"] = data
        for out, node in compiled.derived:
            if from_failed(out):
                set_path(data, out, None)
                continue
            try:
                set_path(data, out, evaluate(node, ctx))
            except Exception:
                _logger.warning("use_case=%s: derived '%s' failed", compiled.ref, out)
                set_path(data, out, None)
        return data

    # ── envelope ─────────────────────────────────────────────────────────────

    def _response_header(self, partner_id, in_reply_to, status, reason=None) -> Dict[str, Any]:
        header = {
            "version": "1.0.0",
            "message_id": str(uuid.uuid4()),
            "message_ts": dci.now_ts(),
            "action": "on-query",
            "status": status,
            "sender_id": self.settings.composite_partner_id,
            "receiver_id": partner_id or "",
            "meta": {"in_reply_to": in_reply_to} if in_reply_to else {},
        }
        if reason:
            header["status_reason_code"], header["status_reason_message"] = reason
        return header

    async def _envelope(self, partner_id, in_reply_to, message, status, reason=None) -> Optional[Dict[str, Any]]:
        header = self._response_header(partner_id, in_reply_to, status, reason)
        try:
            signature = await self.crypto.sign_detached({"header": header, "message": message})
        except Exception as e:
            _logger.error("Cannot sign the response: %s", e)
            return None
        return {"signature": signature, "header": header, "message": message}

    def _emit(self, compiled, kind, request_id, partner_id, outcome, reason=None, context=None):
        if kind in compiled.spec.audit.events:
            self.audit.emit(kind, request_id=request_id, partner_id=partner_id, use_case=compiled.ref,
                            outcome=outcome, reason=reason, context=context)
