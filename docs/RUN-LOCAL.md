# Run and test locally (Aggregation Layer + Consent Manager)

## What runs where

| Service | URL | Container |
|---|---|---|
| Aggregation Layer | http://localhost:8110 (`/docs`) | `aggregation-layer` (no database) |
| Consent Manager (`develop`, no aggregation code, `subject_consent_required=true`) | http://localhost:8000 | from the consent-management repository |
| Partner callback receiver (dev, async route) | http://localhost:9099/all | `agg-callback` |

The service publishes on host port 8110 so it does not collide with an API gateway on 8100.
The registries, Partner Management and Keycloak are separate deployments on the shared
Docker network (`OPENG2P_NETWORK`, see `deploy/docker-compose.yml`). The service keeps no
state: no database, no broker, nothing to migrate.

## One-time setup

```bash
# 1. the registry catalog: one entry per registry this service may query
$EDITOR deploy/registries.yaml

# 2. Keycloak client + role, signing key, PM partner, one CM binding per catalog
#    registry (legitimate_interest), deploy/.env. --partner extends an existing
#    partner binding so it may consent to the catalog's scope ids.
python scripts/register-aggregator.py --partner <partner-audience>

docker compose -p aggregation-layer -f deploy/docker-compose.yml up -d --build
```

The scripts need a virtualenv with `httpx`, `pyjwt`, `cryptography`, `pydantic` and `pyyaml`
(`pip install -e backend` brings them all).

## Test

```bash
# gives the subject an active consent for the partner, signs a fresh consent object,
# writes the local Postman environment (the object is valid 5 min)
G2P_PARTNER_AUDIENCE=<partner-audience> G2P_PARTNER_PM_ID=<pm-partner-id> \
G2P_SUBJECT_USER=<beneficiary login> G2P_SUBJECT_PASSWORD=<password> \
  python postman/agg-prep.py

python scripts/stack-check.py     # A sync, B async, C never asked -> consent in the CM -> sync
```

`stack-check.py` C reads the subject's one-time code back from the CM, so it needs the CM with
`otp_debug_enabled`.

The subject's login name is the beneficiary's `foundationalId`: the registries are searched
by it, and the Aggregation Layer refuses a query for anyone other than the consent's
subject. Pick a beneficiary the registries actually hold.

Postman: import `postman/OpenG2P-Aggregator.postman_collection.json` and the environment
`agg-prep.py` just wrote (`OpenG2P-Aggregator.local.postman_environment.json`), then run the
folders in order. Re-run `agg-prep.py` if a search returns `replay` or
`consent_object_reused`.

"Subject never asked": `python postman/agg-prep.py --no-grant` withdraws the subject's
consents to the partner; the search then answers `403 no_subject_consent`. The partner
raises a consent request in the CM, the subject confirms it with the one-time code, and the
partner searches again with a new consent object (`stack-check.py` C does exactly this).

## Without Docker

```bash
pip install -e backend pytest jsonschema
pytest test/unit                                               # no services needed
CM_SRC=../consent-management VENV=.venv bash test/e2e/run.sh   # real CM + Postgres (CM only), fakes around them
```

## Switches

- A catalog change needs a restart of the service (it is loaded once, at startup) and, for
  a new registry or scope, `scripts/register-aggregator.py` for the CM bindings.
- More than one pod or worker: all of them need the same signing key, and the same
  `AGGREGATION_LAYER_STAFF_SEARCH_TICKET_SECRET` (or none, then it is derived from the signing
  key) so any of them can read a staff `searchId`.
- Logs: `docker logs -f aggregation-layer`. The catalog is logged at startup
  (`Registry catalog ...: <codes>`); an invalid catalog stops the container with every
  problem listed. The audit is one JSON line per partner call (`"event": "aggregation"`) and
  per staff search / tab (`staff_search`, `staff_tab_view`), on the logger
  `app.audit`.
- Async: the callback is one attempt with no retry, and an async search in flight is lost if
  the service restarts. The audit line's `outcome` says what happened (`delivered`,
  `callback_failed`, `consent_withdrawn`, `consent_check_failed`).
