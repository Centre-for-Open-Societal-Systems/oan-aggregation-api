# Kafka in the aggregated fetch

## The problem this solves

When a subject verified their OTP, the aggregator did this:

```python
request.status = "verified"
await self._mint_grants(request)
asyncio.create_task(self._fan_out_and_deliver(request.id))   # ← here
return request
```

`_fan_out_and_deliver` queries **every registry the partner named, one after
another**, then POSTs the result to the partner's callback. All of it inside
the API process, on the same event loop that serves the consent portal.

Three things follow, and all three bite at exactly the moment the system is
being used properly — several subjects authorising at once:

1. **Unbounded concurrency.** Fifty subjects verifying in the same minute means
   fifty concurrent fan-outs. Nothing caps them. Each holds a DB session and
   several outbound HTTP calls, and they compete with the portal's own request
   handling. This is the "portal gets stuck" the change was asked for — and the
   registries feel it first: the Celery/Postgres connection exhaustion already
   recorded against this stack is the same pressure arriving from the other end.
2. **Work dies with the process.** `asyncio.create_task` is not durable. Restart
   a worker — a deploy, an OOM, a `Ctrl-C` — and every fan-out in flight is
   gone. The subject's OTP is spent, the grants are minted, and no data will
   ever arrive.
3. **One callback attempt, ever.** `_deliver` POSTed once. A partner webhook
   that was restarting got `status = failed` and nothing else. There was no
   retry because there was nowhere to keep the work.

## The shape now

```
POST /aggregation/v1/requests/{id}/verify-otp
        │
        │  checks the code, mints the grants, publishes. Returns in
        │  milliseconds — it does not touch a registry.
        ▼
 openg2p.aggregation.fanout ──────▶ FanOutConsumer
                                                 │ queries each registry,
                                                 │ builds + signs the
                                                 │ on-search envelope
                                                 ▼
 openg2p.aggregation.delivery ────▶ DeliveryConsumer ──▶ partner callback
                                                 │ 5xx / timeout        │ 2xx
                                                 ▼                      ▼
 openg2p.aggregation.delivery.retry.10s                    delivered
 openg2p.aggregation.delivery.retry.60s
 openg2p.aggregation.delivery.retry.300s
 openg2p.aggregation.delivery.retry.900s
            │ one RetryConsumer per tier, each holding for ITS delay
            └──────────────▶ back to .delivery
                                                 │ attempts exhausted
                                                 ▼
 openg2p.aggregation.dlq
```

The two stages are separate topics on purpose. They fail for unrelated reasons:
the fan-out fails when a *registry* is down, the delivery fails when the
*partner* is down. Keeping them together would mean a slow partner webhook
holding a worker that should be querying registries, and — worse — a callback
retry re-querying the registries, turning one partner outage into repeated
reads of the subject's data.

### Why the retry topic is four topics

Kafka has no delayed delivery, so the wait has to live somewhere. Sleeping in a
dedicated consumer is the simplest place that does not involve polling the
database — but only if every message on that topic waits the **same** length of
time.

This was originally one retry topic whose consumer slept until each message was
due, and that is a bug. A Kafka consumer is a loop: it does not fetch the next
batch until the current one is done. Put a 300-second hold and a 10-second hold
on the same topic, and the 10-second one waits 300 seconds — because the
consumer is still asleep on the message in front of it. What you see is a row
with `next_retry_at` in the past and nothing happening, which looks exactly like
a dead queue. It is not; it is asleep.

Splitting by duration removes the problem instead of managing it. Every message
on a tier waits the same time, so arrival order **is** due order, and the sleep
only ever blocks messages that were going to wait that long anyway. Each tier
gets its own consumer group, so a busy tier cannot hold up an idle one.

The cost is four consumers instead of one, and a topic per entry in
`kafka_retry_backoff_seconds`. That is the price of not having a long hold block
a short one, and it is worth paying.

The tier arithmetic (one topic per backoff, a delay never rounded down) is
covered by `test/kafka/test_consumer_flow.py`.

### Why these topic names

They follow the platform's Audit Manager, which is OpenG2P's existing Kafka
implementation (`openg2p.audit.events`, `openg2p.audit.dlq`, CloudEvents 1.0
envelopes, `aiokafka`, an idempotent topic-init on startup). Same conventions,
same client, same message envelope — one Kafka skill set covers both services.

### Keying and ordering

Every message is keyed by the **aggregation id**. Work spreads evenly across
partitions, which is the point — subjects verifying simultaneously must land on
different workers. A retry keys to the same partition as its original, so the
attempts of one request stay ordered relative to each other.

Keying by partner would put a busy partner's entire traffic on one partition
and let it block everyone sharing it.

## What bounds the load now

| | before | now |
|---|---|---|
| concurrent fan-outs | unbounded | `kafka_fanout_concurrency` × consumers, capped by partitions |
| survives a restart | no | yes — the message is on the broker |
| callback attempts | 1 | `kafka_delivery_max_attempts` (5) with backoff, then DLQ |
| where it runs | the API event loop | a worker process, if you want it there |

