"""Drive the three consumer handlers with a stubbed service and bus.

    ~/agg-venv/bin/python test/kafka/test_consumer_flow.py

No broker and no database, on purpose: this is the part of the queue that is
pure arithmetic and wiring — which attempt number goes where, whether a retry
keeps the delivery's message id, whether the last attempt gives up instead of
looping — and none of it needs infrastructure to be wrong. Run it before
standing anything up; the end-to-end flow is `./demo-check.sh` plus a seek.
"""
import asyncio
from datetime import datetime, timezone

from openg2p_aggregation_layer.config import Settings
cfg = Settings.get_config()

import openg2p_aggregation_layer.kafka_bus.consumers as C
from openg2p_aggregation_layer.kafka_bus.topics import envelope

published = []          # (topic, aggregation_id, event)
calls = []              # what the "service" was asked to do

class FakeBus:
    enabled = True
    async def publish(self, topic, agg_id, event, headers=None):
        published.append((topic, agg_id, event))
        return True
    async def start(self): return True
    async def stop(self): pass

class FakeService:
    def __init__(self, deliver_ok):
        self.deliver_ok = deliver_ok
    async def fetch_stage(self, agg_id):
        calls.append(("fetch", agg_id))
        return {"callback_url": "http://partner/cb",
                "body": {"header": {"message_id": "m1"}, "message": {}},
                "results": {"farmer": {"status": "ok"}}}
    async def deliver_stage(self, agg_id, url, body, attempt):
        calls.append(("deliver", agg_id, attempt))
        return self.deliver_ok
    async def schedule_retry(self, agg_id, when):
        calls.append(("schedule_retry", agg_id, when))
    async def give_up(self, agg_id, reason):
        calls.append(("give_up", agg_id, reason))
    async def _mark_failed(self, agg_id, reason):
        calls.append(("mark_failed", agg_id, reason))

fake = {"svc": None}
C.bus = FakeBus()
C._aggregator = lambda: fake["svc"]

def reset(deliver_ok):
    published.clear(); calls.clear()
    fake["svc"] = FakeService(deliver_ok)

async def main():
    ok = True

    # 1. fan-out publishes a delivery message
    reset(True)
    await C.FanOutConsumer().handle(envelope(C.TYPE_DELIVERY_REQUESTED, "agg-1", {}))
    topic, _, ev = published[0]
    assert topic == cfg.topic_delivery, topic
    assert ev["data"]["attempt"] == 1
    assert ev["data"]["callback_url"] == "http://partner/cb"
    print("1 fan-out      -> published to", topic, "attempt", ev["data"]["attempt"])

    # 2. a successful delivery publishes nothing further
    reset(True)
    await C.DeliveryConsumer().handle(envelope(
        C.TYPE_DELIVERY_REQUESTED, "agg-2",
        {"callback_url": "http://p/cb", "body": {"x": 1}, "attempt": 1}))
    assert published == [], published
    print("2 delivery ok  -> nothing requeued, calls:", [c[0] for c in calls])

    # 3. a failed delivery schedules the next attempt with the first backoff
    reset(False)
    dc = C.DeliveryConsumer()
    src = envelope(C.TYPE_DELIVERY_REQUESTED, "agg-3",
                   {"callback_url": "http://p/cb", "body": {"x": 1}, "attempt": 1})
    await dc.handle(src)
    topic, _, ev = published[0]
    assert topic == cfg.retry_tier_for(cfg.retry_backoff[0]), topic
    assert topic.endswith("%ds" % cfg.retry_backoff[0]), topic
    assert ev["data"]["attempt"] == 2, ev["data"]["attempt"]
    assert ev["id"] == src["id"], "retry must keep the delivery's id"
    due = datetime.fromisoformat(ev["data"]["not_before"])
    delay = (due - datetime.now(timezone.utc)).total_seconds()
    assert cfg.retry_backoff[0] - 2 <= delay <= cfg.retry_backoff[0] + 2, delay
    print("3 delivery fail-> %s, attempt 2, in %.0fs, same id %s"
          % (topic.rsplit(".", 1)[-1] + " tier", delay, ev["id"][:8]))

    # 4. the last attempt gives up and dead-letters instead of looping
    reset(False)
    last = cfg.kafka_delivery_max_attempts
    await C.DeliveryConsumer().handle(envelope(
        C.TYPE_DELIVERY_REQUESTED, "agg-4",
        {"callback_url": "http://p/cb", "body": {"x": 1}, "attempt": last}))
    assert any(c[0] == "give_up" for c in calls), calls
    assert published[0][0] == cfg.topic_dlq, published[0][0]
    print("4 attempt %d/%d -> give_up + %s" % (last, last, cfg.topic_dlq))

    # 5. the retry consumer re-queues once the hold has elapsed
    reset(True)
    past = datetime.now(timezone.utc).isoformat()
    ev_in = envelope(C.TYPE_DELIVERY_RETRY, "agg-5",
                     {"callback_url": "http://p/cb", "body": {"x": 1},
                      "attempt": 3, "not_before": past})
    tier = cfg.retry_tiers[0]
    await C.RetryConsumer(tier["delay"], tier["topic"], tier["group"]).handle(ev_in)
    topic, _, ev = published[0]
    assert topic == cfg.topic_delivery, topic
    assert "not_before" not in ev["data"]
    assert ev["data"]["attempt"] == 3
    assert ev["id"] == ev_in["id"]
    print("5 retry due    -> back to", topic, "attempt", ev["data"]["attempt"])

    # 6. a malformed delivery message is dead-lettered, not retried
    reset(True)
    await C.DeliveryConsumer().handle(envelope(
        C.TYPE_DELIVERY_REQUESTED, "agg-6", {"attempt": 1}))
    assert published[0][0] == cfg.topic_dlq
    assert any(c[0] == "give_up" for c in calls)
    print("6 malformed    -> dlq + give_up, never retried")

    # 6b. every backoff has its own tier, and each one is distinct
    tiers = [t["topic"] for t in cfg.retry_tiers]
    assert len(set(tiers)) == len(cfg.retry_backoff), tiers
    for delay in cfg.retry_backoff:
        got = cfg.retry_tier_for(delay)
        assert got.endswith("%ds" % delay), (delay, got)
    # The bug this replaced: a 10s hold queued behind a 900s hold on one topic
    # waits the full 900s, because the consumer loop is asleep on the message
    # in front of it.
    assert cfg.retry_tier_for(cfg.retry_backoff[0]) != cfg.retry_tier_for(
        cfg.retry_backoff[-1]), "short and long backoffs share a topic"
    print("6 tiers        ->", ", ".join(t.rsplit(".", 1)[-1] for t in tiers),
          "(separate topics, so a long hold cannot block a short one)")

    # 7. backoff schedule as an operator would read it
    print("7 schedule     -> attempts 1..%d, gaps %s, then dlq"
          % (cfg.kafka_delivery_max_attempts, cfg.retry_backoff))
    print("\nALL CHECKS PASSED")

asyncio.run(main())
