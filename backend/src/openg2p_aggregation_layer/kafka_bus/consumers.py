"""The consumer side: three loops, one per stage.

    FanOutConsumer    aggregation.fanout           → queries the registries
    DeliveryConsumer  aggregation.delivery         → POSTs to the partner
    RetryConsumer     aggregation.delivery.retry   → waits out a backoff

Offsets and failure
-------------------
Every consumer commits its offsets after the batch, **whether or not the work
succeeded**, and expresses failure in the database and on the dead-letter topic
instead of by replaying the offset. That is a deliberate choice: replaying is
the standard way to retry, but here a message that fails deterministically —
a registry that rejects the aggregator's key, a callback URL that no longer
parses — would be redelivered forever, and each redelivery re-queries live
registries for real personal data. A poison message in this pipeline is not
just noisy, it is a repeated disclosure attempt.

So retries are explicit and bounded: delivery failures move to the retry topic
with a backoff and an attempt count, and anything else is dead-lettered once.
What that gives up is automatic recovery from a worker that dies *after* doing
the work but *before* committing. That case is covered instead by the claim
columns and ``reap_stale_claims()`` — see `-m openg2p_aggregation_layer.reap`.

Concurrency
-----------
Each loop fetches a batch and processes it with a bounded ``gather``. The
ceiling on registry load is therefore ``kafka_fanout_concurrency`` times the
number of fan-out consumers, itself capped by the partition count — a number
an operator can state, which is the thing the in-process
``asyncio.create_task`` per verified OTP never had.
"""
import asyncio
import logging
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional

from ..config import Settings
from .bus import bus
from .topics import (
    TYPE_DEAD_LETTER,
    TYPE_DELIVERY_REQUESTED,
    TYPE_DELIVERY_RETRY,
    envelope,
    parse,
)

_config = Settings.get_config()
_logger = logging.getLogger(_config.logging_default_logger_name)


def _aggregator():
    """Resolved late: the service graph is built after this module imports."""
    from ..services.aggregator_service import AggregatorService

    return AggregatorService.get_component()


def _max_poll_interval_ms(longest_wait: int = 0) -> int:
    """Long enough that a deliberate sleep is not mistaken for a dead consumer.

    A consumer that does not poll within this window has its partitions
    reassigned. Only the retry tiers sleep, and each one sleeps exactly its own
    delay — so the window is sized per consumer rather than globally to the
    longest backoff, which would give the fan-out consumer a 20-minute
    liveness window it has no use for.
    """
    return int((longest_wait + 300) * 1000)


class _BaseConsumer:
    """One topic, one group, a bounded-concurrency batch loop."""

    topic: str = ""
    group_id: str = ""
    concurrency: int = 4
    #: How long this consumer's handler may deliberately block. Only the retry
    #: tiers set it; everything else does its work and polls again.
    longest_wait: int = 0

    def __init__(self) -> None:
        self._consumer = None
        self._stopping = asyncio.Event()
        self._sem: Optional[asyncio.Semaphore] = None

    async def run(self) -> None:
        try:
            from aiokafka import AIOKafkaConsumer
        except ImportError:
            _logger.error("aiokafka is not installed; %s will not run",
                          type(self).__name__)
            return

        self._sem = asyncio.Semaphore(self.concurrency)
        self._consumer = AIOKafkaConsumer(
            self.topic,
            bootstrap_servers=_config.kafka_bootstrap_servers,
            group_id=self.group_id,
            client_id=_config.kafka_client_id,
            # Offsets are ours to commit, after the work.
            enable_auto_commit=False,
            # A worker joining a topic that already has messages must take
            # them: these are subjects waiting on data they have authorised.
            auto_offset_reset="earliest",
            max_poll_interval_ms=_max_poll_interval_ms(self.longest_wait),
            # One batch is exactly one wave of the semaphore. A retry tier
            # sleeps out its delay inside the handler, so a batch bigger than
            # the concurrency would run in two waves and could exceed
            # max_poll_interval_ms — the consumer would be declared dead
            # mid-wait and its partitions reassigned, in the middle of holding
            # deliveries that are only waiting for a clock.
            max_poll_records=self.concurrency,
        )
        try:
            await self._consumer.start()
        except Exception as exc:  # noqa: BLE001
            _logger.error("%s could not connect to %s: %s", type(self).__name__,
                          _config.kafka_bootstrap_servers, exc)
            return

        _logger.info("%s consuming %s as %s (concurrency %d)",
                     type(self).__name__, self.topic, self.group_id,
                     self.concurrency)
        try:
            while not self._stopping.is_set():
                batches = await self._consumer.getmany(
                    timeout_ms=1000, max_records=self.concurrency)
                messages = [m for msgs in batches.values() for m in msgs]
                if not messages:
                    continue
                await asyncio.gather(*(self._one(m) for m in messages))
                try:
                    await self._consumer.commit()
                except Exception:  # noqa: BLE001
                    _logger.warning("%s could not commit offsets; the batch may "
                                    "be redelivered (the claim check will drop "
                                    "the duplicates)", type(self).__name__,
                                    exc_info=True)
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001
            _logger.exception("%s loop died", type(self).__name__)
        finally:
            try:
                await self._consumer.stop()
            except Exception:  # noqa: BLE001
                pass
            _logger.info("%s stopped", type(self).__name__)

    async def stop(self) -> None:
        self._stopping.set()

    async def _one(self, message) -> None:
        async with self._sem:
            try:
                event = parse(message.value)
            except ValueError as exc:
                # Unparseable: never worth a retry, and there is no aggregation
                # id to attach it to. Record it and move on.
                _logger.error("%s: dropping malformed message at %s:%s - %s",
                              type(self).__name__, message.topic,
                              message.offset, exc)
                await self._dead_letter("unknown", {"raw_offset": message.offset,
                                                    "topic": message.topic},
                                        "malformed_message: %s" % exc)
                return
            try:
                await self.handle(event)
            except Exception as exc:  # noqa: BLE001
                aggregation_id = event.get("subject") or "unknown"
                _logger.exception("%s: handling %s failed", type(self).__name__,
                                  aggregation_id)
                await _aggregator()._mark_failed(aggregation_id, "internal_error")
                await self._dead_letter(aggregation_id, event.get("data") or {},
                                        "handler_error: %s" % exc)

    async def handle(self, event: Dict[str, Any]) -> None:  # pragma: no cover
        raise NotImplementedError

    @staticmethod
    async def _dead_letter(aggregation_id: str, data: Dict[str, Any],
                           reason: str) -> None:
        payload = dict(data)
        payload["dead_letter_reason"] = reason
        payload["dead_lettered_at"] = datetime.now(timezone.utc).isoformat()
        await bus.publish(_config.topic_dlq, aggregation_id,
                          envelope(TYPE_DEAD_LETTER, aggregation_id, payload))