The ceiling on registry load is now a number you can state: consumers ×
`kafka_fanout_concurrency`, never more than `kafka_topic_partitions`. That is
the thing the old code never had.

## Measured

40 subjects verifying their OTP in the same instant, against a three-registry
development stack. Measured while the aggregation still ran inside the Consent
Manager (the queue design is unchanged since); the load scripts were not
carried over to this repository.

| | Kafka off | Kafka on, in-app | Kafka on, worker |
|---|---|---|---|
| verify-otp median | 1353 ms | 2158 ms | **1761 ms** |
| verify-otp **max** | **7600 ms** | 2587 ms | **2003 ms** |
| portal GET median | **313 ms** | 25 ms | **12 ms** |
| portal GET max | 1240 ms | 1061 ms | **419 ms** |
| burst returned in | 7693 ms | 2745 ms | **2085 ms** |
| all 40 delivered in | 7.5 s | 18.0 s | 14.2 s |

The "portal GET" row is the symptom this work was asked for. It is an unrelated
read (the catalog discovery endpoint, which touches no registry) fired
repeatedly *while* the fan-outs run, so any latency on it is pure contention.
Off, it degrades 26× — 12 ms idle to 313 ms median. On, with the consumers in
their own process, it does not move at all.

The trade is visible in the last row: **everything finishes slower**, 14 s
instead of 7.5 s. That is the fan-out concurrency cap doing its job. Off, all
40 fan-outs start at once — 120 concurrent registry calls, which is how the
Celery/Postgres connection exhaustion on this stack gets provoked. On, at most
`kafka_fanout_concurrency` run per worker. Slower in total, bounded throughout,
and nobody waiting on the portal can tell it is happening.

Two things to read carefully:

- **At small N there is no win.** At 12 concurrent the numbers are *better*
  without Kafka (560 ms median verify vs 944 ms) — the publish round-trip is
  pure overhead when the registries are keeping up. The queue earns its place
  at load, and for durability and retry, not for latency.
- **`consumers_in_app=true` gives up about half the benefit.** The API process
  is still doing the fan-out; it is simply bounded now. The worker column is
  the shape to deploy.

## Verified

| | |
|---|---|
| `test/kafka/test_consumer_flow.py` | the handlers with a stubbed bus — retry arithmetic, stable message id, give-up at the last attempt, malformed → DLQ, one topic per backoff tier. No broker needed. |
| `test/e2e` | the whole flow against a real Consent Manager and Postgres, with the in-process path (`kafka_enabled=false`), which walks the same statuses. |

## Duplicates, and why the same fetch never runs twice

Kafka delivers **at least once**. The same fan-out message can legitimately
arrive twice: on a consumer rebalance, or after a worker died between doing the
work and committing its offset. Re-querying three registries and POSTing the
subject's data to the partner a second time is not an acceptable response.

So every stage begins by trying to move the row out of the status it expects,
as a conditional `UPDATE`:

```sql
UPDATE aggregation_requests SET status='fetching', claimed_by=..., claimed_at=now()
 WHERE id = :id AND status IN ('queued','verified')
```

Exactly one caller can win. A loser updates zero rows, logs, and drops its copy
of the message. No new infrastructure, no distributed lock — the row's own
status is the lock, which is also why the status is worth reading.

New statuses: `queued` → `fetching` → `fetched` → `delivering` → `delivered`.
They are walked identically with and without Kafka, so a row's history reads
the same either way.

Offsets are committed after the batch **whether or not the work succeeded**.
Failure is expressed in the database and on the dead-letter topic, not by
replaying the offset. Replaying is the usual way to retry, but a message that
fails deterministically would be redelivered forever, and each redelivery
re-queries live registries for real personal data. A poison message here is not
just noise, it is a repeated disclosure attempt.

What that gives up is automatic recovery from a worker that dies *after* the
work and *before* the commit. That case is covered instead by:

```bash
python -m openg2p_aggregation_layer.reap
```

which releases rows claimed longer ago than `kafka_claim_timeout_sec` and
republishes them. Run it from a CronJob, like `expire.py`.

## Two things to be deliberate about

**The delivery topic carries personal data.** The aggregated record travels in
the message so that a callback retry never re-queries a registry. That is the
right trade — but it means the subject's data sits in a Kafka topic for as long
as the topic keeps it. The retention is therefore set on the topic at creation
(`kafka_delivery_retention_ms`, one hour by default; the DLQ gets seven days)
rather than inherited from a broker default. Nothing new lands in Postgres:
`registry_results` still holds counts and field names, never values.

**Delivery is at-least-once, so a partner can see the same result twice.** The
on-search `message_id` is derived deterministically from the correlation id, so
every attempt at one delivery carries the same id and a partner can de-duplicate
on it. The envelope is also signed once and re-sent byte-identical — re-signing
would produce a different signature for the same facts.

## Running it

