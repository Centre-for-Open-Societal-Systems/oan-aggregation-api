#!/usr/bin/env python3
"""Release aggregations abandoned by a worker that died.

    python -m openg2p_aggregation_layer.reap

A worker claims a row by moving its status, does the work, then moves it on.
Kill it in between and the row keeps a claim nobody holds — and the Kafka
message, redelivered to its replacement, is correctly refused as already
claimed. This is the sweep that unsticks that: rows claimed longer ago than
``kafka_claim_timeout_sec`` go back to 'queued' and are published again.

Built as a one-shot command rather than an in-process timer for the same
reason ``expire.py`` is: the API pods stay stateless, and the sweep runs once
per tick regardless of how many replicas are up. Run it from a CronJob every
few minutes.
"""

# ruff: noqa: E402, I001
import asyncio
import logging

from .config import Settings

_config = Settings.get_config()

from .app import Initializer
from .services import AggregatorService

_logger = logging.getLogger(_config.logging_default_logger_name)


async def _run() -> int:
    from .kafka_bus.bus import bus

    aggregator = AggregatorService.get_component()
    released = await aggregator.reap_stale_claims()
    if released and _config.kafka_enabled:
        await bus.start()
        for request_id in released:
            await aggregator.republish(request_id)
        await bus.stop()
    elif released:
        # No broker: the rows are back in 'queued' and the in-process path is
        # what will pick them up, so drive them directly.
        for request_id in released:
            await aggregator._fan_out_and_deliver(request_id)
    return len(released)


def main() -> None:
    Initializer().initialize()
    count = asyncio.run(_run())
    _logger.info("Reap run complete: %d aggregation(s) released", count)


if __name__ == "__main__":
    main()
