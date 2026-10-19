# Consent Manager APIs the Aggregation Layer depends on

The Aggregation Layer never reads or writes the Consent Manager (CM) database, and the CM
carries **no code for it**: every call below is a generic CM API that exists on CM `develop`
and that any partner-facing service could make. All calls go through
`services/cm_client.py`. Nothing is pushed from the CM to this service, and this service
keeps nothing: every consent decision is the CM's, asked again on every call.

The Aggregation Layer does not raise consent requests, issue or check one-time codes, poll
the CM or record grants. Getting the subject's consent is the partner's job, done in the CM
before the partner calls this service (see
[below](#what-the-partner-does-in-the-cm-before-calling-the-al)).

## Calls

| Call | CM API audience / auth | Used for |
|---|---|---|
| `POST /consent/v1/validate` | partner, none | The partner's consent object, on every search (signature against Partner Management, policy ceiling, replay window, subject grant), with `requested_scopes` = the scope ids of the registries the query covers. A permit gives `consent_id`, `subject_id`, `effective_data_scopes`, `lawful_basis`. A deny gives `reason_code`; `no_subject_consent` is answered to the partner as `403 no_subject_consent` with a hint to raise a consent request. The registries call the same API on every hop. |
| `GET /consent/v1/consents/{consent_id}/status` | partner, none | `active` / `revoked` / `expired` of the `consent_id` above. Asked on the async route only, right before the callback: anything but `active` turns the callback into a rejected on-search (`consent_withdrawn`); a CM that cannot answer, into `consent_check_failed`. The CM revokes this record with the subject's consent (cascade on withdraw), so this is how a withdrawal is seen. |

With `api_audience=all` it is one base URL; split deployments need the partner API
reachable from this service.

Auth: both calls are open partner APIs today. The service still sends a Keycloak
client-credentials token for the client `aggregation-layer` when `CM_TOKEN_URL` is set; the
service account needs no CM role for these two calls. (`scripts/register-aggregator.py`
still gives it `CONSENT_MANAGER_ADMIN`, which this service no longer uses; it can be
dropped.)

## What the partner does in the CM before calling the AL

1. **Raise a consent request.** `POST /consent/v1/consent-requests` with the subject, the
   partner, the purpose and the scope ids (`<registryCode>.<block>`, from
   `GET /aggregation/v1/registries`). The CM generates a one-time code and sends it to the
   subject (farmer) by SMS.
2. **Confirm the code.** The subject tells the partner the code; the partner submits it to
   the CM. On a correct code the CM records the subject's consent (the grant) for the scopes
   asked for, and returns the consent id.
3. **Call the AL.** The partner signs a consent object for that subject
   (`authorize.consent_jws`) and calls `POST /dci/registry/sync/search` (or the async
   route). The AL's `/validate` call finds the grant from step 2 and permits.

Without step 2 the AL answers `403 no_subject_consent` and calls no registry.

### Needed in the CM, not done yet

The CM's consent-request routes were built for the subject's own consent screen. For the
flow above, three changes are needed in the CM (none of them in this repository):

1. **Send the OTP when the request is raised.** Today `POST /consent/v1/consent-requests`
   (Keycloak service token) only creates the request; the OTP is a separate
   `POST /consent-requests/{id}/otp`, a subject-only step (refused for a partner token when
   `bene_auth_required`). Needed: issue and send the OTP when the request is created, when the
   partner's policy has `required_auth_method=otp`.
2. **A partner-facing "validate OTP" that approves.** Today
   `POST /consent-requests/{id}/verify-otp` only records the authentication (a subject-only
   step) and `/approve` is a separate subject step. Needed: one partner-facing call that, on a
   correct code, verifies **and** approves (grants the requested scopes) and returns the
   consent id. It must check that the request belongs to the calling partner.
3. **Run with `subject_consent_required=true`.** Otherwise `/validate` permits a
   consent-basis partner on its policy ceiling without any subject consent, and step 2 is
   never needed.

Until 1 and 2 exist, the steps can be done with the subject's routes as they are
(`/otp`, `/verify-otp`, `/approve`; `postman/agg-prep.py`, `scripts/stack-check.py` and
`test/e2e` do this).

## Scopes

The CM treats data scopes as opaque strings, so two vocabularies coexist without any CM
change:

- **Partner → Aggregation Layer bindings** carry scope ids `<registryCode>.<block>`
  (e.g. `FARMER_REGISTRY.farmer_personal_details`), one per top-level block of a
  registry's outgest template, as listed by `GET /aggregation/v1/registries`. This is what
  the subject consents to and what the partner's consent object names.
- **Aggregation Layer → registry bindings** carry the registry's own block names
  (`farmer_personal_details`), which is what the registry clamps its record on.

The mapping between the two, and the per-field filter inside each block, is the registry
catalog (`deploy/registries.yaml`).

The CM answers a consent object once: its first `/validate` records the permit, and every
later `/validate` of the same object returns that record. A second search with the same
object for other registries is answered `409 consent_object_reused`; the partner signs a new
object.

## The registry hop

The registries still validate every hop through CM `/validate`. The aggregation layer's
registry bindings (one per catalog entry, `binding.audience` / `binding.controller_id`)
are on lawful basis **`legitimate_interest`**: the CM skips the subject-grant check for them
and caps each hop at the binding's policy ceiling. The subject's consent is checked by the
AL before the hop, through the CM's `/validate` of the partner's object; a registry is
called only for scopes that permit covers. The query's `foundationalId` must equal the
consent's `subject_id.value` (`403 subject_mismatch` otherwise), because the hop's CM check
is on the aggregator's binding, not on the subject.

- Moving a binding from `consent` to `legitimate_interest` is a widening: with AWE enabled
  the new policy version is `pending` until approved (`scripts/register-aggregator.py`
  approves its own tasks).
- The CM caches a partner's policy for `partner_cache_ttl_sec` (60s by default): a binding
  change can take a minute to reach `/validate`.

## CM settings this relies on

| Setting | Why |
|---|---|
| `subject_consent_required=true` | Only then does `/validate` answer `no_subject_consent` for a partner without a grant. With `false` the CM permits a consent-basis partner on its policy ceiling and the subject is never asked. |
| `subject_consent_enabled=true` (default) | The narrowing of a permit to what the subject granted, and the cascade that makes `/consents/{id}/status` report a withdrawal. |
| `replay_freshness_window_sec` (default 300) | A consent object is accepted only within this window of its `issued_at`; the partner signs a new one per search. |

## Gaps

1. **The partner-side consent flow** (OTP on raise, validate-OTP-and-approve): see
   [Needed in the CM, not done yet](#needed-in-the-cm-not-done-yet).
2. **My consents.** The CM holds no grants for the aggregator; each registry hop leaves an
   embedded artefact on the aggregator's `legitimate_interest` binding, shown to the subject
   as its own top-level row.