class FanOutConsumer(_BaseConsumer):
    """Query the registries, then hand the finished envelope to delivery."""

    def __init__(self) -> None:
        super().__init__()
        self.topic = _config.topic_fanout
        self.group_id = _config.kafka_group_fanout
        self.concurrency = _config.kafka_fanout_concurrency

    async def handle(self, event: Dict[str, Any]) -> None:
        aggregation_id = event["subject"]
        service = _aggregator()
        prepared = await service.fetch_stage(aggregation_id)
        if prepared is None:
            return  # another worker has it, or there is nothing to do

        delivery = envelope(TYPE_DELIVERY_REQUESTED, aggregation_id, {
            "aggregation_id": aggregation_id,
            "callback_url": prepared["callback_url"],
            "body": prepared["body"],
            "attempt": 1,
        })
        if await bus.publish(_config.topic_delivery, aggregation_id, delivery):
            return
        # The registries have already answered and the envelope is signed;
        # losing it now because the broker blinked would mean re-querying them
        # to recover. Deliver inline instead, once, and let the reaper or the
        # operator see a row that never reached 'delivered'.
        _logger.warning("Aggregation %s: could not queue the callback, "
                        "delivering inline", aggregation_id)
        await service.deliver_stage(aggregation_id, prepared["callback_url"],
                                    prepared["body"], attempt=1)


class DeliveryConsumer(_BaseConsumer):
    """POST to the partner; on a retriable failure, schedule the next attempt."""

    def __init__(self) -> None:
        super().__init__()
        self.topic = _config.topic_delivery
        self.group_id = _config.kafka_group_delivery
        self.concurrency = _config.kafka_delivery_concurrency

    async def handle(self, event: Dict[str, Any]) -> None:
        aggregation_id = event["subject"]
        data = event["data"]
        attempt = int(data.get("attempt") or 1)
        callback_url = data.get("callback_url")
        body = data.get("body")
        if not callback_url or not isinstance(body, dict):
            await self._dead_letter(aggregation_id, data,
                                    "delivery message has no callback or body")
            await _aggregator().give_up(aggregation_id, "malformed_delivery")
            return

        service = _aggregator()
        if await service.deliver_stage(aggregation_id, callback_url, body,
                                       attempt=attempt):
            return

        backoff: List[int] = _config.retry_backoff
        if attempt >= _config.kafka_delivery_max_attempts:
            await service.give_up(
                aggregation_id,
                "callback_failed after %d attempt(s)" % attempt)
            await self._dead_letter(aggregation_id, data,
                                    "delivery attempts exhausted")
            return

        delay = backoff[min(attempt - 1, len(backoff) - 1)]
        not_before = datetime.now(timezone.utc) + timedelta(seconds=delay)
        retry = envelope(TYPE_DELIVERY_RETRY, aggregation_id, {
            **data,
            "attempt": attempt + 1,
            "not_before": not_before.isoformat(),
            "last_failure": "callback_failed",
        }, event_id=event.get("id"))  # same id: this is the same delivery
        await service.schedule_retry(aggregation_id, not_before)
        # The tier matching this delay, not a shared retry topic: a 10-second
        # hold behind a 900-second one would otherwise wait the full 900.
        tier = _config.retry_tier_for(delay)
        if not await bus.publish(tier, aggregation_id, retry):
            await service.give_up(aggregation_id,
                                  "callback_failed and the retry could not be queued")


