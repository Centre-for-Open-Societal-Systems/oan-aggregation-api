#!/usr/bin/env python3
"""Standalone aggregation worker.

    python -m openg2p_aggregation_layer.worker

Runs the three Kafka consumers — registry fan-out, callback delivery, and the
retry hold — and the CM poll loop (services/cm_poller.py), with no HTTP server
attached. This is the production shape: the
API pods publish and nothing else, so a subject waiting on the consent portal
is never behind fifty registry queries on the same event loop. Scale it
independently of the API; the ceiling on how much work runs at once is the
number of workers times ``kafka_fanout_concurrency``, capped by the topic's
partition count.

Set ``aggregation_layer_kafka_consumers_in_app=false`` and
``aggregation_layer_cm_poll_in_app=false`` on the API deployment when running
this, or both will consume and poll — which is not wrong (consumers join the
same group and split the partitions; pollers claim rows) but puts the load
back where it was.
"""

# ruff: noqa: E402, I001
import asyncio
import logging
import signal

from .config import Settings

_config = Settings.get_config()

from .app import Initializer

_logger = logging.getLogger(_config.logging_default_logger_name)


async def _run() -> None:
    from .kafka_bus.bus import bus
    from .kafka_bus.consumers import runner
    from .services.cm_poller import poller

    stopping = asyncio.Event()

    def _request_stop(*_args) -> None:
        _logger.info("Shutdown signal received; draining the current batch")
        stopping.set()

    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, _request_stop)
        except NotImplementedError:
            # Windows: no loop signal handlers. Ctrl-C still raises
            # KeyboardInterrupt out of asyncio.run, which is good enough.
            signal.signal(sig, _request_stop)

    await runner.start()
    await poller.start()
    if not _config.kafka_enabled and not poller.enabled:
        _logger.error("aggregation_layer_kafka_enabled=false and "
                      "cm_poll_interval_sec=0 - there is nothing for this "
                      "worker to do. Exiting.")
        return
    try:
        await stopping.wait()
    finally:
        await poller.stop()
        await runner.stop()
        # The runner does not own the bus — this process does.
        await bus.stop()


def main() -> None:
    # Builds the service graph and the DB engine without serving HTTP: the
    # consumers call straight into AggregatorService, so it has to exist.
    Initializer().initialize()
    _logger.info("Aggregation worker starting against %s",
                 _config.kafka_bootstrap_servers)
    try:
        asyncio.run(_run())
    except KeyboardInterrupt:
        pass
    _logger.info("Aggregation worker stopped")


if __name__ == "__main__":
    main()
