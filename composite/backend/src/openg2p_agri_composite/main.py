#!/usr/bin/env python3

# ruff: noqa: I001, E402

from openg2p_agri_composite.config import Settings

_config = Settings.get_config()

from openg2p_agri_composite.services.composite_service import console_active

if console_active():
    # Staff login (IAM) for the console, as in Master Data: initialised after the
    # Settings above so iam-core reads AGRI_COMPOSITE_*.
    from iam_core.user_auth.app import Initializer as IAMInitializer

    IAMInitializer()

from openg2p_agri_composite.app import Initializer
from openg2p_fastapi_common.ping import PingInitializer

initializer = Initializer()
PingInitializer()

app = initializer.return_app()

if console_active():
    from iam_core.user_auth.middleware import (
        CsrfMiddleware,
        ResolvePermissionMiddleware,
        ValidateAndRefreshTokenMiddleware,
    )

    # Only endpoints that declare a permission (the console's /admin routes) are
    # checked; the partner API (signed envelopes, no session) passes through, and
    # its POST .../query is exempt from the browser CSRF check.
    # Order (last added = outermost): CSRF -> ValidateAndRefresh -> ResolvePermission -> app
    app.add_middleware(ResolvePermissionMiddleware, client_id=_config.keycloak_client_id, allow_by_default=True)
    app.add_middleware(ValidateAndRefreshTokenMiddleware)
    app.add_middleware(CsrfMiddleware, enabled=_config.csrf_enabled, excluded_paths=("/query", "/ping"))

if __name__ == "__main__":
    initializer.main()
