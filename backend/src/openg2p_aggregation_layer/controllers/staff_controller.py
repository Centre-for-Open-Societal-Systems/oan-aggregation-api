"""Staff search routes.

    POST /aggregation/v1/staff/registry/search    staff, role-gated, no consent
    POST /aggregation/v1/staff/registry/tab       one tab of a record that search returned

A registry staff user looks a beneficiary up across every registry with a
``staff_api`` in the catalog and gets, per record, a summary and its tabs. A
tab's data is fetched only when the user opens it, with the request the
search handed out for that tab. See services/staff_search_service.py for what
is (and is not) checked.
"""
import logging

from fastapi import Depends, HTTPException, status
from fastapi.responses import JSONResponse
from openg2p_fastapi_common.controller import BaseController

from ..auth import CallerIdentity, auth_config_problem, current_identity
from ..config import Settings
from ..schemas.staff import (
    StaffSearchRequest,
    StaffSearchResponse,
    StaffTabRequest,
    StaffTabResponse,
)
from ..services.aggregator_service import AggregationError
from ..services.staff_search_service import TAB_PATH, StaffSearchService

_config = Settings.get_config()
_logger = logging.getLogger(_config.logging_default_logger_name)

_PREFIX = "/aggregation/v1/staff"


async def current_staff(identity: CallerIdentity = Depends(current_identity)) -> CallerIdentity:
    """A caller holding one of ``staff_search_roles``; 403 otherwise.

    With auth disabled (dev) current_identity hands out a synthetic identity
    with no roles, so the route stays closed rather than opening to anyone.
    """
    if not StaffSearchService.allowed_roles(identity):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="staff search requires one of the roles: %s"
                   % (", ".join(_config.staff_search_roles) or "(none configured)"))
    return identity


def _error(exc: AggregationError) -> JSONResponse:
    headers = {"Retry-After": "60"} if exc.status == 429 else None
    return JSONResponse(status_code=exc.status, headers=headers,
                        content={"error": exc.reason, "detail": exc.detail})


class StaffController(BaseController):
    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self.staff_search = StaffSearchService.get_component()
        self.router.tags += ["Aggregation Layer (staff search, no consent)"]
        problem = auth_config_problem()
        if problem:
            _logger.error("Staff search routes will refuse every token (503): %s", problem)

        self.router.add_api_route(
            _PREFIX + "/registry/search", self.search,
            responses={200: {"model": StaffSearchResponse}}, methods=["POST"],
            response_model=StaffSearchResponse, response_model_by_alias=True,
        )
        self.router.add_api_route(
            TAB_PATH, self.tab,
            responses={200: {"model": StaffTabResponse}}, methods=["POST"],
            response_model=StaffTabResponse, response_model_by_alias=True,
        )

    async def search(self, data: StaffSearchRequest,
                     staff: CallerIdentity = Depends(current_staff)):
        """Find a beneficiary in every staff-enabled registry: summary and tabs per record.

        No consent, consent object or OTP: the caller's role is the authority
        here and each registry's staff-api checks this service's own service
        account. Every call is audited (a log line). A registry that fails is
        reported in its own entry (``status: error``); the others still answer.
        Each tab carries the ``request`` that opens it; tab data is not included.
        """
        try:
            self.staff_search.check_rate(staff)
            return await self.staff_search.search(
                staff, data.foundational_id, data.registry_filter, data.purpose)
        except AggregationError as exc:
            return _error(exc)

    async def tab(self, data: StaffTabRequest,
                  staff: CallerIdentity = Depends(current_staff)):
        """One tab of a record the caller's search returned: sections, fields and rows.

        Send the ``request.body`` a search gave for the tab. Refused for a
        record that search did not return (400), another user's or an unknown
        search (404), or one older than ``staff_search_ttl_sec`` (410: search
        again). List sections are paged (``page``, ``pageSize``). Audited.
        """
        try:
            self.staff_search.check_rate(staff)
            return await self.staff_search.tab(
                staff, data.search_id, data.registry_code, data.register_id,
                data.record_id, data.tab_id, data.page, data.page_size)
        except AggregationError as exc:
            return _error(exc)
