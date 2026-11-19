import asyncio
import base64
import json
import logging
from typing import Any, Dict, List, Optional, Tuple

import httpx
from openg2p_fastapi_common.service import BaseService

from ..config import Settings
from ..core.activity import ActivityStore
from ..core.audit import AuditEmitter
from ..core.catalogue import ScopeCatalogue
from ..core.crypto import build_composite_crypto
from ..core.engine import CompositeEngine, EngineSettings
from ..core.loader import UseCaseRegistry
from ..core.policies import PartnerPolicyStore
from ..core.ratelimit import RateLimiter

_config = Settings.get_config()
_logger = logging.getLogger(_config.logging_default_logger_name)


def console_active() -> bool:
    """The console runs only when switched on and given a database (its call log)."""
    return bool(_config.console_enabled and _config.db_datasource and not _config.db_datasource.startswith("none:"))


class CompositeService(BaseService):
    """Wires the engine to the service configuration.

    One pooled ``httpx.AsyncClient`` per worker process, created lazily on the
    worker's event loop (after gunicorn forks) and closed at shutdown. Startup
    loads the use-case files but never calls a registry.
    """

    def __init__(self, name="", http_transport: Optional[httpx.AsyncBaseTransport] = None, pm_transport=None,
                 activity: Optional[ActivityStore] = None, **kwargs):
        super().__init__(name=name, **kwargs)
        self._transport = http_transport
        self._client: Optional[httpx.AsyncClient] = None
        self._reload_task: Optional[asyncio.Task] = None

        registries = {cid: ep.model_dump() for cid, ep in _config.registries.items()}
        if not registries:
            _logger.warning("No registries configured (AGRI_COMPOSITE_REGISTRIES); every source will fail")
        self.registry = UseCaseRegistry(_config.use_cases_dir, known_controllers=registries.keys())
        self.registry.load()

        self.crypto = build_composite_crypto(
            partner_mgmt_api_url=_config.partner_mgmt_api_url,
            signing_p12_path=_config.signing_p12_path,
            signing_p12_password=_config.signing_p12_password,
            signing_kid=_config.signing_kid,
            signing_key_pem_path=_config.signing_key_pem_path,
            signing_algorithm=_config.signing_algorithm,
            allowed_algorithms=_config.crypto_allowed_algorithms,
            soft_ttl=_config.partner_key_cache_ttl_seconds,
            hard_ttl=_config.partner_key_hard_ttl_seconds,
            negative_ttl=_config.partner_key_negative_ttl_seconds,
            refresh_cooldown=_config.partner_key_refresh_cooldown_seconds,
            fetch_timeout=_config.partner_key_fetch_timeout_seconds,
            pm_transport=pm_transport,
        )
        if _config.consent_mode == "exchange" and not _config.consent_exchange_cm_url:
            _logger.error("Consent mode is exchange but AGRI_COMPOSITE_CONSENT_EXCHANGE_CM_URL is empty; "
                          "every query that needs consent will fail")
        self.audit = AuditEmitter(
            _config.audit_manager_url, self.http_client, timeout=_config.audit_timeout_seconds, source=_config.audit_source
        )
        if activity is None:
            activity = ActivityStore(retention_days=_config.activity_retention_days)
            if console_active():
                from openg2p_fastapi_common.context import async_session_maker, dbengine

                activity = ActivityStore(async_session_maker.get(), dbengine.get(), _config.activity_retention_days)
        self.activity = activity
        self.catalogue = ScopeCatalogue(
            registries, self.crypto, self.http_client, _config.composite_partner_id,
            ttl=_config.catalogue_cache_seconds,
        )
        self.policies = PartnerPolicyStore(
            _config.partner_mgmt_api_url, self.http_client,
            soft_ttl=_config.partner_policy_cache_ttl_seconds,
            hard_ttl=_config.partner_policy_hard_ttl_seconds,
            negative_ttl=_config.partner_policy_negative_ttl_seconds,
            refresh_cooldown=_config.partner_policy_refresh_cooldown_seconds,
            timeout=_config.partner_policy_fetch_timeout_seconds,
        )
        self.limiter = RateLimiter(
            redis_url=_config.rate_limit_redis_url,
            redis_password=_config.rate_limit_redis_password,
            key_prefix=_config.rate_limit_redis_key_prefix,
            redis_timeout_seconds=_config.rate_limit_redis_timeout_seconds,
            redis_retry_seconds=_config.rate_limit_redis_retry_seconds,
        )
        if not self.limiter.shared:
            _logger.info("No AGRI_COMPOSITE_RATE_LIMIT_REDIS_URL; rate limits count per worker process")
        self.engine = CompositeEngine(
            EngineSettings(
                composite_partner_id=_config.composite_partner_id,
                request_max_skew_seconds=_config.request_max_skew_seconds,
                default_source_timeout_ms=_config.default_source_timeout_ms,
                default_overall_timeout_ms=_config.default_overall_timeout_ms,
                registries=registries,
                consent_mode=_config.consent_mode,
                exchange_cm_url=_config.consent_exchange_cm_url,
                exchange_cm_timeout_seconds=_config.consent_exchange_cm_timeout_seconds,
            ),
            self.registry,
            self.crypto,
            self.http_client,
            audit=self.audit,
            limiter=self.limiter,
            activity=self.activity,
            policies=self.policies,
        )

    def http_client(self) -> httpx.AsyncClient:
        if self._client is None or self._client.is_closed:
            self._client = httpx.AsyncClient(
                transport=self._transport,
                limits=httpx.Limits(
                    max_connections=_config.http_max_connections,
                    max_keepalive_connections=_config.http_max_keepalive_connections,
                    keepalive_expiry=_config.http_keepalive_expiry_seconds,
                ),
                timeout=_config.default_source_timeout_ms / 1000.0,
            )
        return self._client

    async def signing_kid(self) -> Optional[str]:
        """The kid the composite signs with (from a throw-away signature), or None if it cannot sign."""
        if not self.crypto.can_sign:
            return None
        try:
            jws = await self.crypto.sign_detached({"header": {}, "message": {}})
            head = jws.split(".")[0]
            return json.loads(base64.urlsafe_b64decode(head + "=" * (-len(head) % 4))).get("kid")
        except Exception:
            _logger.warning("Cannot read the composite's signing kid", exc_info=True)
            return None

    async def partner_keys(self, pm_reference: str) -> Tuple[Optional[List[Dict[str, Any]]], Optional[str]]:
        """([{kid, algorithm}] Partner Management serves for the partner, or None, error message)."""
        url = f"{_config.partner_mgmt_api_url.rstrip('/')}/keys/{pm_reference}"
        try:
            response = await self.http_client().get(url, timeout=_config.partner_key_fetch_timeout_seconds)
        except Exception as e:
            return None, f"Partner Management unreachable: {type(e).__name__}"
        if response.status_code == 404:
            return [], None
        if response.status_code != 200:
            return None, f"HTTP {response.status_code} from Partner Management"
        try:
            keys = response.json().get("keys") or []
        except (ValueError, AttributeError):
            return None, "Partner Management answer is not JSON"
        return [{"kid": k.get("kid"), "algorithm": k.get("algorithm")} for k in keys if isinstance(k, dict)], None

    async def start(self):
        await self.activity.start()
        if _config.use_cases_reload_seconds > 0 and self._reload_task is None:
            self._reload_task = asyncio.get_running_loop().create_task(self._reload_loop())

    async def stop(self):
        if self._reload_task:
            self._reload_task.cancel()
            self._reload_task = None
        await self.audit.drain()
        await self.activity.stop()
        await self.limiter.close()
        if self._client is not None:
            await self._client.aclose()
            self._client = None

    async def _reload_loop(self):
        while True:
            await asyncio.sleep(_config.use_cases_reload_seconds)
            try:
                if self.registry.reload_if_changed():
                    _logger.info("Use-case files changed; reloaded")
            except Exception:
                _logger.exception("Use-case reload failed; keeping the loaded set")

