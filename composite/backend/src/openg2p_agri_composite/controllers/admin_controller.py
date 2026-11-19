import asyncio
import os
from typing import Any, Dict, List, Optional, Tuple

from fastapi import Query
from fastapi.responses import JSONResponse
from iam_core.user_auth.decorators import require_permissions
from openg2p_fastapi_common.controller import BaseController

from ..config import Settings
from ..core.activity import OUTCOMES
from ..core.catalogue import catalogue_url
from ..core.policies import PolicyUnavailable
from ..services.composite_service import CompositeService
from .api_docs import CONSOLE_RESPONSES, ROUTER_RESPONSES, ApiError

_config = Settings.get_config()

VIEW = "composite:view"


_CONSOLE_AUTH = (
    "\n\nStaff only: needs a console login (IAM session cookie or `Authorization: Bearer`) with the "
    "`composite:view` permission. Present only when the console is on (`console_enabled` and a database)."
)
_NOT_FOUND = {404: {"model": ApiError, "description": "Not found (`unknown_use_case`, `unknown_registry`)."}}
_NOT_FOUND_OR_INVALID = {
    400: {"model": ApiError, "description": "`invalid_use_case`: the reference is malformed."},
    **_NOT_FOUND,
}
_INVALID_OUTCOME = {400: {"model": ApiError, "description": "`invalid_outcome`: unknown `outcome` filter."}}


def _not_found(code: str, message: str) -> JSONResponse:
    return JSONResponse(status_code=404, content={"error": {"code": code, "message": message}})


