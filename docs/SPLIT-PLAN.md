# Split plan: Aggregation Layer out of Consent Management

Status: **approved** (2026-10-07). Steps 1 and the aggregator half of step 3 are done in this
repo; the CM additions (step 2) are next. Nothing has been removed from consent-management.

Source today: `Centre-for-Open-Societal-Systems/consent-management`, branch `main`, commit `260bb0e`
(aggregator, OTP, Kafka queue, consent grouping). The aggregator currently runs **inside** the
Consent Manager (CM) process and reads/writes CM tables directly.

## 1. Target picture

```
Partner ──► Aggregation Layer ──► Consent Manager      (validate, raise consent, record grant)
             │        ▲    │
             │        │    └──► Farmer / Livestock / Cropsown registries
             │        │          POST /dci/registry/sync/search  (unchanged)
             │        └── CM events: consent approved / withdrawn
             └──► Partner callback  (DCI on-search)
```

- The Aggregation Layer is its own service, its own database, its own signing key.
- It talks to the CM **only over HTTP APIs** and receives CM events over a webhook (or Kafka).
- Registries are not changed. They keep validating every hop with the CM.

## 2. What moves to this repo

| From `consent-management/backend/src/openg2p_consent_manager/` | Notes |
|---|---|
| `services/aggregator_service.py` | Rewritten to call the CM over HTTP instead of its tables |
| `controllers/aggregator_controller.py` | Same routes (`/dci/registry/async/search`, `/aggregation/...`) |
| `services/field_catalog.py` | As is |
| `services/registry_client.py` | Signs with the aggregator's **own** key, not the CM key |
| `models/aggregation.py`, `schemas/aggregation.py` | Table `aggregation_requests` moves to the aggregator DB |
| `kafka_bus/` (bus, consumers, topics), `worker.py`, `reap.py` | Fan-out + callback retry queue |
| `services/otp_provider.py`, `otp_publisher.py`, `otp_service.py`, `utils/fayda_otp.py` | **Copied**, see §3 |
| `postman/OpenG2P-Aggregator.*`, `postman/agg-prep.py`, `postman/callback_receiver.py`, `register-aggregator.py`, `docker-compose.kafka.yml`, `test/kafka/` | As is, endpoints updated |

## 3. What stays in Consent Management

- Validation / PDP (`/consent/v1/validate`), incl. the subject-consent check (B8) and the
  `legitimate_interest` lawful basis. **The Cropsown → Farmer lookup depends on this.**
- Consent requests, approve / deny / revoke, the OTP on the consent screen.
- My consents portal + grouping, Decisions, receipts, AWE policy approval.
- OTP code is needed on **both** sides (CM consent screen, aggregator release). It is copied,
  not shared; a common library can come later.

## 4. New things the CM must offer (the only CM code changes)

| # | CM API | Replaces (today: direct DB access) |
|---|---|---|
| 1 | `/consent/v1/validate` returns `partner_id` + `partner_audience` in the decision | Reading `ConsentArtefact` / `Partner` after validate |
| 2 | `GET /consent/v1/partners/by-audience/{aud}` (service role) | `select(Partner).where(audience=...)` |
| 3 | `POST /consent/v1/grants` (service role, aggregator only): record AuthContext + one originated grant per registry binding, reuse an identical live grant | `_mint_grants` writing `AuthContext` + `ConsentArtefact` |
| 4 | `GET /consent/v1/consent-requests/{id}/granted-scopes` (service role) | Reading the approved artefact after approval |
| 5 | Outbound event `consent_request.approved` and `consent.withdrawn` (webhook to the aggregator; Kafka optional) | `lifecycle_service` calling `release_for_consent_request`; `consent_service` cancelling in-flight aggregations |

Existing CM APIs reused as is: `POST /consent/v1/consent-requests` (raise consent),
`GET /consent/v1/partners/{id}/policy` (`required_auth_method`).

Service-to-service auth: a Keycloak client `aggregation-layer` with a CM service role.

## 5. Things that change behaviour (to agree on)

1. **My consents grouping.** Today the CM joins `aggregation_requests` to show one consent with
   its registry grants. After the split the CM keeps that grouping from its own data (the grant
   API stores the aggregation id in the AuthContext); the aggregation status is no longer shown there.
2. **Withdraw cascade.** Withdrawing a consent cancels in-flight fetches via the event in §4.5.
   Between the withdraw and the event there is a small window; the registry hop is still denied
   by the CM because the grant is already revoked.
3. **New partner identity.** The aggregator gets its own key + PM registration
   (`PARTNER_AGGREGATION_LAYER`) instead of signing with the CM key.

## 6. Steps (after confirmation)

1. Copy the files in §2 here, keep them running against the CM in-process mode → baseline tests.
2. Add the 5 CM additions (§4) in `consent-management` on a feature branch.
3. Switch the aggregator to HTTP + events; own DB; own key.
4. Run the existing tests: Postman folder 10/12, `verify-aggregated.py`, Kafka consumer test.
5. Only then remove the aggregator code from `consent-management` (separate PR).

Nothing is removed from `consent-management` until step 5 passes.
