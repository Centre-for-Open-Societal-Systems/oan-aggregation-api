"""The producer side: one shared, lazily started Kafka client.

Why a module-level singleton rather than a BaseService component: the producer
holds a TCP connection pool and a background sender task, and it has to be
reachable from both the API process and the standalone worker without either
of them constructing the other's service graph.

Failure policy
--------------
Publishing runs inside the HTTP request that verified the subject's OTP. By
that point the code has been consumed, the attempt counter has moved and the
grants have been minted — the authorisation is spent and cannot be reissued.
So a broker that is down must never turn into a 5xx: ``publish`` returns False
and the caller falls back to the in-process task it used before Kafka existed.
Slower and unbounded, but the request is not lost, and the log says plainly
which mode served it.
"""
import asyncio
import logging
from typing import Any, Dict, Optional

from ..config import Settings
from .topics import key_for, serialize

_config = Settings.get_config()
_logger = logging.getLogger(_config.logging_default_logger_name)


class EventBus:
    """Lazily started AIOKafkaProducer with a safe "not available" state."""

    def __init__(self) -> None:
        self._producer = None
        self._lock = asyncio.Lock()
        self._started = False
        self._topics_ready = False

    # ── lifecycle ───────────────────────────────────────────────────────────

    @property
    def enabled(self) -> bool:
        return bool(_config.kafka_enabled)

    @property
    def connected(self) -> bool:
        """Is there a live producer right now?

        Deliberately does not probe the broker: this is read by a status
        endpoint, and a health check that opens a connection is a health check
        that can hang. False here with ``enabled`` true means either nothing
        has published yet or the last publish failed and dropped the client.
        """
        return bool(self._started and self._producer is not None)

    async def start(self) -> bool:
        """Connect, creating the topics first if configured to.

        Idempotent and safe to call from several coroutines at once. Returns
        False when Kafka is disabled or unreachable; it never raises, because
        every caller's fallback is to carry on without the broker.
        """
        if not self.enabled:
            return False
        if self._started:
            return True
        async with self._lock:
            if self._started:
                return True
            # A producer left over from a failed send: publish() drops the
            # started flag so the next call reconnects, but the old client
            # still holds a sender task and a socket pool. Close it before
            # building another, or a flapping broker leaks one per attempt.
            if self._producer is not None:
                try:
                    await self._producer.stop()
                except Exception:  # noqa: BLE001
                    pass
                self._producer = None
            try:
                from aiokafka import AIOKafkaProducer
            except ImportError:
                _logger.error(
                    "aggregation_layer_kafka_enabled=true but aiokafka is not "
                    "installed; falling back to in-process fan-out. "
                    "pip install 'aiokafka>=0.10'")
                return False

            if _config.kafka_topic_init and not self._topics_ready:
                await self.ensure_topics()

            producer = AIOKafkaProducer(
                bootstrap_servers=_config.kafka_bootstrap_servers,
                client_id=_config.kafka_client_id,
                # Durability over latency: a message lost here is a subject's
                # authorisation lost with it. Idempotence makes the producer's
                # own retries safe, so a broker hiccup cannot duplicate a
                # fan-out and query every registry twice.
                acks="all",
                enable_idempotence=True,
                compression_type="gzip",
                linger_ms=5,
                request_timeout_ms=int(_config.kafka_producer_timeout * 1000),
            )
            try:
                await asyncio.wait_for(producer.start(),
                                       timeout=_config.kafka_producer_timeout)
            except Exception as exc:  # noqa: BLE001
                _logger.error("Kafka producer could not start against %s: %s",
                              _config.kafka_bootstrap_servers, exc)
                try:
                    await producer.stop()
                except Exception:  # noqa: BLE001
                    pass
                return False
            self._producer = producer
            self._started = True
            _logger.info("Kafka producer connected to %s (fanout=%s delivery=%s "
                         "retry=%s dlq=%s)",
                         _config.kafka_bootstrap_servers, _config.topic_fanout,
                         _config.topic_delivery,
                         ", ".join(t["topic"] for t in _config.retry_tiers),
                         _config.topic_dlq)
            return True

    async def stop(self) -> None:
        if self._producer is not None:
            try:
                await self._producer.stop()
            except Exception:  # noqa: BLE001
                _logger.warning("Kafka producer did not stop cleanly", exc_info=True)
        self._producer = None
        self._started = False

    # ── topics ──────────────────────────────────────────────────────────────

    async def ensure_topics(self) -> None:
        """Create the four topics if they are missing.

        The same job the Audit Manager's ``topicInit`` Helm hook does, run from
        the client so the compose stack needs no extra container. Existing
        topics are left exactly as they are — this never shrinks or reconfigures
        one, because an operator who widened the partition count meant it.
        """
        try:
            from aiokafka.admin import AIOKafkaAdminClient, NewTopic
        except ImportError:
            _logger.warning("aiokafka.admin unavailable; skipping topic creation")
            self._topics_ready = True
            return

        admin = AIOKafkaAdminClient(
            bootstrap_servers=_config.kafka_bootstrap_servers,
            client_id=_config.kafka_client_id + "-admin")
        try:
            await asyncio.wait_for(admin.start(),
                                   timeout=_config.kafka_producer_timeout)
        except Exception as exc:  # noqa: BLE001
            _logger.error("Kafka admin could not connect to %s: %s; assuming the "
                          "topics already exist", _config.kafka_bootstrap_servers, exc)
            return

        # The delivery and dead-letter topics hold the aggregated record, i.e.
        # personal data. Their retention is part of the topic definition rather
        # than a broker default, so the window it is readable in is explicit.
        wanted = [
            NewTopic(name=_config.topic_fanout,
                     num_partitions=_config.kafka_topic_partitions,
                     replication_factor=_config.kafka_topic_replication),
            NewTopic(name=_config.topic_delivery,
                     num_partitions=_config.kafka_topic_partitions,
                     replication_factor=_config.kafka_topic_replication,
                     topic_configs={"retention.ms": str(_config.kafka_delivery_retention_ms),
                                    "cleanup.policy": "delete"}),
            # One retry topic per backoff step — see Settings.retry_tiers for
            # why a single one cannot work. Their retention has to clear the
            # tier's own delay, or a message could be deleted while it waits.
            *[NewTopic(name=tier["topic"],
                       num_partitions=_config.kafka_topic_partitions,
                       replication_factor=_config.kafka_topic_replication,
                       topic_configs={
                           "retention.ms": str(max(
                               _config.kafka_delivery_retention_ms,
                               (tier["delay"] + 600) * 1000)),
                           "cleanup.policy": "delete"})
              for tier in _config.retry_tiers],
            NewTopic(name=_config.topic_dlq,
                     num_partitions=max(1, _config.kafka_topic_partitions // 4),
                     replication_factor=_config.kafka_topic_replication,
                     topic_configs={"retention.ms": str(_config.kafka_dlq_retention_ms),
                                    "cleanup.policy": "delete"}),
        ]
        try:
            await admin.create_topics(wanted)
            _logger.info("Kafka topics ensured: %s",
                         ", ".join(t.name for t in wanted))
        except Exception as exc:  # noqa: BLE001
            # TopicAlreadyExistsError is the overwhelmingly common case and is
            # not a problem; anything else is logged and tolerated, because the
            # broker may simply forbid client-side creation.
            _logger.info("Kafka topic creation returned %s: %s",
                         type(exc).__name__, exc)
        finally:
            try:
                await admin.close()
            except Exception:  # noqa: BLE001
                pass
        self._topics_ready = True

    # ── publishing ──────────────────────────────────────────────────────────

    async def publish(self, topic: str, aggregation_id: str,
                      event: Dict[str, Any],
                      *, headers: Optional[Dict[str, str]] = None) -> bool:
        """Send one event. True if the broker acknowledged it.

        Waits for the ack rather than fire-and-forget: the caller is about to
        stop tracking this request in memory, and "handed to Kafka" has to mean
        the broker has it, not that it sits in a local buffer that a restart
        would drop.
        """
        if not await self.start():
            return False
        kafka_headers = [(k, str(v).encode("utf-8"))
                         for k, v in (headers or {}).items()]
        try:
            await asyncio.wait_for(
                self._producer.send_and_wait(
                    topic, value=serialize(event), key=key_for(aggregation_id),
                    headers=kafka_headers or None),
                timeout=_config.kafka_producer_timeout)
        except Exception as exc:  # noqa: BLE001
            _logger.error("Aggregation %s: publish to %s failed (%s); the caller "
                          "will fall back", aggregation_id, topic, exc)
            # A producer that failed mid-flight may be unusable; drop it so the
            # next publish reconnects instead of failing the same way forever.
            self._started = False
            return False
        _logger.debug("Aggregation %s: published %s to %s",
                      aggregation_id, event.get("type"), topic)
        return True


#: Shared instance. The API process and the worker both use this one.
bus = EventBus()