class AdminController(BaseController):
    """The console's read-only API (staff, IAM session with ``composite:view``).

    Configuration as loaded (use cases, registries, settings), each registry's
    data scope catalogue, the partners Partner Management grants use cases to
    (their use-case policies) with their keys, and the call log.
    """

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self.service = CompositeService.get_component()
        self.router.prefix += "/composite/v1/admin"
        self.router.tags += ["Console (staff)"]
        # Documented per route (the framework default lists 401/403/404 on every route).
        self.router.responses = dict(ROUTER_RESPONSES)
        for path, handler, summary, description, extra in (
            ("/overview", self.overview, "Overview",
             "The Composite's settings (partner ID, consent mode, Partner Management URL, signing key ID), "
             "counts of use cases, registries and partners, and the last 24 hours of calls.", {}),
            ("/use-cases", self.list_use_cases, "List use cases (console)",
             "Published use cases with the partners granted each in Partner Management (`granted_to`: PM "
             "partner IDs; null with `granted_to_error` when PM cannot be read) and file name, and the "
             "use-case files that failed to load (with the error).", {}),
            ("/use-cases/{use_case}", self.get_use_case, "Get a use case (console)",
             "One published use case (`name` or `name@major`) with the partners granted it in Partner "
             "Management, its response mapping, derived fields and the YAML file text.", _NOT_FOUND_OR_INVALID),
            ("/registries", self.list_registries, "List registries",
             "The configured registries: search and catalogue URLs, Partner Management partner ID, whether "
             "the catalogue answered, the number of data scopes, and the use cases that query each.", {}),
            ("/registries/{registry_id}/data-scopes", self.get_data_scopes, "Get a registry's data scopes",
             "The registry's data scope catalogue (each scope's current version and fields) and the use-case "
             "sources that ask for each scope. Cached; `refresh=true` fetches it again.", _NOT_FOUND),
            ("/partners", self.list_partners, "List partners",
             "Partners with a Partner Management policy for a published use case (one policy per use "
             "case): the use cases granted and the keys Partner Management serves for each (key ID and "
             "algorithm). `pm_error` is set when Partner Management cannot be read.", {}),
            ("/activity", self.list_activity, "List calls",
             "The call log, newest first: use case, partner, HTTP status, outcome, reason, duration and "
             "per-source statuses. No request or response data is stored. `total` stops at 10000 "
             "(`total_capped` is then true). `partners`: the partner IDs in the log (for filtering).", _INVALID_OUTCOME),
        ):
            self.router.add_api_route(
                path, handler, methods=["GET"], summary=summary,
                description=description + _CONSOLE_AUTH,
                responses={**CONSOLE_RESPONSES, **extra},
            )

    # ── helpers ──────────────────────────────────────────────────────────────

    def _links(self) -> Dict[str, Optional[str]]:
        return {"pm_portal": _config.console_pm_portal_url or None, "cm_portal": _config.console_cm_portal_url or None}

    async def _grants(self, published) -> Tuple[Dict[str, List[Dict[str, Any]]], Optional[str]]:
        """({use case ref: PM use-case policies granting it}, error): from Partner Management,
        ``GET /policies?use_case=`` per published use case. A use case PM could not be asked about
        is left out, and the error says why."""
        results = await asyncio.gather(
            *(self.service.policies.list_for_use_case(c.ref) for c in published), return_exceptions=True)
        grants: Dict[str, List[Dict[str, Any]]] = {}
        error = None
        for compiled, result in zip(published, results):
            if isinstance(result, PolicyUnavailable):
                error = str(result)
            elif isinstance(result, BaseException):
                raise result
            else:
                grants[compiled.ref] = result
        return grants, error

    @staticmethod
    def _granted_to(policies: Optional[List[Dict[str, Any]]]) -> Optional[List[str]]:
        return None if policies is None else sorted({str(p["partner_id"]) for p in policies})

    def _use_case_entry(self, compiled, grants, error) -> Dict[str, Any]:
        policies = grants.get(compiled.ref)
        return {**compiled.describe(), "granted_to": self._granted_to(policies),
                "granted_to_error": error if policies is None else None,
                "file": os.path.basename(compiled.path)}

    @staticmethod
    def _partners(grants: Dict[str, List[Dict[str, Any]]]) -> Dict[str, Dict[str, Any]]:
        """{PM partner ID: {"use_cases": [...]}} from the use-case policies."""
        out: Dict[str, Dict[str, Any]] = {}
        for ref, policies in grants.items():
            for p in policies:
                entry = out.setdefault(str(p["partner_id"]), {"use_cases": []})
                if ref not in entry["use_cases"]:
                    entry["use_cases"].append(ref)
        return dict(sorted(out.items()))

    def _used_by(self) -> Dict[str, List[str]]:
        out: Dict[str, List[str]] = {}
        for compiled in self.service.registry.published():
            for controller in sorted({s.controller for s in compiled.spec.sources}):
                out.setdefault(controller, []).append(compiled.ref)
        return out

    # ── endpoints ────────────────────────────────────────────────────────────

    @require_permissions(VIEW)
    async def overview(self):
        """Settings, counts and the last day's calls."""
        activity = self.service.activity
        engine = self.service.engine.settings
        grants, error = await self._grants(self.service.registry.published())
        return {
            "composite_partner_id": engine.composite_partner_id,
            "consent_mode": engine.consent_mode,
            "exchange_cm_url": engine.exchange_cm_url if engine.consent_mode == "exchange" else "",
            "partner_mgmt_api_url": _config.partner_mgmt_api_url,
            "audit_manager_enabled": self.service.audit.enabled,
            "activity_recording": activity.enabled,
            "signing_kid": await self.service.signing_kid(),
            "use_cases": {"published": len(self.service.registry.published()),
                          "errors": len(self.service.registry.errors())},
            "registries": len(engine.registries),
            # Partners granted a published use case in Partner Management; null when PM cannot be read.
            "partners": None if error else len(self._partners(grants)),
            "activity_24h": await activity.summary(24) if activity.enabled else None,
            "links": self._links(),
        }

    @require_permissions(VIEW)
    async def list_use_cases(self):
        """Published use cases, with the partners granted each in PM, and the files that failed to load."""
        published = self.service.registry.published()
        grants, error = await self._grants(published)
        return {
            "use_cases": [self._use_case_entry(c, grants, error) for c in published],
            "errors": [{"file": f, "error": e} for f, e in sorted(self.service.registry.errors().items())],
        }

    @require_permissions(VIEW)
    async def get_use_case(self, use_case: str):
        """One published use case (``name`` or ``name@major``), with its mapping and file text."""
        try:
            compiled = self.service.registry.get(use_case)
        except ValueError as e:
            return JSONResponse(status_code=400, content={"error": {"code": "invalid_use_case", "message": str(e)}})
        if compiled is None:
            return _not_found("unknown_use_case", f"no published use case '{use_case}'")
        try:
            with open(compiled.path, encoding="utf-8") as fh:
                text = fh.read()
        except OSError:
            text = None
        grants, error = await self._grants([compiled])
        return {**self._use_case_entry(compiled, grants, error), "mapping": dict(compiled.spec.response.mapping),
                "derived": dict(compiled.spec.response.derived), "source_yaml": text}

    @require_permissions(VIEW)
    async def list_registries(self):
        """Configured registries; reachability and scope count from their (cached) catalogues."""
        registries = self.service.engine.settings.registries
        used_by = self._used_by()
        results = await asyncio.gather(*(self.service.catalogue.fetch(c) for c in registries))
        out = []
        for (controller, endpoint), result in zip(registries.items(), results):
            ok = result["error"] is None
            out.append({
                "id": controller, "url": endpoint.get("url"), "catalogue_url": catalogue_url(endpoint),
                "partner_id": endpoint.get("partner_id") or "", "receiver_id": endpoint.get("receiver_id") or "",
                "reachable": ok, "scope_count": len(result["data_scopes"]) if ok else None,
                "error": result["error"], "used_by": used_by.get(controller, []),
            })
        return {"registries": out}

    @require_permissions(VIEW)
    async def get_data_scopes(
        self, registry_id: str,
        refresh: bool = Query(False, description="Fetch the catalogue again instead of using the cache."),
    ):
        """The registry's data scopes (current version's fields), and which use case sources use each."""
        if registry_id not in self.service.engine.settings.registries:
            return _not_found("unknown_registry", f"no registry '{registry_id}' is configured")
        result = await self.service.catalogue.fetch(registry_id, refresh=refresh)
        used: Dict[str, List[Dict[str, Any]]] = {}
        for compiled in self.service.registry.published():
            for src in compiled.spec.sources:
                for scope in src.scopes:
                    used.setdefault(scope, []).append({"use_case": compiled.ref, "source": src.id, "required": True})
                for scope in src.optional_scopes:
                    used.setdefault(scope, []).append({"use_case": compiled.ref, "source": src.id, "required": False})
        scopes = []
        for s in result["data_scopes"]:
            versions = s.get("versions") or []
            current = next((v for v in versions if v.get("version") == s.get("current_version")), None) or {}
            scope_id = s.get("scope_id") or f"{registry_id}.{s.get('name')}"
            scopes.append({
                "scope_id": scope_id, "name": s.get("name"), "label": s.get("label"),
                "description": s.get("description"), "status": s.get("status"),
                "current_version": s.get("current_version"),
                "fields": current.get("resolved_fields") or current.get("fields") or [],
                "used_by": used.get(scope_id, []),
            })
        return {"registry": registry_id, "data_controller": result["data_controller"],
                "fetched_at": result["fetched_at"], "error": result["error"], "data_scopes": scopes}

    @require_permissions(VIEW)
    async def list_partners(self):
        """Partners with PM policies for published use cases, with the keys PM serves for them."""
        grants, error = await self._grants(self.service.registry.published())
        partners = self._partners(grants)
        keys = await asyncio.gather(*(self.service.partner_keys(p) for p in partners))
        out = []
        for (partner, entry), (served, key_error) in zip(partners.items(), keys):
            out.append({"partner_id": partner, "use_cases": entry["use_cases"], "pm_keys": served,
                        "pm_error": key_error})
        return {"partners": out, "pm_error": error, "links": self._links()}

    @require_permissions(VIEW)
    async def list_activity(
        self,
        limit: int = Query(50, ge=1, le=200, description="Rows per page."),
        offset: int = Query(0, ge=0, description="Rows to skip."),
        partner: Optional[str] = Query(None, description="Only this partner's calls."),
        use_case: Optional[str] = Query(None, description="Only calls to this use case (`name@major`)."),
        outcome: Optional[str] = Query(None, description="Only `success`, `denied` or `failure`."),
    ):
        """The call log, newest first."""
        activity = self.service.activity
        if not activity.enabled:
            return {"recording": False, "total": 0, "total_capped": False, "items": [], "partners": []}
        if outcome and outcome not in OUTCOMES:
            return JSONResponse(status_code=400, content={"error": {
                "code": "invalid_outcome", "message": f"outcome must be one of {list(OUTCOMES)}"}})
        total, capped, items = await activity.query(limit=limit, offset=offset, partner=partner,
                                                    use_case=use_case, outcome=outcome)
        # total stops at 10000 (total_capped true): counting a large log on every page load is slow.
        return {"recording": True, "total": total, "total_capped": capped, "items": items,
                "partners": await activity.partners()}
