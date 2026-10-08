# Consent Manager APIs the Aggregation Layer depends on

The Aggregation Layer never reads or writes the Consent Manager (CM) database. Every call
goes through `services/cm_client.py`. This is the contract the CM has to meet.

Auth for all calls: a Keycloak client-credentials token for the client `aggregation-layer`,
which holds a CM service role (proposed name `CONSENT_MANAGER_SERVICE`).

## Already in the CM, used as is

| Call | Used for |
|---|---|
| `GET /consent/v1/partners/{partner_id}/policy` | `required_auth_method` (is an OTP needed). 404 = no policy, the OTP is kept. |
| `POST /consent/v1/consent-requests` | Raise the consent the farmer was never asked for. Body: `subject_id`, `partner_id`, `purpose`, `requested_scopes`. Response must carry `id`. |

Both need to accept the service role (today they take the admin / subject role).

## New in the CM (5)

### 1. `POST /consent/v1/validate` — add two fields to the decision

Same request and response as today, plus:

```json
{ "partner_id": "<cm partner uuid>", "partner_audience": "<aud>" }
```

Replaces reading `ConsentArtefact` + `Partner` after the decision.

### 2. `GET /consent/v1/partners/by-audience/{audience}`

```json
{ "id": "...", "audience": "...", "controller_id": "..." }
```

404 when there is no binding. Replaces `select(Partner).where(audience=...)`.

### 3. `POST /consent/v1/grants`

Records what the farmer did and one originated grant per registry binding. This is the old
`_mint_grants` + `_reusable_grant`, moved into the CM because the rows are the CM's.

Request:

```json
{
  "aggregation_id": "...",
  "subject_id": {"type": "functional_id", "value": "7615076397"},
  "issuer": "aggregation-layer",
  "auth_method": "otp | consent | none",
  "auth_timestamp": "2026-10-07T10:00:00+00:00",
  "lawful_basis": "consent | legitimate_interest",
  "otp_channel": "PHONE",
  "purpose": {"code": "loan_origination"},
  "valid_until": "2026-10-07T10:05:00+00:00",
  "reuse_min_remaining_sec": 150,
  "bindings": [
    {"registry": "farmer", "audience": "aggregation-layer-farmer",
     "scopes": ["farmer_personal_details"]}
  ],
  "consent_request_id": "<the request the aggregator raised, if any>",
  "root_consent_id": "<consent_id /validate returned at seek, if consent was already held>"
}
```

`consent_request_id` / `root_consent_id` are stored in the AuthContext claims and are how
**My consents** groups the registry grant under the consent it came from (the CM can no
longer read `aggregation_requests` for that).

CM behaviour:
- One `AuthContext` (`consent_request_id` = `aggregation_id`, `auth_provider` = `issuer`,
  `token_validated` = false, `verified_claims` carries `auth_method`, `lawful_basis`,
  `otp_channel`, `aggregation_id`, `subject_id_value`). Always written.
- Per binding: look up the partner by `audience`; none → `skipped`.
  Reuse the newest active originated grant only if scopes, purpose, `auth_method` and
  `lawful_basis` match exactly, its AuthContext came from `issuer`, and at least
  `reuse_min_remaining_sec` is left. Otherwise write a new active originated
  `ConsentArtefact` (`fetch_type` = `oneshot`).

Response:

```json
{ "auth_context_id": "...", "minted": ["farmer"], "reused": [],
  "skipped": [{"registry": "livestock", "reason": "unknown_audience"}] }
```

The `aggregation_id` in the AuthContext is what lets **My consents** keep grouping the
per-registry grants under one consent without reading `aggregation_requests`.

### 4. `GET /consent/v1/consent-requests/{id}/granted-scopes`

For an approved request:

```json
{ "status": "approved", "partner_id": "...",
  "subject_id": {"type": "...", "value": "..."},
  "granted_scopes": ["farmer.firstname", "farmer.lastname"],
  "otp_verified_at": "2026-10-07T10:00:00+00:00",
  "otp_channel": "PHONE", "otp_provider": "fayda" }
```

`granted_scopes` = `effective_data_scopes` of the newest active originated artefact for
(partner, subject). 404 when the request does not exist.

### 5. Events: CM → `POST {aggregation_layer}/aggregation/v1/cm-events`

Replaces the two in-process calls (`lifecycle_service` → `release_for_consent_request`,
`consent_service.withdraw` → cancel `aggregation_requests`).

```json
{ "event_id": "uuid", "type": "consent_request.approved",
  "occurred_at": "...", "data": {"consent_request_id": "..."} }

{ "event_id": "uuid", "type": "consent.withdrawn",
  "occurred_at": "...",
  "data": {"consent_id": "...", "partner_id": "...",
           "subject_id": {"type": "...", "value": "..."}} }
```

- Send `consent.withdrawn` only when no other live consent to the same partner remains
  (the CM already computes `still_consented`).
- Header `X-CM-Signature: t=<unix seconds>,v1=<hex HMAC-SHA256 of "<t>.<raw body>">`,
  shared secret = `AGGREGATION_LAYER_CM_EVENTS_HMAC_SECRET`.
- Retry on non-2xx (503 means the aggregator could not reach the CM for its follow-up read).
  Both handlers are idempotent. Unknown event types are acknowledged and ignored.
- Kafka can replace the webhook later; the payload stays the same.

## Where it is implemented

consent-management branch `feature/aggregation-layer-apis`:
`schemas/verification.py` + `services/verification_service.py` (#1),
`controllers/partner_controller.py` + `services/partner_service.py` (#2, policy read opened to the service role),
`controllers/aggregation_layer_controller.py` + `services/aggregation_layer_service.py` (#3, #4),
`services/aggregation_events.py`, called from `lifecycle_service.approve` and `consent_service` withdraw (#5),
`services/consent_service.py` `_classify` (grouping from the AuthContext claims),
`auth.py` `require_any_role`, `config.py` (`auth_service_role`, `aggregation_layer_events_*`).

Mounting: #3/#4 are on the **partner** API audience, #2 and the policy read on **staff**,
raising a consent request on **beneficiary**. With `api_audience=all` it is one URL.

Verified by `test/e2e/run.sh` (both services, real Postgres, fake PM/registry/callback): 19/19.

## Config the CM needs

| Setting | Value |
|---|---|
| Aggregation Layer events URL | `http://<aggregation-layer>:8100/aggregation/v1/cm-events` |
| Events HMAC secret | same as the aggregation layer |
| Service role on the `aggregation-layer` Keycloak client | for calls 2–4 + the two existing ones |
