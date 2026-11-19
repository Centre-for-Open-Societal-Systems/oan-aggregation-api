"""Consent templates: each published use case granted to a partner, for the Consent Manager.

Internal (in-cluster, no auth), read by the Exchange Consent Manager's partner portal and
assisted entry, which offer these templates instead of knowing use cases. The chart keeps
``/composite/v1/internal`` off the public host (the API VirtualService answers 404 there),
and a request that came through the public gateway (``x-forwarded-host`` set by its
VirtualService) is answered 404 here too.

``partner`` is the partner's audience / sender ID (``bank-a``), mapped to its PM partner
ID (``PARTNER_BANK_A``) as for senders. A template is listed only when the partner has an
active PM use-case policy for it; its ``limits`` come from that policy: ``allowed_subject_id_types``
and ``max_validity_duration`` when set, ``allow_partner_consents`` and ``allow_assisted_confirmation``
always (false when the policy does not set them).
"""

import asyncio
import re
from typing import Any, Dict, List, Optional, Tuple

from fastapi import Path, Query, Request
from fastapi.responses import JSONResponse
from openg2p_fastapi_common.controller import BaseController

from ..core.loader import CompiledUseCase
from ..core.policies import PolicyUnavailable, policy_limits
from ..services.composite_service import CompositeService

INTERNAL_PREFIX = "/composite/v1/internal"
CACHE_CONTROL = "private, max-age=60"
PUBLIC_HOST_HEADER = "x-forwarded-host"
# A partner ID as senders use it (it becomes part of a Partner Management URL path).
PARTNER_PATTERN = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,254}")


def consent_template(compiled: CompiledUseCase, policy: Dict[str, Any]) -> Dict[str, Any]:
    """One use case as a consent template: purpose, scopes per registry, the policy's limits."""
    s = compiled.spec
    declared = s.consent_scopes()
    scopes = {
        c: declared.get(c, {"required": [], "optional": []})
        for c in sorted({src.controller for src in s.sources})
    }
    return {
        "code": compiled.ref,
        "title": s.title,
        "description": s.description,
        "purpose": s.purpose,
        "scopes": scopes,
        "limits": policy_limits(policy, signing_algs=False),
    }


def _error(status: int, code: str, message: str) -> JSONResponse:
    return JSONResponse(status_code=status, content={"error": {"code": code, "message": message}},
                        headers={"Cache-Control": "no-store"})


def _not_found() -> JSONResponse:
    # One answer for an unknown template, one not granted, and an unknown partner.
    return _error(404, "unknown_template", "no consent template by that code for this partner")


class ConsentTemplateController(BaseController):
    """Internal: consent templates per partner, for the Consent Manager (not on the public host)."""

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self.service = CompositeService.get_component()
        self.router.prefix += INTERNAL_PREFIX
        self.router.tags += ["Internal"]
        # Not in the partner API's OpenAPI document: in-cluster only.
        self.router.add_api_route("/consent-templates", self.list_templates, methods=["GET"],
                                  include_in_schema=False)
        self.router.add_api_route("/consent-templates/{code}", self.get_template, methods=["GET"],
                                  include_in_schema=False)

    async def _granted(self, partner: str, published: List[CompiledUseCase]
                       ) -> List[Tuple[CompiledUseCase, Dict[str, Any]]]:
        policies = self.service.policies
        answers = await asyncio.gather(*(policies.use_case_policy(partner, c.ref) for c in published))
        return [(c, p) for c, p in zip(published, answers) if p is not None]

    @staticmethod
    def _public(request: Request) -> bool:
        return bool(request.headers.get(PUBLIC_HOST_HEADER))

    async def list_templates(self, request: Request, partner: str = Query(...)):
        if self._public(request):
            return _error(404, "not_found", "Not Found")
        try:
            granted = [] if not PARTNER_PATTERN.fullmatch(partner) else \
                await self._granted(partner, self.service.registry.published())
        except PolicyUnavailable:
            return _error(503, "partner_policy_unavailable", "Partner Management cannot be read; try again later")
        return JSONResponse({"templates": [consent_template(c, p) for c, p in granted]},
                            headers={"Cache-Control": CACHE_CONTROL})

    async def get_template(self, request: Request, code: str = Path(...),
                           partner: str = Query(...)):
        if self._public(request):
            return _error(404, "not_found", "Not Found")
        compiled: Optional[CompiledUseCase] = next(
            (c for c in self.service.registry.published() if c.ref == code), None)
        if compiled is None or not PARTNER_PATTERN.fullmatch(partner):
            return _not_found()
        try:
            granted = await self._granted(partner, [compiled])
        except PolicyUnavailable:
            return _error(503, "partner_policy_unavailable", "Partner Management cannot be read; try again later")
        if not granted:
            return _not_found()
        return JSONResponse(consent_template(*granted[0]), headers={"Cache-Control": CACHE_CONTROL})
