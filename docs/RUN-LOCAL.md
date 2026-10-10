# Run and test locally (Aggregation Layer + Consent Manager)

## What runs where

| Service | URL | Container |
|---|---|---|
| Aggregation Layer | http://localhost:8110 (`/docs`) | `aggregation-layer` (+ `aggregation-db`) |
| Consent Manager (`develop`, no aggregation code, `subject_consent_required=true`) | http://localhost:8000 | from the consent-management repository |
| CM consent UI | http://localhost:3002 | from the consent-management repository |
| Partner callback receiver (dev) | http://localhost:9099/all | `agg-callback` |
| Kafka / Kafka UI (optional) | localhost:9092 / http://localhost:8085 | `agg-kafka`, `agg-kafka-ui` |

The service publishes on host port 8110 so it does not collide with an API gateway on 8100.
The registries, Partner Management and Keycloak are separate deployments on the shared
Docker network (`OPENG2P_NETWORK`, see `deploy/docker-compose.yml`).

## One-time setup

```bash
# 1. the registry catalog: one entry per registry this service may query
$EDITOR deploy/registries.yaml

# 2. Keycloak client + role, signing key, PM partner, one CM binding per catalog
#    registry (legitimate_interest), deploy/.env. --partner extends an existing
#    partner binding so it may consent to the catalog's scope ids.
python scripts/register-aggregator.py --partner <partner-audience>

# 3. local-only switches in deploy/.env: AGGREGATION_LAYER_OTP_DEBUG_ENABLED=true
#    (no SMS gateway locally; the OTP is readable from the API)

docker compose -p aggregation-layer -f deploy/docker-compose.yml up -d --build
docker compose -p aggregation-kafka -f deploy/docker-compose.kafka.yml up -d   # optional (KAFKA_ENABLED=true)
```

The scripts need a virtualenv with `httpx`, `pyjwt`, `cryptography`, `pydantic` and `pyyaml`
(`pip install -e backend` brings them all).

## Test

```bash
# fresh partner consent object + local Postman environment (valid 5 min)
G2P_PARTNER_AUDIENCE=<partner-audience> G2P_PARTNER_PM_ID=<pm-partner-id> \
G2P_SUBJECT_USER=<beneficiary login> G2P_SUBJECT_PASSWORD=<password> \
  python postman/agg-prep.py

python scripts/stack-check.py     # consent-held flow + raise-and-approve flow, per catalog registry
```

The subject's login name is the beneficiary's `foundationalId`: the registries are searched
by it, and the Aggregation Layer refuses a query for anyone other than the consent's
subject. Pick a beneficiary the registries actually hold.

Postman: import `postman/OpenG2P-Aggregator.postman_collection.json` and the environment
`agg-prep.py` just wrote (`OpenG2P-Aggregator.local.postman_environment.json`), then run the
folders in order. Re-run `agg-prep.py` if a seek returns `replay`.

Manual "subject never asked" flow: sign a consent object for a subject with no consent; the
seek ack carries `consent_url` (CM UI). Approve there with the OTP **within 300s of the
object's `issued_at`** (the CM's replay window: the aggregation layer re-validates the
partner's object to learn the granted scopes). The poll picks it up within
`AGGREGATION_LAYER_CM_POLL_INTERVAL_SEC` and the partner's on-search arrives at the callback
without another call. Approved later, the row is rejected with
`consent_approved_after_replay_window` and the partner seeks again.

## Without Docker

```bash
pip install -e backend pytest jsonschema
pytest test/unit                                            # catalog, filter, bene-360 mapping + schemas
PYTHONPATH=backend/src python test/otp/test_otp_flow.py
PYTHONPATH=backend/src python test/kafka/test_consumer_flow.py
CM_SRC=../consent-management VENV=.venv bash test/e2e/run.sh   # real CM + Postgres, fakes around them
```

## Switches

- Kafka off: `AGGREGATION_LAYER_KAFKA_ENABLED=false` in `deploy/.env`, then
  `docker compose -p aggregation-layer -f deploy/docker-compose.yml up -d aggregation-layer`.
- A catalog change needs a restart of the service (it is loaded once, at startup) and, for
  a new registry or scope, `scripts/register-aggregator.py` for the CM bindings.
- Logs: `docker logs -f aggregation-layer`. The catalog is logged at startup
  (`Registry catalog ...: <codes>`); an invalid catalog stops the container with every
  problem listed.
- Consent decisions: the aggregation layer polls the CM; look for `released by consent
  request` / `is no longer active` in its log. Nothing is logged on the CM side.
