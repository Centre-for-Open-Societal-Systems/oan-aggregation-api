# ruff: noqa: E402
import logging

from .config import Settings

_config = Settings.get_config()

from openg2p_fastapi_common.app import Initializer as BaseInitializer

from .controllers import AggregatorController, StaffController
from .registry_catalog import CatalogError, get_catalog
from .services import (
    AggregatorService,
    CMClient,
    CryptoService,
    RegistryClient,
    StaffApiClient,
    StaffSearchService,
)

_logger = logging.getLogger(_config.logging_default_logger_name)


class Initializer(BaseInitializer):
    def initialize(self, **kwargs):
        super().initialize(**kwargs)

        # The registry catalog first, and fatally: a service that starts with
        # a broken catalog fails on the first partner call instead, far from
        # the cause. The message lists every problem in the file.
        try:
            catalog = get_catalog()
        except CatalogError as exc:
            _logger.critical("%s", exc)
            raise
        _logger.info("Registry catalog %s: %s", _config.registry_catalog_path,
                     ", ".join(catalog.codes()))

        # Services — order matters: a service that calls get_component() in its
        # __init__ must be constructed after its dependencies.
        CryptoService()
        CMClient()
        RegistryClient()   # depends on CryptoService
        AggregatorService()  # depends on CMClient, RegistryClient
        StaffApiClient()
        StaffSearchService()  # depends on StaffApiClient, CryptoService

        AggregatorController().post_init()
        StaffController().post_init()

    def init_db(self):
        # No database: every answer is built from the call itself and the
        # Consent Manager's decision on it.
        pass
