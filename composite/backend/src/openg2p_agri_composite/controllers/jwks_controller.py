from fastapi.responses import ORJSONResponse
from openg2p_fastapi_common.controller import BaseController

from ..services.composite_service import CompositeService


class JwksController(BaseController):
    """The composite's public signing key(s), for Partner Management to read.

    Partner Management onboards the composite with this URL and reads its keys
    from it when a registry checks the composite's signature, so the key can be
    generated at install and rotated without re-entering it anywhere.
    """

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self.service = CompositeService.get_component()
        self.router.tags += ["Keys"]
        self.router.add_api_route(
            "/.well-known/jwks.json",
            self.get_jwks,
            methods=["GET"],
            summary="The composite's public keys (JWKS)",
            description="Public key(s) the composite signs with; empty `keys` when it has no signing key.",
        )

    async def get_jwks(self):
        return ORJSONResponse(self.service.crypto.jwks, headers={"Cache-Control": "public, max-age=300"})
