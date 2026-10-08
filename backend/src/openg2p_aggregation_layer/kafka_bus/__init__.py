"""Kafka transport for the aggregated fetch.

The aggregator's work is two long, failure-prone hops — querying every registry
the partner asked about, then POSTing the result to the partner's callback —
triggered by an HTTP request the subject is waiting on. Doing them inline is
what makes the portal stall when several subjects verify an OTP at once, and
what makes a single unreachable callback lose the data for good.

This package is the queue between them:

    verify_otp ──publish──▶ aggregation.fanout ──▶ FanOutConsumer
                                                       │ registries queried
                                                       ▼
                            aggregation.delivery ──▶ DeliveryConsumer ──▶ callback
                                       ▲                   │ 5xx / timeout
                                       └── RetryConsumer ◀─┘
                                     aggregation.delivery.retry
                                                           │ attempts exhausted
                                                           ▼
                                                   aggregation.dlq

Nothing here decides *whether* data may move — consent, the policy ceiling and
the OTP are all settled before the first message is published. This only
decides when the work runs and how often it is retried.

Import cost is deferred: ``aiokafka`` is imported inside the functions that
need it, so a deployment with ``kafka_enabled=false`` neither needs the
dependency installed nor pays for it at startup.
"""
from .bus import EventBus, bus
from .topics import (
    TYPE_DELIVERY_REQUESTED,
    TYPE_FANOUT_REQUESTED,
    envelope,
    parse,
)

__all__ = [
    "EventBus",
    "bus",
    "envelope",
    "parse",
    "TYPE_FANOUT_REQUESTED",
    "TYPE_DELIVERY_REQUESTED",
]
