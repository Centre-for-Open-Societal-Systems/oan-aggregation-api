# Aggregation Layer

One partner call for data from several registries (Farmer, Livestock, Cropsown), gated on the
farmer's consent and OTP, delivered as a single DCI `on-search` callback.

Split out of `Centre-for-Open-Societal-Systems/consent-management` (commit `260bb0e`), where it
ran inside the Consent Manager. See [docs/SPLIT-PLAN.md](docs/SPLIT-PLAN.md).

## How it relates to the Consent Manager

The Consent Manager (CM) keeps every consent decision: validate, consent requests,
approve / withdraw, lawful basis, My consents. It carries no code for this service. This
service owns the aggregation and all of its state, in its own database: the request row,
the per-registry grant (`aggregation_grants`), the OTP for the release, the registry
fan-out, the callback and its retries.

It talks to the CM **only over the CM's generic HTTP APIs**
(`backend/src/openg2p_aggregation_layer/services/cm_client.py`) and **polls** them for
consent approval / denial / withdrawal; nothing is pushed by the CM. The registry hops are
validated by the CM against bindings on lawful basis `legitimate_interest`; the farmer's
consent and OTP are enforced here before any registry is called.
See [docs/CM-API-CONTRACT.md](docs/CM-API-CONTRACT.md).

## API

| Route | Who |
|---|---|
| `POST /dci/registry/async/search` | Partner (consent JWS inside). Returns an ack; data comes on the callback. |
| `POST /consent/v1/aggregation/{id}/verify-otp` | Subject's OTP |
| `POST /consent/v1/farmer-consent-validate` | Same, farmer token, subject checked |
| `GET /consent/v1/aggregation/{id}` | Status (subject's own) |
| `GET /consent/v1/aggregation/fields` | Field catalog |
| `GET /consent/v1/aggregation/queue` | Queue health |

Routes are unchanged from the CM version, so the Postman collection only needs `agg_url`.

## Layout

```
backend/   FastAPI service + worker (Kafka consumers + CM poll,
           python -m openg2p_aggregation_layer.worker) and reaper
           (CM poll + stale claims, python -m openg2p_aggregation_layer.reap)
deploy/    Dockerfile, docker-compose (service + own Postgres), Kafka compose, .env.example
postman/   Collection + environment for the end-to-end flow, callback receiver
scripts/   register-aggregator.py (PM partner + CM bindings), queue-status.sh
docs/      Split plan, CM API contract, Kafka design
test/      OTP + Kafka consumer unit tests, end-to-end run against a real CM (test/e2e)
```

## Run

See [docs/RUN-LOCAL.md](docs/RUN-LOCAL.md). In short:

```bash
python scripts/register-aggregator.py   # Keycloak client, key, PM partner, CM bindings, .env
docker compose -p aggregation-layer -f deploy/docker-compose.yml up -d --build
curl http://localhost:8110/ping
python postman/agg-prep.py && python scripts/stack-check.py
```

## Status

- [x] Code moved, CM table access replaced by `cm_client.py`, own DB, own signing key
- [x] App boots and mounts all routes; Kafka worker / reaper / consumers load
- [x] OTP validation: `test/otp/test_otp_flow.py` (Fayda + internal providers) passes
- [x] Generic CM APIs only (no CM change), own `aggregation_grants`, consent state polled
      (branch `feature/standalone-db`)
- [x] End-to-end against CM `develop` without its aggregator
      (consent-management `feature/remove-aggregator`):
      `CM_SRC=../consent-management bash test/e2e/run.sh` — 18/18
      (no aggregation API on the CM, consent held + OTP, raise + approve, withdraw, deny,
      approval after the replay window)
- [x] `scripts/register-aggregator.py`: own key `PARTNER_AGGREGATION_LAYER`, Keycloak client,
      CM bindings on `legitimate_interest`
- [ ] Re-run `scripts/register-aggregator.py` + `scripts/stack-check.py` on the real stack
- [ ] Manual Postman run
