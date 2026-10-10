# ruff: noqa: E402
import asyncio
import logging

from .config import Settings

_config = Settings.get_config()

from openg2p_fastapi_common.app import Initializer as BaseInitializer

from .controllers import AggregatorController
from .models import AggregationGrant, AggregationRequest
from .registry_catalog import CatalogError, get_catalog
from .services import (
    AggregatorService,
    CMClient,
    CryptoService,
    OtpPublisher,
    OtpService,
    RegistryClient,
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
        OtpPublisher()   # no-op unless otp_publish_enabled
        OtpService()
        RegistryClient()   # depends on CryptoService
        AggregatorService()  # depends on CMClient, RegistryClient, OtpService

        AggregatorController().post_init()

    # ── the aggregation queue ───────────────────────────────────────────────
    #
    # These are the platform's lifespan hooks, called from the base
    # Initializer's ``fastapi_app_lifespan``. They are the only ones that fire:
    # because that lifespan is passed to FastAPI explicitly, Starlette ignores
    # any ``@app.on_event`` handler registered alongside it.
    #
    # With ``kafka_consumers_in_app`` the API process also consumes (dev). In
    # production set it false and run ``python -m openg2p_aggregation_layer.worker``.

    async def fastapi_app_startup(self, app):
        await super().fastapi_app_startup(app)
        # Consent decisions are read back from the CM, not pushed by it; see
        # services/cm_poller.py. In production the worker polls instead.
        if _config.cm_poll_in_app:
            from .services.cm_poller import poller

            await poller.start()
        if not _config.kafka_enabled:
            return
        from .kafka_bus.bus import bus

        await bus.start()
        if _config.kafka_consumers_in_app:
            from .kafka_bus.consumers import runner

            await runner.start()
        else:
            _logger.info("kafka_consumers_in_app=false - this process publishes "
                         "only; run `python -m openg2p_aggregation_layer.worker`")

    async def fastapi_app_shutdown(self, app):
        from .services.cm_poller import poller

        await poller.stop()
        if _config.kafka_enabled:
            from .kafka_bus.bus import bus
            from .kafka_bus.consumers import runner

            if _config.kafka_consumers_in_app:
                await runner.stop()
            await bus.stop()
        await super().fastapi_app_shutdown(app)

    def migrate_database(self, args):
        super().migrate_database(args)

        async def migrate():
            _logger.info("Migrating aggregation layer database")
            # A fresh database: the tables are created in their current shape,
            # so none of the ALTERs the CM carried for older schemas apply here.
            await AggregationRequest.create_migrate()
            # What the subject authorised per registry hop - this service's
            # own record now that the CM records nothing for it.
            await AggregationGrant.create_migrate()

            from openg2p_fastapi_common.context import dbengine
            from sqlalchemy import text

            async with dbengine.get().begin() as conn:
                # The reaper and the queue depth both filter on (status,
                # claimed_at); without this they sequentially scan the table.
                await conn.execute(
                    text(
                        "CREATE INDEX IF NOT EXISTS "
                        "ix_aggregation_requests_status_claimed_at "
                        "ON aggregation_requests (status, claimed_at)"
                    )
                )
                await conn.execute(
                    text(
                        "CREATE INDEX IF NOT EXISTS "
                        "ix_aggregation_requests_consent_request_id "
                        "ON aggregation_requests (consent_request_id)"
                    )
                )
                # The Beneficiary-360 contract replaced field aliases with
                # scope ids: the column is renamed rather than re-created, so
                # history keeps what was asked. Idempotent.
                await conn.execute(
                    text(
                        "DO $$ BEGIN IF EXISTS (SELECT 1 FROM "
                        "information_schema.columns WHERE table_name = "
                        "'aggregation_requests' AND column_name = "
                        "'requested_fields') AND NOT EXISTS (SELECT 1 FROM "
                        "information_schema.columns WHERE table_name = "
                        "'aggregation_requests' AND column_name = "
                        "'requested_scopes') THEN ALTER TABLE "
                        "aggregation_requests RENAME COLUMN requested_fields "
                        "TO requested_scopes; END IF; END $$"
                    )
                )
                # Columns added after the first release. create_migrate() does
                # not ALTER an existing table, so a database created before
                # carries them here; idempotent on every start.
                for column, ddl in (
                    ("consent_jws", "TEXT"),
                    ("cm_consent_id", "VARCHAR"),
                    ("grant_ids", "JSONB DEFAULT '{}'::jsonb"),
                    ("query", "JSONB DEFAULT '{}'::jsonb"),
                ):
                    await conn.execute(
                        text(
                            "ALTER TABLE aggregation_requests ADD COLUMN IF NOT "
                            "EXISTS %s %s" % (column, ddl)
                        )
                    )
                await conn.execute(
                    text(
                        "CREATE INDEX IF NOT EXISTS "
                        "ix_aggregation_requests_cm_consent_id "
                        "ON aggregation_requests (cm_consent_id)"
                    )
                )
                # The withdrawal sweep and the reuse lookup both start here.
                await conn.execute(
                    text(
                        "CREATE INDEX IF NOT EXISTS "
                        "ix_aggregation_grants_reuse "
                        "ON aggregation_grants (partner_id, subject_id_value, "
                        "registry, status)"
                    )
                )
            _logger.info("Database migration complete")

        asyncio.run(migrate())
