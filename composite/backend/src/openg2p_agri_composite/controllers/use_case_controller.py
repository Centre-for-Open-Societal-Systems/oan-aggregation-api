from typing import Any, Optional

from fastapi import Header, Path, Request
from fastapi.responses import ORJSONResponse
from openg2p_fastapi_common.controller import BaseController

from ..services.composite_service import CompositeService
from .api_docs import (
    ROUTER_RESPONSES,
    QUERY_REQUEST_BODY,
    QUERY_RESPONSES,
    USE_CASE_MAJOR_HEADER,
    USE_CASE_PATH,
    ApiError,
    UseCaseDescription,
    UseCaseList,
)


class UseCaseController(BaseController):
    """Partner API: describe published use cases and query one.

    Trust is the partner-signed envelope verified against Partner Management
    keys (no Keycloak). The describe endpoints return configuration only.
    """

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self.service = CompositeService.get_component()
        self.router.prefix += "/composite/v1"
        self.router.tags += ["Use cases"]
        # Documented per route (the framework default lists 401/403/404 on every route).
        self.router.responses = dict(ROUTER_RESPONSES)

        self.router.add_api_route(
            "/use-cases",
            self.list_use_cases,
            methods=["GET"],
            summary="List use cases",
            description=(
                "The published use cases: for each, the subject types and parameters it takes, the registries "
                "(sources) it queries, the consent grants and data scopes it needs, and its output fields. "
                "Configuration only; not signed and needs no signature."
            ),
            responses={200: {"model": UseCaseList, "description": "The published use cases."}},
        )
        self.router.add_api_route(
            "/use-cases/{use_case}",
            self.describe_use_case,
            methods=["GET"],
            summary="Describe a use case",
            description=(
                "One published use case, by `name` or `name@major` (or `name` with the `X-Use-Case-Major` header). "
                "Without a major version, the highest published one. Use it to build the query and the consent: "
                "`input` lists the subject types and parameters, `consent_grants_needed` and `consent_scopes` "
                "what the consent must grant. Configuration only; needs no signature."
            ),
            responses={
                200: {"model": UseCaseDescription, "description": "The use case."},
                400: {"model": ApiError, "description": "`invalid_use_case`: the path and the header name different "
                      "major versions, or the reference is malformed."},
                404: {"model": ApiError, "description": "`unknown_use_case`: no published use case by that reference."},
            },
        )
        self.router.add_api_route(
            "/use-cases/{use_case}/query",
            self.query,
            methods=["POST"],
            summary="Query a use case",
            description=(
                "Serve one query for one subject. The body is a signed envelope `{signature, header, message}`: "
                "`signature` is a detached JWS over `{header, message}` made with the partner's key and "
                "verified against Partner Management (`header.sender_id`). The Composite checks that the "
                "partner has a policy for the use case (`name@major`) in Partner Management, and the "
                "subject's consent (`message.consent_id`, `message.consent_jws`, or neither: the Exchange's Consent "
                "Manager then looks up the consent it holds from this partner for this subject and the use "
                "case's purpose), "
                "queries each registry the use case names, and returns one response envelope signed with "
                "its own key.\n\n"
                "Each source gets a status: `ok`, `no_record`, `denied`, `unavailable` or `error`. "
                "A failing optional source leaves its fields null (the response is still 200); "
                "a failing mandatory source fails the request (`source_denied`, `source_unavailable`, `source_error`).\n\n"
                "Every answer, errors included, is a signed envelope: on an error `header.status` is `rjct` and "
                "`message.error` has the `code` and `message`."
            ),
            responses=QUERY_RESPONSES,
            openapi_extra=QUERY_REQUEST_BODY,
        )

    async def list_use_cases(self):
        """Published use cases: input, parameters, sources and output fields."""
        return {"use_cases": [c.describe() for c in self.service.registry.published()]}

    async def describe_use_case(
        self,
        use_case: str = Path(..., description=USE_CASE_PATH),
        x_use_case_major: Optional[int] = Header(None, description=USE_CASE_MAJOR_HEADER),
    ):
        """One published use case (``name`` or ``name@major``)."""
        try:
            compiled = self.service.registry.get(use_case, x_use_case_major)
        except ValueError as e:
            return ORJSONResponse(status_code=400, content={"error": {"code": "invalid_use_case", "message": str(e)}})
        if compiled is None:
            return ORJSONResponse(
                status_code=404,
                content={"error": {"code": "unknown_use_case", "message": f"no published use case '{use_case}'"}},
            )
        return compiled.describe()

    async def query(
        self,
        request: Request,
        use_case: str = Path(..., description=USE_CASE_PATH),
        x_use_case_major: Optional[int] = Header(None, description=USE_CASE_MAJOR_HEADER),
    ):
        """Serve one query. Body: the signed envelope; response: the signed result envelope."""
        try:
            body: Any = await request.json()
        except Exception:
            body = None
        status, envelope = await self.service.engine.handle_query(use_case, body, x_use_case_major)
        return ORJSONResponse(status_code=status, content=envelope)