class RetryConsumer(_BaseConsumer):
    """One backoff tier. Holds a delivery for exactly this tier's delay.

    Kafka has no delayed delivery, so the wait has to live somewhere, and
    sleeping in the consumer is the simplest place that does not involve
    polling the database. What makes it safe is that a tier holds **only**
    messages with the same delay.

    Get that wrong — one retry topic for every backoff — and the consumer loop
    turns into the bug: it does not fetch the next batch until the current one
    finishes, so a 10-second hold sitting behind a 900-second hold waits the
    full 900 seconds. The row shows `next_retry_at` in the past and nothing
    happening, which looks like the queue has died. It has not; it is asleep on
    the message in front.

    Here the sleep only ever blocks messages that were going to wait the same
    length of time anyway, and because they arrived in order they are due in
    order. See ``Settings.retry_tiers``.
    """

    def __init__(self, delay: int, topic: str, group: str) -> None:
        super().__init__()
        self.delay = delay
        self.topic = topic
        self.group_id = group
        self.concurrency = _config.kafka_delivery_concurrency
        self.longest_wait = delay

    async def handle(self, event: Dict[str, Any]) -> None:
        aggregation_id = event["subject"]
        data = event["data"]
        raw = data.get("not_before")
        try:
            due = datetime.fromisoformat(raw) if raw else datetime.now(timezone.utc)
        except ValueError:
            due = datetime.now(timezone.utc)
        if due.tzinfo is None:
            due = due.replace(tzinfo=timezone.utc)

        wait = (due - datetime.now(timezone.utc)).total_seconds()
        # Capped at this tier's own delay. A clock skew or a hand-edited
        # message must not park a delivery for longer than the tier promises —
        # and must not push the consumer past its liveness window, which is
        # sized from exactly this number.
        wait = max(0.0, min(wait, float(self.delay)))
        if wait:
            _logger.info("Aggregation %s: holding callback attempt %s for %.0fs "
                         "(%ds tier)", aggregation_id, data.get("attempt"),
                         wait, self.delay)
            await asyncio.sleep(wait)

        delivery = envelope(TYPE_DELIVERY_REQUESTED, aggregation_id,
                            {k: v for k, v in data.items() if k != "not_before"},
                            event_id=event.get("id"))
        if not await bus.publish(_config.topic_delivery, aggregation_id, delivery):
            await _aggregator().give_up(
                aggregation_id, "retry could not be re-queued for delivery")


class ConsumerRunner:
    """Owns the three loops as asyncio tasks."""

    def __init__(self) -> None:
        self._consumers: List[_BaseConsumer] = []
        self._tasks: List[asyncio.Task] = []

    async def start(self) -> None:
        if not _config.kafka_enabled:
            return
        # The producer is started first so the topics exist before any consumer
        # subscribes; a consumer on a missing topic is not an error, just a
        # loop that sees nothing until someone else creates it.
        await bus.start()
        self._consumers = [FanOutConsumer(), DeliveryConsumer()]
        # One per backoff step. Four tiers is four more consumers, which is the
        # price of not having a long hold block a short one.
        self._consumers += [RetryConsumer(t["delay"], t["topic"], t["group"])
                            for t in _config.retry_tiers]
        self._tasks = [asyncio.create_task(c.run(), name=type(c).__name__)
                       for c in self._consumers]
        _logger.info("Aggregation consumers started (%d loops)", len(self._tasks))

    async def stop(self) -> None:
        for consumer in self._consumers:
            await consumer.stop()
        for task in self._tasks:
            task.cancel()
        if self._tasks:
            await asyncio.gather(*self._tasks, return_exceptions=True)
        self._tasks, self._consumers = [], []
        # The bus is NOT stopped here. It is shared with whatever else is in
        # this process — in the API that is the request handlers, which publish
        # and must keep working while the consumers wind down. Its owner stops
        # it: Initializer.fastapi_app_shutdown, or worker.main.

    async def wait(self) -> None:
        """Block until the loops end — the standalone worker's main body."""
        if self._tasks:
            await asyncio.gather(*self._tasks, return_exceptions=True)


runner = ConsumerRunner()