```bash
# 1. the broker (its own compose file)
docker compose -p aggregation-kafka -f deploy/docker-compose.kafka.yml up -d
#    Kafka on localhost:9092, Kafka UI on http://localhost:8085

# 2. turn it on - deploy/.env:
#      AGGREGATION_LAYER_KAFKA_ENABLED=true
#      AGGREGATION_LAYER_KAFKA_BOOTSTRAP_SERVERS=agg-kafka:9094
docker compose -p aggregation-layer -f deploy/docker-compose.yml up -d aggregation-layer
```

The four topics are created on startup if they are missing, the same job the
Audit Manager's `topicInit` Helm hook does. `KAFKA_CFG_AUTO_CREATE_TOPICS_ENABLE`
is off in the compose file on purpose: a typo in a topic name should fail
loudly, not silently start producing to a new topic nobody consumes.

### Splitting the worker off (the production shape)

Running the consumers inside the API process is the single-process dev default
and matches how the Audit Manager runs producer and consumer in one service.
But the point of moving the fan-out off the request path is lost if the registry
queries still share an event loop with the portal. So:

```bash
# deploy/.env:  AGGREGATION_LAYER_KAFKA_CONSUMERS_IN_APP=false
#               AGGREGATION_LAYER_CM_POLL_IN_APP=false
# API (publishes only), then the consumers + CM poll, in another process or ×N:
python -m openg2p_aggregation_layer.worker
```

Both processes joining the same consumer group is not wrong, it just puts the
load back where it was.

### On Kubernetes, do not port this compose file

`docker-compose.kafka.yml` is a **local development broker** and nothing more —
one node, no volume, no auth, replication factor 1. It exists so this service
can be run and demonstrated on a laptop without asking anyone to stand up a
cluster first.

OpenG2P already ships what a real deployment needs, so none of it has to be
written again (checked against the org on 2026-09-23):

| Where | What |
|---|---|
| `OpenG2P/openg2p-helm` | `third-party/kafka-29.3.14.tgz` — the packaged broker chart |
| `OpenG2P/commons` | `charts/openg2p-commons-base/templates/kafka-ui/` — configmap, deployment, service, gateway, virtualservice |
| `OpenG2P/audit-manager` | the reference for a service that owns topics: `aiokafka`, CloudEvents, a `topicInit` Job, a DLQ |

So the Kubernetes path is: deploy the broker from `openg2p-helm`, take the
Kafka UI from `commons` (it is already an Istio-fronted template, unlike the
`provectuslabs/kafka-ui` container here), and point this service at it with
three environment variables:

```yaml
AGGREGATION_LAYER_KAFKA_ENABLED: "true"
AGGREGATION_LAYER_KAFKA_BOOTSTRAP_SERVERS: "kafka:9092"
AGGREGATION_LAYER_KAFKA_CONSUMERS_IN_APP: "false"   # + a worker Deployment
```

Two things to change from the defaults when you do:

- **`kafka_topic_replication`** — 1 is the only value a single-node dev broker
  can serve, and the only value that guarantees data loss on a real one. Set it
  to 3 and size `kafka_topic_partitions` for the number of workers you intend.
- **Topic creation.** This service creates its own topics at startup, which is
  convenient locally and may be exactly what a cluster forbids. If client-side
  creation is disabled, set `kafka_topic_init=false` and create them from a
  `topicInit` Job instead — the Audit Manager's chart is the pattern, and the
  names come from `Settings.retry_tiers` (remember there is one retry topic
  **per backoff step**, not one).

The worker becomes its own Deployment running
`python -m openg2p_aggregation_layer.worker`, and `-m openg2p_aggregation_layer.reap`
becomes a CronJob alongside the existing consent-expiry one.

> For context on why none of this could be inherited: the upstream
> `OpenG2P/consent-manager` has **no Kafka at all** (154 files, zero matches),
> and neither does any repository in the
> `Centre-for-Open-Societal-Systems` org (31 repos, all clean — including
> `oan_registry_cdc`, which despite its name contains only a LICENSE file).
> Within OpenG2P, Kafka exists in `audit-manager` (live) and
> `openg2p-reporting` (Debezium → Kafka → OpenSearch, **archived 2026-07-14**).
> The queue in this document is new work; what it borrows from Audit Manager is
> convention, not code.

## If the broker is down

Nothing fails closed on the subject. By the time `verify_otp` publishes, the
OTP is spent and the grants are minted — the authorisation cannot be reissued,
so refusing the request would lose it. `bus.publish` returns `False`, the row
goes back to `verified`, and the old in-process task runs instead. Slower and
unbounded, exactly as before, and the log says which mode served the request:

```
Aggregation <id>: Kafka unavailable, running the fan-out in-process
```

`kafka_enabled=false` is the same path, chosen deliberately. That is how a
deployment without a broker runs unchanged.

## Watching it work

- **Kafka UI** — <http://localhost:8085>, the four topics and their lag.
- **The status endpoint** — `GET /aggregation/v1/requests/{id}` returns
  `fetch_attempts` and `next_retry_at`. A request sitting in `fetched` with a
  `next_retry_at` is backing off, not stuck; `fetch_attempts > 1` means a worker
  died mid-fetch and the registries were queried more than once.
- **The DLQ** — `openg2p.aggregation.dlq` holds everything that gave up,
  with a `dead_letter_reason`.
