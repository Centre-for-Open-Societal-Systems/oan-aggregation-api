# Run and test locally (Aggregation Layer + Consent Manager)

## What runs where

| Service | URL | Container |
|---|---|---|
| Aggregation Layer | http://localhost:8110 (`/docs`) | `aggregation-layer` (+ `aggregation-db`) |
| Consent Manager (branch `feature/aggregation-layer-apis`) | http://localhost:8000 | `consent-manager-backend-1` |
| CM consent UI | http://localhost:3002 | `consent-manager-frontend-1` |
| Partner callback receiver | http://localhost:9099/all | `agg-callback` |
| Kafka / Kafka UI | localhost:9092 / http://localhost:8085 | `agg-kafka`, `agg-kafka-ui` |

Port 8110, not 8100: `oan-kong` uses 8100 when it is running.

## One-time setup (already done on this machine, 2026-10-07)

```bash
python scripts/register-aggregator.py      # Keycloak client + role, key, PM partner, CM bindings, .env files
bash ../../OPENG2P/coss-run/run-cm.sh       # rebuild the CM from the checked-out branch
docker compose -p aggregation-layer -f deploy/docker-compose.yml up -d --build
docker compose -p aggregation-kafka -f deploy/docker-compose.kafka.yml up -d   # optional (KAFKA_ENABLED=true in deploy/.env)
```

## Test

```bash
python postman/agg-prep.py        # fresh partner consent object + Postman env (valid 5 min)
python scripts/stack-check.py     # automated: consent-held flow + raise-and-approve flow
```

Postman: import `postman/OpenG2P-Aggregator.postman_collection.json` and the environment
`agg-prep.py` just wrote, then run folders 0 → 4. Re-run `agg-prep.py` if a seek returns `replay`.

Manual "farmer never asked" flow: sign a consent object for a subject with no consent; the
seek ack carries `consent_url` (CM UI :3002). Approve there with the OTP; the partner's
on-search arrives at :9099 without another call.

## Switches

- Kafka off: `AGGREGATION_LAYER_KAFKA_ENABLED=false` in `deploy/.env`, then
  `docker compose -p aggregation-layer -f deploy/docker-compose.yml up -d aggregation-layer`.
- Logs: `docker logs -f aggregation-layer`, `docker logs -f consent-manager-backend-1`.
- CM → aggregation layer events: look for `Aggregation Layer event ... delivered` in the CM log.

## Known data gap

The Cropsown register is empty since the 2026-10-06 demo clean-up, so cropsown answers
`ok, records=0`. Approve an intake in the Cropsown UI (:3004) and query its functional id.
