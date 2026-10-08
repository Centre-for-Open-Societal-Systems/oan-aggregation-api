# Split plan: Aggregation Layer out of Consent Management

Status: **revised** (2026-10-08). The first plan (approved 2026-10-07) added five
aggregation-specific APIs to the Consent Manager (CM PR #2: `/grants`, `/granted-scopes`,
`/partners/by-audience`, extra `/validate` fields, a CM → AL webhook). That PR is closed.
The decision now is:

- **The CM contains zero aggregation-specific code.** The in-CM aggregator is removed
  (consent-management branch `feature/remove-aggregator`).
- **The Aggregation Layer keeps all its own state in its own Postgres DB** and uses only the
  CM's generic, pre-existing APIs (docs/CM-API-CONTRACT.md).

## 1. Target picture

```
Partner ──► Aggregation Layer ──► Consent Manager (generic APIs only)
             │   own DB:            validate, consent-requests, consent status,
             │   aggregation_requests, partner policy / list
             │   aggregation_grants ◄── poll: request approved/denied, consent withdrawn
             │
             ├──► every registry in the registry catalog (deploy/registries.yaml)
             │      POST /dci/registry/sync/search (unchanged; each hop validated by the
             │      CM against an AL binding on lawful_basis legitimate_interest)
             └──► Partner callback (DCI on-search carrying a Beneficiary-360 response)
```

## 2. What lives in this repo

| Piece | Notes |
|---|---|
| `services/aggregator_service.py`, `controllers/aggregator_controller.py` | `POST /dci/registry/async/search` (Beneficiary-360 query) and the service's own `/aggregation/v1/...` routes (see README) |
| `services/cm_client.py` | The only place that talks to the CM |
| `services/cm_poller.py`, `AggregatorService.sync_with_cm` | Reads approval / denial / withdrawal back from the CM |
| `models/aggregation.py` (`aggregation_requests`), `models/grant.py` (`aggregation_grants`) | Own DB. The grant table replaces the per-registry grant the CM used to record |
| `registry_catalog.py`, `bene360.py`, `services/registry_client.py` | The registry catalog (YAML, config only), the bene-360 mapping + field-level filter, and the registry hops, signed with the aggregator's own key |
| `kafka_bus/`, `worker.py`, `reap.py` | Fan-out + callback retry queue; worker and reaper also poll the CM |
| `services/otp_*.py`, `utils/fayda_otp.py` | Copied from the CM (the CM keeps its own copy for the consent screen) |

## 3. What stays in Consent Management (unchanged behaviour)

- Validation / PDP (`/consent/v1/validate`), incl. the subject-consent check (B8) and the
  `legitimate_interest` lawful basis.
- Consent requests, approve / deny / revoke, the OTP on the consent screen,
  `required_auth_method`.
- My consents portal + grouping (consent ← access records), Decisions, receipts, AWE.

## 4. Behaviour that changed with this revision

1. **Registry hops run on `legitimate_interest`.** The CM no longer records a grant per
   registry for the aggregator; the AL→registry bindings skip B8 and are capped at their
   policy ceiling. The subject's consent + OTP are enforced by the Aggregation Layer before
   any registry call (`aggregation_grants`, consent status check).
2. **Approval / withdrawal are polled**, not pushed. Effect within
   `AGGREGATION_LAYER_CM_POLL_INTERVAL_SEC`; a withdrawal is also checked right before the
   fan-out and before the callback.
3. **Raised consent must be approved within the CM replay window** (300s by default) of the
   partner object's `issued_at`; later, the aggregation is rejected
   (`consent_approved_after_replay_window`) and the partner re-seeks.
4. **My consents** no longer groups per-registry grants under the consent (there are none in
   the CM); each hop's embedded artefact on the AL binding is a separate row.
5. **Service account role.** The `aggregation-layer` Keycloak client needs
   `CONSENT_MANAGER_ADMIN` (the CM's policy read and partner list require it).
6. **Beneficiary-360 contract, config-only registries** (2026-10-08). The partner's
   query is an OpenG2P bene-360 request and the callback carries a bene-360 response.
   Partners consent to scope ids `<registryCode>.<block>` instead of field aliases;
   the per-field filter moved from code (`field_catalog.py`, removed) to the registry
   catalog. Every registry, binding and path now lives in `deploy/registries.yaml`:
   adding one is configuration plus `scripts/register-aggregator.py`.

## 5. Steps

1. ~~Copy the aggregator here~~ (done, PR #2 of this repo).
2. ~~Add 5 CM APIs~~ — dropped; CM PR closed.
3. Switch the aggregator to generic CM APIs + own grants + polling (branch
   `feature/standalone-db`). Verified by `test/e2e/run.sh` against CM `develop` minus the
   aggregator: real Postgres, both services, fake PM / registry / callback.
4. Remove the in-CM aggregator (consent-management branch `feature/remove-aggregator`).
5. Re-run `scripts/register-aggregator.py` (bindings → `legitimate_interest`, AWE approval,
   60s policy cache), then `scripts/stack-check.py` and the Postman collection on the stack.
6. Beneficiary-360 request/response + registry catalog (this repo, `develop`). Partner
   bindings must allow the new scope ids: `scripts/register-aggregator.py --partner <aud>`.
