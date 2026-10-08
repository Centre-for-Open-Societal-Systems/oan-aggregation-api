# Aggregation Layer

One partner call for data from several registries (Farmer, Livestock, Cropsown), gated on the
farmer's consent and OTP, delivered as a single DCI `on-search` callback.

Split out of `Centre-for-Open-Societal-Systems/consent-management` (commit `260bb0e`), where it
ran inside the Consent Manager. See [docs/SPLIT-PLAN.md](docs/SPLIT-PLAN.md).

## How it relates to the Consent Manager

The Consent Manager (CM) keeps every consent decision: validate, consent requests,
approve / withdraw, lawful basis, My consents. This service owns only the aggregation:
the request row, the OTP for the release, the registry fan-out, the callback and its retries.

It talks to the CM **only over HTTP** (`backend/src/openg2p_aggregation_layer/services/cm_client.py`)
and receives consent events on `POST /aggregation/v1/cm-events`.
The CM APIs it needs are listed in [docs/CM-API-CONTRACT.md](docs/CM-API-CONTRACT.md).

## API

| Route | Who |
|---|---|
| `POST /dci/registry/async/search` | Partner (consent JWS inside). Returns an ack; data comes on the callback. |
| `POST /consent/v1/aggregation/{id}/verify-otp` | Subject's OTP |
| `POST /consent/v1/farmer-consent-validate` | Same, farmer token, subject checked |
| `GET /consent/v1/aggregation/{id}` | Status (subject's own) |
| `GET /consent/v1/aggregation/fields` | Field catalog |
| `GET /consent/v1/aggregation/queue` | Queue health |
| `POST /aggregation/v1/cm-events` | CM → consent approved / withdrawn |

Routes are unchanged from the CM version, so the Postman collection only needs `agg_url`.

## Layout

```
backend/   FastAPI service + Kafka worker (python -m openg2p_aggregation_layer.worker)
           and reaper (python -m openg2p_aggregation_layer.reap)
deploy/    Dockerfile, docker-compose (service + own Postgres), Kafka compose, .env.example
postman/   Collection + environment for the end-to-end flow, callback receiver
scripts/   register-aggregator.py (PM partner + CM bindings), queue-status.sh
docs/      Split plan, CM API contract, Kafka design
test/      Kafka consumer flow test
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
- [x] CM side: the 5 additions in `docs/CM-API-CONTRACT.md` (consent-management branch `feature/aggregation-layer-apis`)
- [x] End-to-end against the CM over HTTP: `CM_SRC=../consent-management bash test/e2e/run.sh` — 19/19
      (validate, by-audience, grants, granted-scopes, approve/withdraw events, OTP, My consents grouping)
- [x] `scripts/register-aggregator.py`: own key `PARTNER_AGGREGATION_LAYER`, Keycloak client, CM bindings
- [x] Real local stack (farmer, livestock, cropsown registries), in-process and Kafka mode:
      `scripts/stack-check.py` 11/11
- [ ] Manual Postman run
- [ ] Then remove the aggregator code from consent-management (separate PR)
