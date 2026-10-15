# Aggregation Layer

One partner call for a beneficiary's data across several OpenG2P registries, gated on the
beneficiary's consent, answered as an
[OpenG2P Beneficiary-360](https://github.com/OpenG2P/bene-360-api) response inside a signed
DCI `on-search`: in the HTTP response (sync, the default) or POSTed to the partner's callback
(async).

- **Standard contract.** The partner sends a Beneficiary-360 (bene-360) request and receives a
  bene-360 response; both validate against the published JSON Schemas.
- **Consent first.** Every consent decision belongs to the OpenG2P Consent Manager (CM). The
  partner obtains the beneficiary's consent in the CM (consent request + one-time code) before
  it calls this service; no registry is called without it.
- **Stateless.** No database, no queue, no worker. Everything needed to answer a call is in
  the call; the CM is asked again on every call.
- **Data minimisation.** Registries release whole consented blocks; this service cuts each block
  down to the fields its configuration allows before anything leaves it.
- **Registries are configuration.** Every registry, its binding, its mapping and its allowed
  fields live in one YAML file. Adding a registry needs no code change.

## Contents

- [Architecture](#architecture)
- [Beneficiary-360 conformance](#beneficiary-360-conformance)
- [API](#api)
- [Consent flow](#consent-flow)
- [Configuration](#configuration)
- [Adding a new registry](#adding-a-new-registry)
- [Running locally](#running-locally)
- [Tests](#tests)
- [Security notes](#security-notes)
- [Repository layout](#repository-layout)

## Architecture

```
  Partner                                                    Consent Manager
  ───────                                                    ───────────────
  (a) POST /consent/v1/consent-requests ───────────────────► raise; OTP sent to the subject (SMS)
  (b) confirm the subject's one-time code ─────────────────► consent recorded (grant)

                         ┌──────────────────────────────────────────────┐
  (c) POST /dci/registry/│ Aggregation Layer (stateless)                │
      sync/search ──────►│  1 validate the partner's consent object ────┼──► CM /consent/v1/validate
  bene-360 request +     │    (no subject consent -> 403)               │
  consent JWS            │  2 per registry in the catalog, in parallel: │
                         │      DCI sync search (signed,                │──► Registry A ┐ partner API
                         │      legitimate_interest binding)            │──► Registry B ├ /dci/registry/
                         │  3 project to allowed fields, map to bene-360│──► Registry … ┘ sync/search
  ◄── 200 on-search ─────┤  4 sign the on-search                        │
  (bene-360 response)    │                                              │
                         │  async route: 202 ack at once; before the    │
  ◄── on-search ─────────┤  callback, CM consent status checked again ──┼──► CM /consents/{id}/status
      on sender_uri      │  (one attempt, no retry)                     │
                         └──────────────────────────────────────────────┘
```

- The subject's consent comes first and does not involve this service: the partner raises a
  consent request in the CM, the CM sends the subject a one-time code, the partner confirms
  the code with the CM and the CM records the consent
  ([docs/CM-API-CONTRACT.md](docs/CM-API-CONTRACT.md#what-the-partner-does-in-the-cm-before-calling-the-al)).
- The partner then calls this service once. Sync (`/dci/registry/sync/search`, the default):
  the signed on-search is the HTTP response. Async (`/dci/registry/async/search`): a `202`
  acknowledgement, and the same on-search is POSTed to `header.sender_uri` as soon as the
  registries have answered.
- The CM decides whether the partner may receive each registry block. This service never
  reads the CM database and the CM carries no code for it; it uses two generic CM APIs,
  `/consent/v1/validate` and `/consent/v1/consents/{id}/status`.
- Each registry is called through its unchanged OpenG2P partner API
  (`POST /dci/registry/sync/search`), all of them in parallel. Every hop carries a consent
  object signed with this service's own key and is validated by the CM against a binding on
  lawful basis `legitimate_interest`; the beneficiary's own consent is checked here, through
  the CM, before the hop.
- Nothing is stored. The audit is one JSON log line per call (see [Audit](#audit)). An async
  search in flight is an in-process task: it is lost if the service restarts.

## Beneficiary-360 conformance

Specification: [OpenG2P/bene-360-api](https://github.com/OpenG2P/bene-360-api) (`develop`).
The schemas this service is tested against are vendored in
[test/fixtures/bene360](test/fixtures/bene360) with their source commit.

| bene-360 | Status |
|---|---|
| Request: `@context`, `foundationalId`, `timeframe`, `asOfDate`, `sections`, `registryFilter`, `correlationId` | Supported; validated exactly as `request.schema.json` (unknown properties are rejected). |
| `registries[] → registers[] → tables[]` (any depth) | Supported. Placement comes from the registry catalog. |
| `attributes` | Only the catalog's allowed fields. Register attributes are grouped by the consent scope (block) they came from. |
| `meta.warnings[]` | Every registry asked for that gave nothing back is explained: `REGISTRY_NOT_CONFIGURED`, `NOT_CONSENTED`, or the registry's own error code; `IDENTIFIER_MISMATCH` when a registry returned records that are not this beneficiary (discarded). |
| `meta.resolvedTimeframe` | The requested window (`Short` = 90 days, `Medium` = 1 year, `Long` = all time) ending at `asOfDate` or today. `perSourceSystem` reports what each registry actually covered: its current record, as of today. |
| `sections: PROGRAMS`, `DISBURSEMENTS`, `BRIDGE_PROCESSING` | **Not yet.** No PBMS or G2P-Bridge is connected: `programs[]` / `bridgeProcessing[]` are empty and a `SECTION_NOT_SUPPORTED` warning is added. A query that asks only for these is refused (422). |
| History within the timeframe | **Not yet.** The OpenG2P partner API returns the current record. |
| `asOfDate` in the past | Anchors `resolvedTimeframe` only; a warning `AS_OF_DATE_NOT_APPLIED` says so. |

## API

| Route | Caller | Purpose |
|---|---|---|
| `POST /dci/registry/sync/search` | Partner (consent JWS in the body) | **Default.** Submit a bene-360 request; `200`, the body is the signed on-search carrying the bene-360 response. |
| `POST /dci/registry/async/search` | Partner (consent JWS in the body) | The same request; `202` ack, the on-search is POSTed to `header.sender_uri`. |
| `GET /aggregation/v1/registries` | Anyone | The registry catalog: registries, scope ids, placements, allowed fields. No URLs or secrets. |
| `POST /aggregation/v1/staff/registry/search` | Registry staff (bearer token + staff search role) | Find a beneficiary in every registry's staff-api, **no consent**: summary and tabs per record. See [Staff search](#staff-search). |
| `POST /aggregation/v1/staff/registry/tab` | Registry staff, same role | One tab of a record that search returned: sections, fields, rows. |

OpenAPI: `GET /docs` on a running instance.

### Request

A DCI search envelope whose `search_criteria.query` **is** the bene-360 request. The bene-360
schema forbids extra properties, so what this service needs besides the query travels where
DCI already has a place for it:

| Field | Meaning |
|---|---|
| `header.sender_uri` | Async route only, required there: callback URL for the `on-search` (DCI's own "respond here" field). Ignored on the sync route. |
| `message.transaction_id`, `search_request[0].reference_id` | Echoed in the on-search. |
| `search_criteria.query_type` | Always `beneficiary360`. |
| `search_criteria.query` | The bene-360 request. |
| `search_criteria.authorize.consent_jws` | The partner's signed consent object for its binding with this service. Its `data_scopes` are scope ids (below). |
| `search_criteria.purpose` | Optional; defaults to the consent object's purpose. |

One beneficiary per call (`search_request` has exactly one item). `foundationalId` must be the
consent object's `subject_id.value`.

```json
POST /dci/registry/sync/search
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

The async route takes the same body with `header.sender_uri` set.

### Sync answer (`200`): `on-search` carrying the bene-360 response

The answer is a signed DCI `on-search`; `search_response[0].data.reg_records[0]` is the
bene-360 response. The DCI `header.meta` carries this service's own facts about the release
(`lawful_basis`, `consent_enforcement`), which have no place in the bene-360 schema.
`header.status` is `rjct` (`AGG-VAL-001`) only when no registry could be queried; the record
then explains why in `meta.warnings`. `message.correlation_id` is this service's id for the
call (it is also in the audit line).

```json
{
  "signature": "eyJhbGciOiJFZERTQSIs…..<sig>",
  "header": {
    "version": "1.0.0", "message_id": "…", "message_ts": "2026-10-08T09:30:00+00:00",
    "action": "on-search", "status": "succ", "total_count": 1, "completed_count": 1,
    "sender_id": "aggregation-layer", "receiver_id": "partner-x", "is_msg_encrypted": false,
    "meta": {"consent_enforcement": "enabled", "lawful_basis": "consent"}
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
  "@id": "urn:openg2p:aggregation:9d0c…",
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

### Async: acknowledgement (`202`) and callback

Every check (callback URL, query, consent) runs before the ack, so a `202` means the
registries are being asked:

```json
{
  "correlation_id": "9d0c…", "transaction_id": "txn-0001", "status": "pdng",
  "accepted_scopes": ["FARMER_REGISTRY.farmer_personal_details", "FARMER_REGISTRY.family_details"],
  "registries": ["FARMER_REGISTRY"],
  "callback_url": "https://partner.example.org/dci/on-search",
  "message": "Accepted. The Beneficiary-360 response will be POSTed to sender_uri as on-search when the registries have answered."
}
```

The callback is the same signed on-search as the sync answer, with the same
`correlation_id`. It is POSTed once, as soon as the registries have answered; there is no
retry, and an answer in flight is lost if the service restarts (nothing is stored to retry
from). Right before the callback the CM is asked again whether the consent is still active:

| CM says | Callback |
|---|---|
| `active` | the on-search with the data |
| anything else (withdrawn, expired) | a signed on-search with `header.status: rjct`, `status_reason_code: consent_withdrawn`, and no data |
| no answer | the same, with `consent_check_failed` |

### Errors

Errors are `{"error": "<reason>", "detail": "<text>"}` with an HTTP status. A body that does not
match the envelope or the bene-360 request schema is answered `400` by the platform's request
validation.

| Status | `error` | When |
|---|---|---|
| 400 | `no_callback`, `bad_callback` | async route: `header.sender_uri` missing or not http(s) |
| 403 | `no_subject_consent` | the subject has not consented to this partner. The detail says what to do: raise a consent request in the CM (`POST /consent/v1/consent-requests`), have the subject confirm it with the one-time code, then call again with a new consent object |
| 403 | other CM reason code (`signature_invalid`, `replay`, `expired`, …) | the CM refused the consent object |
| 403 | `subject_mismatch` | `foundationalId` is not the consent's subject |
| 403 | `no_scope_permitted` | nothing of the requested registries is within the consent |
| 409 | `consent_object_reused` | the consent object already carried a search for other registries (the CM answers an object once, with its first permit); sign a new one |
| 422 | `section_not_supported` | only sections without a connected source were asked for |
| 422 | `no_registry` | `registryFilter` names no registry in the catalog |
| 503 | `cm_unavailable`, `cm_auth_failed` | the CM could not be asked; nothing is released on a guess |
| CM's status | `cm_rejected` | the CM answered the call itself with an HTTP error |

### Audit

Nothing is stored; the audit is one JSON line per event on the logger
`app.audit`, to be shipped to the log store with the rest of the service's logs.

| `event` | When | Carries |
|---|---|---|
| `aggregation` | every partner call that passed the checks, once answered (sync) or once the callback was attempted (async) | `correlation_id`, `transaction_id`, `mode` (`sync` / `async`), `partner`, `subject` (last four characters only), `lawful_basis`, `cm_consent_id`, `scopes`, per registry `status` / `records` / `reason`, `outcome` (`answered`, `delivered`, `callback_failed`, `consent_withdrawn`, `consent_check_failed`), `duration_ms`, and `callback_status` on async |
| `staff_search` | every staff search | staff user, roles, masked beneficiary, registries, purpose, per-registry outcome |
| `staff_tab_view` | every tab opened | staff user, registry, register, record, tab, outcome |

Attribute values are never logged.

### Staff search

A registry staff user (Farmer, Livestock or Crop sown staff portal) finds a beneficiary in
**every** registry and opens their data tab by tab. It is a separate door from the partner
search: **no consent, no consent object, no consent request, no OTP, no callback**. Two calls,
like the staff portal (tab data is fetched only when a tab is opened):

```
client --(1) POST /aggregation/v1/staff/registry/search --> AL --> each registry's staff-api
       <-- per registry: records, each with a summary and its tabs                (search only)
           (each tab carries the request that opens it)
client --(2) POST /aggregation/v1/staff/registry/tab  (the tab's request.body) --> AL --> staff-api
       <-- that tab only: sections, fields, rows                               (get_tab_records)
```

The client talks to this service only. The staff-apis stay inside the cluster; their
login, CSRF, payload format and ids are this service's business.

| | |
|---|---|
| Caller | Bearer token from the `AUTH_ISSUER` realm (`staff`) holding one of `STAFF_SEARCH_ROLES` (default realm role `AGGREGATION_STAFF_VIEWER`). Otherwise `401` / `403`, before any registry is called. Same for both routes. |
| Registries | Every catalog entry with a `staff_api` block (or `registryFilter`), in parallel. A new registry is a catalog entry: no code change. |
| Search, per registry | `search_in_a_register` per `staff_api.registers` (one staff-api call each) → exact match on `staff_api.match_fields`. The summary is the registry's own list row; the tabs are the register's active tabs (`get_all_tabs`, cached). |
| Tab | One `get_tab_records` call. Section labels and fields come from the tab's section metadata (`/register-tab-metadata/get_sections`, cached). |
| Dynamic tabs | Tabs, sections and fields are the registries' metadata, cached `STAFF_API_METADATA_TTL_SEC` (600s). A tab added in a registry appears by itself; an unknown `tabId` re-reads the tab list once before `400 unknown_tab`. `staff_api.hidden_tabs` keeps a tab away from staff. |
| Tab bound to its search | A tab request is accepted only for a record its search returned (`400 record_not_in_search`), from the staff user who ran it (`404 search_not_found` otherwise), within `STAFF_SEARCH_TTL_SEC` (`410 search_expired`: search again). |
| `searchId` | A signed ticket, not a stored row: it carries the staff user, the records found and the expiry, with an HMAC by this service's key (`STAFF_SEARCH_TICKET_SECRET`, or derived from the signing key when unset). Nothing is stored, so any pod can answer a tab request, provided all pods have the same key. Treat it as an opaque string; it is long. |
| Credentials | This service's own Keycloak client (`STAFF_API_CLIENT_ID`, client_credentials, token reused until shortly before expiry). Its service account needs a view-only role (e.g. Data Supervisor) on each `<registry>-registry-staff-portal` client; IAM turns it into `register:view`. A registry on another Keycloak sets `staff_api.auth`. |
| Load | One pooled HTTP client; at most `STAFF_API_MAX_CONCURRENCY` calls in flight per registry (per worker); a call that waits longer than `STAFF_API_QUEUE_TIMEOUT_SEC` fails that registry with `registry_busy`; `STAFF_RATE_LIMIT_PER_MINUTE` per staff user (per worker, `429`); list sections paged (`page`, `pageSize`). |
| Audit | One JSON log line per search (`"event": "staff_search"`: staff user, roles, masked beneficiary, registries, purpose, outcome and records found) and per tab opened (`staff_tab_view`), on the logger `app.audit`. The registries only see this service's service account, so these lines are the record of who looked: ship them to the log store. |

Search request (`registryFilter` and `purpose` optional):

```json
{ "foundationalId": "2222444466668888", "registryFilter": ["CROPSOWN_REGISTRY"], "purpose": "field_verification" }
```

Response (`200`), shortened:

```json
{
  "schemaVersion": "1",
  "searchId": "eyJleHAiOjE3NjAyNzEyNDIs....Xq3vR8...",
  "foundationalId": "2222444466668888",
  "searchedAt": "2026-10-12T11:30:42+00:00",
  "expiresAt": "2026-10-12T12:00:42+00:00",
  "searchedBy": "staff",
  "registries": [{
    "registryCode": "CROPSOWN_REGISTRY", "registryName": "Crop Sown Registry", "status": "found",
    "records": [{
      "register": "CropSown", "registerId": "6b06a95a-...", "recordId": "cossx-cs-abeba-0001",
      "summary": {
        "functionalId": "REG/S1/2026/90011", "name": "Abeba Tadesse REG/S1/2026/90011",
        "linkFoundationalId": "FAN-2222444466668888",
        "displayFields": [{"name": "region_name", "value": "Oromia", "order": 2}]
      },
      "tabs": [{
        "tabId": "cropsown_sowing_tab", "label": "cropsown_sowing_tab", "order": 30,
        "request": {"method": "POST", "url": "/aggregation/v1/staff/registry/tab",
                    "body": {"searchId": "eyJleHAiOjE3NjAyNzEyNDIs....Xq3vR8...", "registryCode": "CROPSOWN_REGISTRY",
                             "registerId": "6b06a95a-...", "recordId": "cossx-cs-abeba-0001",
                             "tabId": "cropsown_sowing_tab"}}
      }]
    }]
  }]
}
```

Tab request: the tab's `request.body`, optionally with `page` (1-based) and `pageSize`
(default `STAFF_TAB_PAGE_SIZE`, capped at `STAFF_TAB_MAX_PAGE_SIZE`). Response, shortened:

```json
{
  "schemaVersion": "1",
  "searchId": "eyJleHAiOjE3NjAyNzEyNDIs....Xq3vR8...", "registryCode": "CROPSOWN_REGISTRY", "registerId": "6b06a95a-...",
  "recordId": "cossx-cs-abeba-0001", "tabId": "cropsown_sowing_tab", "label": "cropsown_sowing_tab",
  "sections": [{
    "sectionId": "cropsown_sowing_details_section_01", "mnemonic": "cs_sowing_details",
    "label": "Sowing Details", "order": 10, "isList": true, "registerId": "ee41a5f0-...",
    "fields": [{"key": "sowing_date", "label": "Sowing Date", "type": "date", "order": 0}],
    "records": [{"sowing_date": "2026-06-10", "...": "..."}],
    "pagination": {"page": 1, "pageSize": 50, "totalItems": 1, "totalPages": 1}
  }]
}
```

`label`s are the registry's own (a tab label is often an i18n key the client translates).
`fields` come from the section's UI schema; `options` are listed for fixed choices. Records are
what the staff portal shows (the staff-api's own fields); the catalog's `scopes` / `fields`
filter applies to the partner path only. A client renders what it gets and ignores properties
it does not know: within `schemaVersion` "1" changes only add.

Search errors per registry: `status: error` with `error.reason` (`staff_api_forbidden`,
`unreachable`, `unknown_register`, `registry_busy`); one registry failing does not fail the
others. Other errors: `400 unknown_registry` (a `registryFilter` code without `staff_api`),
`400` for a body with unknown properties, `429 rate_limited`, `503 staff_search_not_configured`
(no registry has `staff_api`). Tab errors: `400 unknown_registry` / `record_not_in_search` /
`unknown_tab`, `404 search_not_found`, `410 search_expired`, `502` with the registry's reason.

Postman: [postman/OpenG2P-Aggregator-Staff.postman_collection.json](postman/README.md#staff-search-collection)
(search per registry, every tab per registry, every tab the search returned, paging, refusals).

## Consent flow

1. **Consent request (partner, in the CM).** The partner raises a consent request in the CM
   (`POST /consent/v1/consent-requests`) for the subject, a purpose and the scope ids it wants
   (`GET /aggregation/v1/registries` lists them). The CM sends the subject a one-time code by
   SMS.
2. **One-time code (partner, in the CM).** The subject gives the partner the code; the partner
   confirms it with the CM, and the CM records the subject's consent (the grant) for this
   partner. This service is not involved in either step and holds no OTP.
   Steps 1 and 2 need changes in the CM that are not done yet (the OTP sent on raise, and a
   partner-facing "validate OTP" that also approves):
   [docs/CM-API-CONTRACT.md](docs/CM-API-CONTRACT.md#needed-in-the-cm-not-done-yet).
3. **Search (partner, here).** The partner signs a consent object for the subject and calls
   `POST /dci/registry/sync/search` (or the async route). The object is validated by the CM's
   `/consent/v1/validate`: signature (against Partner Management), policy ceiling, replay
   window, and the subject's grant from step 2. The CM answers with what the consent object
   asks for, what the partner's policy allows and what the subject granted.
   `foundationalId` must be the consent's subject.
4. **No consent.** If the subject has not consented to this partner, the CM answers
   `no_subject_consent` and this service answers `403 no_subject_consent` with a detail that
   says what to do (steps 1-2, then a new consent object). Nothing is raised or parked here,
   and no registry is called.
5. **Internal partners** (policy lawful basis `legitimate_interest`): no subject consent is
   sought; the partner's policy ceiling is the whole of the authority.
6. **Fan-out.** All registries in parallel, each only for the scopes the permit covers: a
   signed DCI search by `foundationalId`, an exact identifier check, projection to the
   allowed fields, and mapping into the response. A registry failing is that registry's
   warning, not the whole call's.
7. **Withdrawal.** Every call is validated afresh, so a consent withdrawn in the CM stops the
   next search. On the async route the CM is asked again right before the callback; a
   withdrawn consent gets a rejected on-search instead of the data. Every check fails closed:
   if the CM cannot answer, no data moves.

## Configuration

### Environment

Every setting is `AGGREGATION_LAYER_<NAME>`; the full list with comments is
[`config.py`](backend/src/openg2p_aggregation_layer/config.py), and
[`deploy/.env.example`](deploy/.env.example) is a working starting point. The most important:

| Variable | Purpose |
|---|---|
| `REGISTRY_CATALOG_PATH` | **Required.** Path to the registry catalog YAML. Invalid or missing = the service does not start. |
| `SIGNING_P12_PATH` (+ `_PASSWORD`) or `SIGNING_PRIVATE_KEY_PEM`, `SIGNING_KID` | This service's signing key; its public half is registered in Partner Management. Every pod needs the same key. |
| `AGGREGATOR_SENDER_ID`, `AGGREGATOR_ISSUER` | DCI `sender_id` / consent `iss` on registry hops. `sender_id` must map to the PM partner (`PARTNER_<SENDER_ID>`). |
| `CM_BASE_URL`, `CM_TOKEN_URL`, `CM_CLIENT_ID`, `CM_CLIENT_SECRET` | The Consent Manager and the client-credentials login to it (optional today: the two CM APIs used are open). |
| `AGGREGATOR_REGISTRY_TIMEOUT`, `AGGREGATOR_PAGE_SIZE` | Defaults for registry hops (a catalog entry may override). |
| `AGGREGATOR_CONSENT_VALIDITY_SEC` | Validity of each registry hop's consent object. |
| `AGGREGATOR_CALLBACK_TIMEOUT` | Async route: timeout of the one callback attempt. |
| `AUTH_ENABLED`, `AUTH_ISSUER`, `AUTH_JWKS_URL`, `AUTH_AUDIENCE` | Verification of the staff bearer token (Keycloak realm), staff routes only. The partner routes are authenticated by the consent object. |
| `STAFF_SEARCH_ROLES` | Roles (JSON list) a staff caller needs for the staff search. Empty closes it. |
| `STAFF_API_TOKEN_URL`, `STAFF_API_CLIENT_ID`, `STAFF_API_CLIENT_SECRET`, `STAFF_API_TIMEOUT` | This service's client-credentials login to the registries' staff-apis. |
| `STAFF_API_METADATA_TTL_SEC`, `STAFF_API_MAX_CONCURRENCY`, `STAFF_API_QUEUE_TIMEOUT_SEC` | Staff-api metadata cache; per-registry concurrency limit and how long a call may wait for it. |
| `STAFF_SEARCH_TTL_SEC`, `STAFF_TAB_PAGE_SIZE`, `STAFF_TAB_MAX_PAGE_SIZE`, `STAFF_RATE_LIMIT_PER_MINUTE` | How long a search opens tabs; list paging; per-user limit (0 = off). |
| `STAFF_SEARCH_TICKET_SECRET` | HMAC key of the staff `searchId` ticket. Empty = derived from the signing key. All pods need the same key. |

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
        strip_prefixes: ["FAN-"]         # optional: same number with or without the prefix
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
   first search.
5. **Restart** this service so it loads the new catalog. `GET /aggregation/v1/registries`
   now lists the registry and its scope ids.
6. **Verify.** `python postman/agg-prep.py` (a consent covering the new scope ids), then
   `python scripts/stack-check.py`: it reports every catalog registry as matched, empty, or
   with the warning that explains why not.

Partners then include the new scope ids in their consent requests (in the CM) and in their
consent objects.

## Running locally

[docs/RUN-LOCAL.md](docs/RUN-LOCAL.md) has the full walk-through. In short:

```bash
python scripts/register-aggregator.py --partner <partner-audience>   # Keycloak, key, PM, CM bindings, deploy/.env
docker compose -p aggregation-layer -f deploy/docker-compose.yml up -d --build
curl http://localhost:8110/ping
python postman/agg-prep.py && python scripts/stack-check.py
```

There is no database or broker to start and nothing to migrate. `deploy/docker-compose.yml`
runs the service and, for the async route, a dev callback receiver (`agg-callback`, :9099),
and mounts `deploy/registries.yaml` at `/config/registries.yaml`.
The image installs [openg2p-fastapi-common](https://github.com/OpenG2P/openg2p-fastapi-common)
from source (`FASTAPI_COMMON_REF` build argument).

## Tests

```bash
pip install -e backend pytest jsonschema

pytest test/unit                                           # no services needed
CM_SRC=../consent-management bash test/e2e/run.sh          # real CM (+ its Postgres), fakes around them
python postman/agg-prep.py && python scripts/stack-check.py   # live stack: A sync, B async, C never asked
```

| Suite | Covers |
|---|---|
| `test/unit/test_registry_catalog.py` | catalog loading; every class of invalid catalog fails with a clear message |
| `test/unit/test_projection.py` | the field-level filter |
| `test/unit/test_identifier_match.py` | the exact identifier check on every record a registry returns |
| `test/unit/test_bene360_mapping.py` | record → registers / tables / joins, warnings, timeframe |
| `test/unit/test_bene360_schema.py` | generated requests and responses against the published bene-360 schemas |
| `test/unit/test_fourth_registry.py` | a registry added only through a catalog fixture is queried and mapped |
| `test/unit/test_subject_checks.py` | the query's beneficiary is the consent's subject; what the CM's permit covers; `consent_object_reused` |
| `test/unit/test_sync_async.py` | sync answer in the response, `no_subject_consent` and other denials, async ack + callback, a withdrawn consent or an unanswering CM before the callback, the audit line |
| `test/unit/test_staff_search.py` | `staff_api` catalog block, exact match of staff-api hits, search summary + tabs, tab sections / fields / paging, metadata cache and new tabs, tab bound to its search (signed ticket), rate limit, audit, one registry failing |
| `test/e2e` | the whole flow against a real CM: guard rails, sync, async + callback, never asked → consent in the CM → search again, consent withdrawn before the async callback |

## Security notes

- **Consent is checked by the CM before any registry call**, on every call, and every check
  fails closed: an unreachable CM stops the release; on the async route the consent is checked
  again right before the callback.
- **The beneficiary is the consent's subject.** A query for any other `foundationalId` is
  refused, so a consent for one person never fetches another.
- **The one-time code is between the subject, the partner and the CM.** This service never
  sees or stores it. The CM must run with `subject_consent_required=true`, otherwise
  `/validate` permits a consent-basis partner on its policy ceiling without any subject
  consent.
- **Data minimisation:** only catalog-listed fields leave the service; consent is enforced per
  block by the CM and the registry. Registries never see the partner, only this service's
  binding.
- **Signing:** registry hops and on-search answers are signed with this service's own key
  (configure a persistent key, the same on every pod; without one an ephemeral key is
  generated and every signature is rejected downstream). The partner's envelope `signature`
  is not verified; the partner is authenticated by its consent object, which the CM verifies
  against Partner Management.
- **Callback URL:** the async route POSTs the data to whatever `header.sender_uri` says, after
  the consent check. Restrict outbound traffic at the network level if partners' callback
  hosts are known.
- **CM service account:** the two CM APIs this service uses are open partner APIs; the
  `aggregation-layer` client needs no CM role for them.
- **Personal data at rest:** none in this service. The audit lines carry masked subject IDs
  and per-registry outcomes, never attribute values.
- **Staff search has no consent check.** Access rests on the staff role
  (`STAFF_SEARCH_ROLES`) and on the view-only role of this service's staff-api account; every
  search and every tab opened is written to the audit log (`staff_search`, `staff_tab_view`).
  A tab opens only for a record the same user's recent search returned (the `searchId`
  ticket is signed; keep `STAFF_SEARCH_TICKET_SECRET` or the signing key secret). Give the
  role only to staff the policy allows, and treat `STAFF_API_CLIENT_SECRET` as read access to
  every registry.
- The registry catalog holds no secrets; secrets belong in the environment.

## Repository layout

```
backend/   FastAPI service (python -m openg2p_aggregation_layer.main); no database, no worker
             registry_catalog.py   catalog model, validation, field-level filter
             bene360.py            bene-360 request model and response mapping
             services/             aggregator (sync / async), CM client, registry client, staff search
deploy/    Dockerfile, docker-compose (service + dev callback receiver),
           registries.yaml (catalog), .env.example
docs/      CM API contract, local run guide, split plan (historical)
postman/   Collections (partner, staff), environment template, agg-prep.py, callback receiver
scripts/   register-aggregator.py, stack-check.py
test/      unit and end-to-end tests; fixtures (bene-360 schemas, catalogs)
```

## License

[MIT](LICENSE)
