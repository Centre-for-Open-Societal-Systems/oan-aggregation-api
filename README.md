# Aggregation Layer

One partner call for a beneficiary's data across several OpenG2P registries, gated on the
beneficiary's consent (and, where the partner's policy asks for it, an OTP), answered as an
[OpenG2P Beneficiary-360](https://github.com/OpenG2P/bene-360-api) response on a signed
DCI `on-search` callback.

- **Standard contract.** The partner sends a Beneficiary-360 (bene-360) request and receives a
  bene-360 response; both validate against the published JSON Schemas.
- **Consent first.** Every consent decision belongs to the OpenG2P Consent Manager (CM). No
  registry is called until the beneficiary has consented and, if required, entered an OTP.
- **Data minimisation.** Registries release whole consented blocks; this service cuts each block
  down to the fields its configuration allows before anything leaves it.
- **Registries are configuration.** Every registry, its binding, its mapping and its allowed
  fields live in one YAML file. Adding a registry needs no code change.

## Contents

- [Architecture](#architecture)
- [Beneficiary-360 conformance](#beneficiary-360-conformance)
- [API](#api)
- [Consent and OTP flow](#consent-and-otp-flow)
- [Configuration](#configuration)
- [Adding a new registry](#adding-a-new-registry)
- [Running locally](#running-locally)
- [Tests](#tests)
- [Security notes](#security-notes)
- [Repository layout](#repository-layout)

## Architecture

```
                         ┌──────────────────────────────────────────────┐
  Partner                │ Aggregation Layer                            │
  ───────                │                                              │
  POST /dci/registry/    │  1 validate consent ─────────────────────────┼──► Consent Manager
    async/search ───────►│    (or raise a consent request)              │    (generic APIs only:
  bene-360 request +     │  2 OTP (subject) / consent screen            │     validate, consent
  consent JWS            │  3 per registry in the catalog:              │     requests, consent
  ◄── 202 ack            │      own grant ─► DCI sync search ───────────┼──► status, policy)
                         │      (signed, legitimate_interest binding)   │
                         │  4 project to allowed fields, map to bene-360│──► Registry A ┐ partner API
                         │  5 sign + POST on-search                     │──► Registry B ├ /dci/registry/
  ◄── on-search ─────────┤     (retried; Kafka optional)                │──► Registry … ┘ sync/search
  (bene-360 response)    │  own Postgres: requests, grants              │
                         └──────────────────────────────────────────────┘
```

- The partner calls this service once. It answers with an acknowledgement; the data arrives
  later on the partner's callback (`header.sender_uri`).
- The CM decides whether the partner may receive each registry block. This service never
  reads the CM database and the CM carries no code for it; consent state is polled from the
  CM's generic APIs ([docs/CM-API-CONTRACT.md](docs/CM-API-CONTRACT.md)).
- Each registry is called through its unchanged OpenG2P partner API
  (`POST /dci/registry/sync/search`). Every hop carries a consent object signed with this
  service's own key and is validated by the CM against a binding on lawful basis
  `legitimate_interest`; the beneficiary's own consent and OTP are enforced here before the
  hop.
- Fan-out and callback delivery run in-process or, with Kafka, in separate workers with
  retry tiers and a dead-letter topic ([docs/KAFKA.md](docs/KAFKA.md)).

## Beneficiary-360 conformance

Specification: [OpenG2P/bene-360-api](https://github.com/OpenG2P/bene-360-api) (`develop`).
The schemas this service is tested against are vendored in
[test/fixtures/bene360](test/fixtures/bene360) with their source commit.

| bene-360 | Status |
|---|---|
| Request: `@context`, `foundationalId`, `timeframe`, `asOfDate`, `sections`, `registryFilter`, `correlationId` | Supported; validated exactly as `request.schema.json` (unknown properties are rejected). |
| `registries[] → registers[] → tables[]` (any depth) | Supported. Placement comes from the registry catalog. |
| `attributes` | Only the catalog's allowed fields. Register attributes are grouped by the consent scope (block) they came from. |
| `meta.warnings[]` | Every registry asked for that gave nothing back is explained: `REGISTRY_NOT_CONFIGURED`, `NOT_CONSENTED`, `NO_ACTIVE_GRANT`, or the registry's own error code; `IDENTIFIER_MISMATCH` when a registry returned records that are not this beneficiary (discarded). |
| `meta.resolvedTimeframe` | The requested window (`Short` = 90 days, `Medium` = 1 year, `Long` = all time) ending at `asOfDate` or today. `perSourceSystem` reports what each registry actually covered: its current record, as of today. |
| `sections: PROGRAMS`, `DISBURSEMENTS`, `BRIDGE_PROCESSING` | **Not yet.** No PBMS or G2P-Bridge is connected: `programs[]` / `bridgeProcessing[]` are empty and a `SECTION_NOT_SUPPORTED` warning is added. A query that asks only for these is refused (422). |
| History within the timeframe | **Not yet.** The OpenG2P partner API returns the current record. |
| `asOfDate` in the past | Anchors `resolvedTimeframe` only; a warning `AS_OF_DATE_NOT_APPLIED` says so. |

## API

| Route | Caller | Purpose |
|---|---|---|
| `POST /dci/registry/async/search` | Partner (consent JWS in the body) | Submit a bene-360 request. `202` ack; data on the callback. |
| `GET /aggregation/v1/registries` | Anyone | The registry catalog: registries, scope ids, placements, allowed fields. No URLs or secrets. |
| `POST /aggregation/v1/requests/{id}/verify-otp` | The subject (bearer token) | Enter the OTP; releases the fetch. |
| `GET /aggregation/v1/requests/{id}` | The subject (bearer token) | Status and audit of the subject's own request. |
| `GET /aggregation/v1/queue` | Any authenticated caller | Queue health (counts and configuration only). |
| `GET /aggregation/v1/requests/{id}/otp` | The subject, **development only** | Mounted only with `AGGREGATION_LAYER_OTP_DEBUG_ENABLED=true`. |

OpenAPI: `GET /docs` on a running instance.

### Request

A DCI search envelope whose `search_criteria.query` **is** the bene-360 request. The bene-360
schema forbids extra properties, so what this service needs besides the query travels where
DCI already has a place for it:

| Field | Meaning |
|---|---|
| `header.sender_uri` | Callback URL for the `on-search` (DCI's own "respond here" field). |
| `message.transaction_id`, `search_request[0].reference_id` | Echoed on the callback. |
| `search_criteria.query_type` | Always `beneficiary360`. |
| `search_criteria.query` | The bene-360 request. |
| `search_criteria.authorize.consent_jws` | The partner's signed consent object for its binding with this service. Its `data_scopes` are scope ids (below). |
| `search_criteria.purpose` | Optional; defaults to the consent object's purpose. |

One beneficiary per call (`search_request` has exactly one item). `foundationalId` must be the
consent object's `subject_id.value`.

```json
POST /dci/registry/async/search
{
  "signature": "<detached JWS, optional>",
  "header": {
    "version": "1.0.0", "message_id": "4f1e…", "message_ts": "2026-10-08T09:29:58Z",
    "action": "search", "sender_id": "partner-x", "receiver_id": "aggregation-layer",
    "sender_uri": "https://partner.example.org/dci/on-search",
    "total_count": 1, "is_msg_encrypted": false
  },
  "message": {
    "transaction_id": "txn-0001",
    "search_request": [{
      "reference_id": "ref-0001",
      "timestamp": "2026-10-08T09:29:58Z",
      "search_criteria": {
        "version": "1.0.0",
        "query_type": "beneficiary360",
        "query": {
          "@context": "https://schemas.openg2p.org/beneficiary360/v1/context.jsonld",
          "foundationalId": "7615076397",
          "timeframe": "Timeframe-Medium",
          "sections": ["REGISTRIES"],
          "registryFilter": ["FARMER_REGISTRY", "LIVESTOCK_REGISTRY", "CROPSOWN_REGISTRY"],
          "correlationId": "req-2026-10-08-0001"
        },
        "purpose": {"code": "loan_origination"},
        "authorize": {"consent_jws": "eyJhbGciOiJFUzI1NiIs…"}
      }
    }]
  }
}
```

**Scope ids.** Consent works at the level of a registry block (one top-level block of the
registry's outgest template). A partner names them as `<registryCode>.<block>`, e.g.
`FARMER_REGISTRY.farmer_personal_details`; `GET /aggregation/v1/registries` lists them. The
CM answers with the intersection of what the consent object asks for, what the partner's
policy allows and what the beneficiary granted.

### Acknowledgement (`202`)

```json
{
  "aggregation_id": "3f1c2a9e-7b1d-4c55-9a51-0e6f4f1d2b77",
  "correlation_id": "9d0c…", "transaction_id": "txn-0001", "status": "pdng",
  "otp_required": true, "otp_channel": "PHONE", "otp_expires_at": "2026-10-08T09:35:00Z",
  "accepted_scopes": ["FARMER_REGISTRY.farmer_personal_details", "FARMER_REGISTRY.family_details"],
  "registries": ["FARMER_REGISTRY"],
  "callback_url": "https://partner.example.org/dci/on-search",
  "message": "OTP sent to the subject. …"
}
```

When the beneficiary has never consented to this partner, the ack instead carries
`consent_request_id` and `consent_url` (see [the flow](#consent-and-otp-flow)).

### Callback: `on-search` carrying the bene-360 response

The callback is a signed DCI `on-search`; `search_response[0].data.reg_records[0]` is the
bene-360 response. The DCI `header.meta` carries this service's own facts about the release
(`lawful_basis`, `subject_authentication`, `consent_enforcement`), which have no place in the
bene-360 schema. `header.status` is `rjct` (`AGG-VAL-001`) only when no registry could be
queried; the record then explains why in `meta.warnings`.

```json
{
  "signature": "eyJhbGciOiJFZERTQSIs…..<sig>",
  "header": {
    "version": "1.0.0", "message_id": "…", "message_ts": "2026-10-08T09:30:00+00:00",
    "action": "on-search", "status": "succ", "total_count": 1, "completed_count": 1,
    "sender_id": "aggregation-layer", "receiver_id": "partner-x", "is_msg_encrypted": false,
    "meta": {"consent_enforcement": "enabled", "lawful_basis": "consent",
             "subject_authentication": "otp"}
  },
  "message": {
    "transaction_id": "txn-0001", "correlation_id": "9d0c…",
    "search_response": [{
      "reference_id": "ref-0001", "timestamp": "2026-10-08T09:30:00+00:00", "status": "succ",
      "data": {
        "version": "1.0.0", "reg_type": "beneficiary360",
        "reg_record_type": "Beneficiary360Response",
        "reg_records": [<the bene-360 response below>]
      },
      "pagination": {"page_size": 1, "page_number": 1, "total_count": 1}, "locale": "en"
    }]
  }
}
```

```json
{
  "@context": "https://schemas.openg2p.org/beneficiary360/v1/context.jsonld",
  "@type": "Beneficiary360Response",
  "@id": "urn:openg2p:aggregation:3f1c2a9e-7b1d-4c55-9a51-0e6f4f1d2b77",
  "beneficiary": {"foundationalId": "7615076397", "matchedRegistryCount": 1},
  "registries": [{
    "registryCode": "FARMER_REGISTRY",
    "registryName": "Farmer Registry",
    "registers": [{
      "registerMnemonic": "FARMER",
      "registerName": "Farmer",
      "functionalRecordId": "7615076397",
      "foundationalId": "7615076397",
      "recordStatus": "UNKNOWN",
      "lastApprovedAt": "2026-07-01T10:00:00+00:00",
      "attributes": {
        "farmer_personal_details": {
          "member_identifier": [{"identifier_value": "7615076397"}],
          "demographic_info": {
            "name": {"given_name": "Mary", "surname": "Bell"},
            "phone_number": ["+251911000055"],
            "sex": "female",
            "birth_date": "1990-04-02"
          },
          "marital_status": "married",
          "education_level": "secondary",
          "registration_date": "2026-02-11"
        }
      },
      "tables": [{
        "tableMnemonic": "HOUSEHOLD",
        "internalRecordId": "HH-0042",
        "tableName": "Household",
        "attributes": {"group_identifier": [{"identifier_type": "UIN", "identifier_value": "HH-0042"}],
                       "group_size": 5},
        "tables": []
      }]
    }]
  }],
  "meta": {
    "generatedAt": "2026-10-08T09:30:00Z",
    "correlationId": "req-2026-10-08-0001",
    "requestParameters": {"foundationalId": "7615076397", "timeframe": "Timeframe-Medium",
                          "sections": ["REGISTRIES"]},
    "resolvedTimeframe": {
      "start": "2025-10-08", "end": "2026-10-08",
      "perSourceSystem": [{"system": "FARMER_REGISTRY", "start": "2026-10-08", "end": "2026-10-08"}]
    },
    "sourceSystemsQueried": ["FARMER_REGISTRY", "LIVESTOCK_REGISTRY"],
    "warnings": [
      {"system": "LIVESTOCK_REGISTRY", "code": "unreachable", "message": "connect timeout"},
      {"system": "CROPSOWN_REGISTRY", "code": "NOT_CONSENTED",
       "message": "no scope of this registry is within the consent; not queried"}
    ]
  }
}
```

### Errors

Errors are `{"error": "<reason>", "detail": "<text>"}` with an HTTP status. A body that does not
match the envelope or the bene-360 request schema is answered `400` by the platform's request
validation.

| Status | `error` | When |
|---|---|---|
| 400 | `no_callback`, `bad_callback` | `header.sender_uri` missing or not http(s) |
| 403 | CM reason code (`signature_invalid`, `replay`, `expired`, …) | the CM refused the consent object |
| 403 | `subject_mismatch` | `foundationalId` is not the consent's subject |
| 403 | `no_scope_permitted` / `no_scope_requested` | nothing of the requested registries is within the consent |
| 422 | `section_not_supported` | only sections without a connected source were asked for |
| 422 | `no_registry` | `registryFilter` names no registry in the catalog |
| 401 / 404 / 409 | `unauthenticated`, `not_found`, `wrong_state`, OTP reasons | subject routes |

## Consent and OTP flow

1. **Seek.** The partner's consent object is validated by the CM's `/consent/v1/validate` —
   signature (against Partner Management), policy ceiling, replay window, and the
   beneficiary's own grant. `foundationalId` must be the consent's subject.
2. **Consent held, OTP required** (the partner's policy has `required_auth_method=otp`): an OTP
   is issued to the subject; the ack says `otp_required: true`. The subject calls
   `POST /aggregation/v1/requests/{id}/verify-otp` **with their own bearer token**; the request
   must be theirs.
3. **Consent held, no OTP required:** the fetch starts at once, on the consent alone.
4. **Never asked** (CM answers `no_subject_consent`): a consent request is raised in the CM for
   the scope ids the partner asked for, and the ack carries `consent_url`. The subject approves
   on the CM consent screen (its OTP is the authentication). This service polls the CM, sees
   the approval, re-validates the partner's object to learn what was granted, and fans out —
   only to the granted blocks. The approval must land within the CM's replay window
   (300 s by default) of the object's `issued_at`; later, the request is rejected with
   `consent_approved_after_replay_window` and the partner seeks again.
5. **Internal partners** (policy lawful basis `legitimate_interest`): no consent or OTP is
   sought; the partner's policy ceiling is the whole of the authority.
6. **Fan-out.** Per registry: a grant recorded by this service (no registry is called without
   one), a check that the CM still reports the consent as active, a signed DCI search by
   `foundationalId`, projection to the allowed fields, and mapping into the response.
7. **Withdrawal.** Withdrawing the consent in the CM cancels every aggregation that has not
   been delivered and revokes its grants (seen on the next poll, and checked again right before
   the fan-out and before the callback). Every check fails closed: if the CM cannot answer, no
   data moves.

## Configuration

### Environment

Every setting is `AGGREGATION_LAYER_<NAME>`; the full list with comments is
[`config.py`](backend/src/openg2p_aggregation_layer/config.py), and
[`deploy/.env.example`](deploy/.env.example) is a working starting point. The most important:

| Variable | Purpose |
|---|---|
| `REGISTRY_CATALOG_PATH` | **Required.** Path to the registry catalog YAML. Invalid or missing = the service does not start. |
| `DB_HOSTNAME`, `DB_PORT`, `DB_USERNAME`, `DB_PASSWORD`, `DB_DBNAME` | This service's own Postgres. |
| `SIGNING_P12_PATH` (+ `_PASSWORD`) or `SIGNING_PRIVATE_KEY_PEM`, `SIGNING_KID` | This service's signing key; its public half is registered in Partner Management. |
| `AGGREGATOR_SENDER_ID`, `AGGREGATOR_ISSUER` | DCI `sender_id` / consent `iss` on registry hops. `sender_id` must map to the PM partner (`PARTNER_<SENDER_ID>`). |
| `CM_BASE_URL`, `CM_TOKEN_URL`, `CM_CLIENT_ID`, `CM_CLIENT_SECRET` | The Consent Manager and the client-credentials login to it. |
| `CM_POLL_INTERVAL_SEC`, `CM_POLL_IN_APP` | How consent decisions are read back from the CM. |
| `AUTH_ENABLED`, `AUTH_ISSUER`, `AUTH_JWKS_URL`, `AUTH_AUDIENCE` | Verification of the subject's bearer token. |
| `OTP_PROVIDER`, `OTP_SALT`, `OTP_TTL_SEC`, `OTP_MAX_ATTEMPTS` | OTP behaviour. |
| `OTP_DEBUG_ENABLED` | **Development only**, default `false`: keeps the plaintext OTP and exposes it on an endpoint. |
| `AGGREGATOR_REGISTRY_TIMEOUT`, `AGGREGATOR_PAGE_SIZE` | Defaults for registry hops (a catalog entry may override). |
| `KAFKA_ENABLED`, `KAFKA_BOOTSTRAP_SERVERS`, … | Optional queue; see [docs/KAFKA.md](docs/KAFKA.md). |

### Registry catalog

One YAML file lists every registry this service may query. It is loaded and validated at
startup (unknown keys, missing fields, dangling references and contradictory field paths
are all errors, listed together). It holds no secrets.
[`deploy/registries.yaml`](deploy/registries.yaml) is a complete, commented example for three
registries.

```yaml
version: 1
registries:
  FARMER_REGISTRY:                       # bene-360 registryCode: letters, digits, '_' '-' (no '.')
    name: Farmer Registry                # bene-360 registryName
    partner_api:
      base_url: http://farmer-registry-partner-api:8000   # calls <base_url>/dci/registry/sync/search
      receiver_id: farmer-registry       # DCI header.receiver_id
      timeout_sec: 30                    # optional
    binding:                             # CM binding this service spends on each hop
      audience: agg-layer-farmer         #   (lawful basis legitimate_interest)
      controller_id: farmer_registry
      allowed_purposes: [loan_origination]   # written by scripts/register-aggregator.py
    search:
      reg_type: Farmer                   # DCI search_criteria.reg_type / reg_record_type
      reg_record_type: spdci-extensions-dci:FarmerRecord
      id_type: UIN                       # DCI identifier type of the foundationalId: sent as
                                         #   query.value.id_type and required on the match
      match:                             # exact identity check on every returned record
        path: farmer_personal_details.member_identifier[]   # identifiers (list or object);
                                         #   first segment must be a scope (always fetched,
                                         #   released only if consented)
        value_key: identifier_value      # default
        type_key: identifier_type        # default; null = compare the value only
      page_size: 10                      # optional
    registers:                           # bene-360 registers the record maps onto
      FARMER:
        name: Farmer
        # Paths into the whole rendered record. What they read is released as identifiers.
        internal_record_id: farmer_personal_details.record_id              # optional
        functional_record_id: farmer_personal_details.member_identifier[].identifier_value
        record_status: farmer_personal_details.status                      # optional
        last_approved_at: farmer_personal_details.last_updated             # optional (ISO date-time)
        default_record_status: ACTIVE    # when record_status finds nothing (default UNKNOWN)
    scopes:                              # one per top-level block of the outgest template
      farmer_personal_details:           # partners consent to FARMER_REGISTRY.farmer_personal_details
        register: FARMER                 # attributes of register FARMER, under this block's name
        fields:                          # the allow-list: only these paths leave the service
          - demographic_info.name.given_name
          - demographic_info.name.surname
          - member_identifier[].identifier_value      # [] maps over a list
      farm_details:
        table: FARM                      # each element (list) or the object becomes a table row
        name: Farm
        parent: FARMER                   # a register, or a table (then `join` is required)
        internal_record_id: farm_id      # row-relative; a positional id is used when absent
        fields: [farm_id, farm_type, land_size, measurement]
        tables:                          # optional nested tables read from inside each row
          - path: farming_activities[].crop_production
            table: CROP
            name: Crop
            fields: [crop_type, season]
      land_parcels:
        table: PARCEL
        parent: FARM                     # a table parent: rows are matched by key
        join: {field: farm_ref, parent_field: farm_id}
        fields: ["*"]                    # whole row; write it only on purpose
```

Rules worth knowing:

- **Field paths** are dotted; `name[]` maps over a list. A listed path keeps its structure in
  the output (`demographic_info.name.given_name` → `{"demographic_info": {"name": {...}}}`).
  Empty values (`""`, `[]`, `{}`, `null`) are dropped so they never look like data. `["*"]`
  keeps a whole block or row and cannot be combined with nested `tables`.
- **Consent stays at block level.** The CM and the registry release whole blocks; `fields` is
  the extra, per-field filter this service applies before the partner sees anything.
- A **join** row whose key matches no parent row is kept, directly under its register.
- Register **metadata paths** (`internal_record_id`, `functional_record_id`, …) are read from
  the record and released as identifiers; point them only at data that may be released.
- A catalog change takes effect on restart.

## Adding a new registry

No code change is needed: a registry is configuration on this service plus a binding in the
Consent Manager.

**Prerequisites on the registry side**

1. The registry runs the OpenG2P partner API and answers `POST /dci/registry/sync/search`
   (DCI `idtype-value` queries).
2. It can find a beneficiary by the **foundational ID**: the value is sent as
   `query.value.id_value` with the catalog's `id_type`. The OpenG2P partner API answers an
   `idtype-value` query with a substring search over the register's `search_text` (and does
   not look at `id_type`), so the foundational ID must be one of the register's search-text
   fields.
3. Its rendered record carries the foundational ID as an identifier the catalog's
   `search.match` can read exactly (in the DCI templates: a `member_identifier` entry with
   `identifier_type` = `id_type`). Because the search is a substring match, a record that
   only *contains* the ID — another person's phone number or ID — can come back; this service
   keeps a record only on an exact identifier match, discards the rest and reports them in
   `meta.warnings` (`IDENTIFIER_MISMATCH`). The exact DCI `expression` query is not used: the
   platform answers it without the record's child hierarchy, so every table would be lost.
4. Its outgest template renders the record as top-level blocks — each block is one consent
   scope.
5. Consent enforcement is on in the registry's partner API: each search is validated with the
   Consent Manager, and the record is clamped to the consented blocks.

**Steps**

1. **Catalog entry.** Add the registry to the catalog file (`deploy/registries.yaml` or
   wherever `AGGREGATION_LAYER_REGISTRY_CATALOG_PATH` points): partner API URL, CM binding
   audience + controller id, DCI search parameters with the `id_type` and the identifier
   `match` path, registers, and one scope per block with
   its placement and allowed fields.
2. **Validate it.** Start the service (or run any script below): an invalid entry is reported
   with every problem at once.
3. **Bindings.** Run
   ```bash
   python scripts/register-aggregator.py --registry NEW_REGISTRY --partner <partner-audience>
   ```
   It creates the CM binding *this service → registry* (lawful basis `legitimate_interest`,
   policy ceiling = the entry's blocks) and, for each `--partner`, adds the new scope ids
   (`NEW_REGISTRY.<block>`) to that partner's binding with this service. Scopes are only ever
   added. With AWE enabled, both changes are widenings and land `pending`; the script approves
   its own tasks (it needs a CM admin login: `G2P_STAFF_USER` / `G2P_STAFF_PASSWORD`).
4. **Wait for the CM's policy cache** (`partner_cache_ttl_sec`, 60 s by default) before the
   first seek.
5. **Restart** this service so it loads the new catalog. `GET /aggregation/v1/registries`
   now lists the registry and its scope ids.
6. **Verify.** `python postman/agg-prep.py` (a consent covering the new scope ids), then
   `python scripts/stack-check.py`: it reports every catalog registry as matched, empty, or
   with the warning that explains why not.

Partners then include the new scope ids in their consent objects, and beneficiaries see them
on the consent screen.

## Running locally

[docs/RUN-LOCAL.md](docs/RUN-LOCAL.md) has the full walk-through. In short:

```bash
python scripts/register-aggregator.py --partner <partner-audience>   # Keycloak, key, PM, CM bindings, deploy/.env
docker compose -p aggregation-layer -f deploy/docker-compose.yml up -d --build
curl http://localhost:8110/ping
python postman/agg-prep.py && python scripts/stack-check.py
```

`deploy/docker-compose.yml` mounts `deploy/registries.yaml` at `/config/registries.yaml`.
The image installs [openg2p-fastapi-common](https://github.com/OpenG2P/openg2p-fastapi-common)
from source (`FASTAPI_COMMON_REF` build argument).

## Tests

```bash
pip install -e backend pytest jsonschema

pytest test/unit                                           # no services needed
PYTHONPATH=backend/src python test/otp/test_otp_flow.py   # OTP providers
PYTHONPATH=backend/src python test/kafka/test_consumer_flow.py   # queue handlers, no broker
CM_SRC=../consent-management bash test/e2e/run.sh          # real CM + Postgres, fakes around them
```

| Suite | Covers |
|---|---|
| `test/unit/test_registry_catalog.py` | catalog loading; every class of invalid catalog fails with a clear message |
| `test/unit/test_projection.py` | the field-level filter |
| `test/unit/test_bene360_mapping.py` | record → registers / tables / joins, warnings, timeframe |
| `test/unit/test_bene360_schema.py` | generated requests and responses against the published bene-360 schemas |
| `test/unit/test_fourth_registry.py` | a registry added only through a catalog fixture is queried and mapped |
| `test/unit/test_subject_checks.py` | subject binding of the query and of the OTP release |
| `test/e2e` | the whole flow against the CM: consent held + OTP, raise + approve, withdraw, deny, late approval |

## Security notes

- **Consent and OTP are enforced before any registry call**, and every check fails closed: an
  unreachable CM stops the release; no grant, no hop.
- **The beneficiary is the consent's subject.** A query for any other `foundationalId` is
  refused, so a consent for one person never fetches another.
- **The OTP release needs the subject's own token** and the request must be theirs; an id plus a
  code is not enough. An unknown request and someone else's look identical (`404`).
- **Data minimisation:** only catalog-listed fields leave the service; consent is enforced per
  block by the CM and the registry. Registries never see the partner, only this service's
  binding.
- **Signing:** registry hops and callbacks are signed with this service's own key (configure a
  persistent key; without one an ephemeral key is generated and every signature is rejected
  downstream). The partner's envelope `signature` is not verified; the partner is
  authenticated by its consent object, which the CM verifies against Partner Management.
- **`OTP_DEBUG_ENABLED` must stay `false`** outside development: it stores the plaintext OTP and
  exposes it on an endpoint. Set a per-environment `OTP_SALT`.
- **CM service account:** the CM's policy read and partner list require `CONSENT_MANAGER_ADMIN`
  today; treat `CM_CLIENT_SECRET` like an admin credential
  ([docs/CM-API-CONTRACT.md](docs/CM-API-CONTRACT.md#gaps-cm-api-not-on-develop-deliberately-not-added-to-the-cm)).
- **Personal data at rest:** with Kafka, the delivery topic carries the signed response until it
  is delivered; its retention is short by default (`KAFKA_DELIVERY_RETENTION_MS`, 1 hour). The
  database keeps per-registry outcomes, never attribute values.
- The registry catalog holds no secrets; secrets belong in the environment.

## Repository layout

```
backend/   FastAPI service (python -m openg2p_aggregation_layer.main), worker
           (Kafka consumers + CM poll, .worker) and reaper (.reap)
             registry_catalog.py   catalog model, validation, field-level filter
             bene360.py            bene-360 request model and response mapping
deploy/    Dockerfile, docker-compose (service + own Postgres), Kafka compose,
           registries.yaml (catalog), .env.example
docs/      Split plan, CM API contract, Kafka design, local run guide
postman/   Collection, environment template, agg-prep.py, callback receiver
scripts/   register-aggregator.py, stack-check.py, queue-status.sh
test/      unit, OTP, Kafka and end-to-end tests; fixtures (bene-360 schemas, catalogs)
```

## License

[MIT](LICENSE)
