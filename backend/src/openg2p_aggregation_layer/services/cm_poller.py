"""The loop that reads consent decisions back from the Consent Manager.

The CM pushes nothing to this service. Whether a raised consent request has
been approved, denied or has expired, and whether a consent an in-flight
aggregation stands on has been withdrawn, are read from the CM's generic APIs
by ``AggregatorService.sync_with_cm`` - and this loop is what calls it every
``cm_poll_interval_sec``.

It runs in the API process when ``cm_poll_in_app`` is true (dev), and in
``python -m openg2p_aggregation_layer.worker`` (production). Running both is
safe: parked rows are claimed before they are worked on and every step is
idempotent. ``python -m openg2p_aggregation_layer.reap`` does one pass too, so
a deployment that runs neither loop still converges on each reaper tick.
"""
import asyncio
import logging
from typing import Optional

from ..config import Settings

_config = Settings.get_config()
_logger = logging.getLogger(_config.logging_default_logger_name)


class CMPoller:
    def __init__(self) -> None:
        self._task: Optional[asyncio.Task] = None
        self._stopping = asyncio.Event()

    @property
    def enabled(self) -> bool:
        return _config.cm_poll_interval_sec > 0

    async def start(self) -> None:
        if not self.enabled or self._task is not None:
            return
        self._stopping = asyncio.Event()
        self._task = asyncio.create_task(self._run(), name="CMPoller")
        _logger.info("CM poll loop started (every %ds)", _config.cm_poll_interval_sec)

    async def stop(self) -> None:
        if self._task is None:
            return
        self._stopping.set()
        self._task.cancel()
        await asyncio.gather(self._task, return_exceptions=True)
        self._task = None

    async def _run(self) -> None:
        # Resolved late: the service graph is built after this module imports.
        from .aggregator_service import AggregatorService

        aggregator = AggregatorService.get_component()
        while not self._stopping.is_set():
            try:
                result = await aggregator.sync_with_cm()
                if any(result.values()):
                    _logger.info("CM poll: %s", result)
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001 - one bad pass must not end the loop
                _logger.exception("CM poll pass failed")
            try:
                await asyncio.wait_for(self._stopping.wait(),
                                       timeout=_config.cm_poll_interval_sec)
            except asyncio.TimeoutError:
                pass


poller = CMPoller()
