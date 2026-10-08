# Consent Manager APIs the Aggregation Layer depends on

The Aggregation Layer never reads or writes the Consent Manager (CM) database, and the CM
carries **no code for it**: every call below is a generic CM API that exists on CM `develop`
and that any partner-facing service could make. All calls go through
`services/cm_client.py`. Nothing is pushed from the CM to this service.

What the CM used to record for the aggregator (an AuthContext and a per-registry grant) is
now this service's own table, `aggregation_grants` (`models/grant.py`).

## Calls

| Call | CM API audience / auth | Used for |
|---|---|---|
| `POST /consent/v1/validate` | partner, none | The partner's consent object at seek (signature, policy ceiling, replay window, B8). A permit gives `consent_id`, `subject_id`, `effective_data_scopes`, `lawful_basis`. Called again with the same object when a raised consent request is approved, to learn what was granted. |
| `GET /consent/v1/consents/{consent_id}/status` | partner, none | `active` / `revoked` / `expired` of the `consent_id` above. The CM revokes it with the subject's consent (cascade on withdraw), so this is how a withdrawal is seen. |
| `POST /consent/v1/consent-requests` | beneficiary, any valid token | Raise the consent the farmer was never asked for. Response carries `id`. |
| `GET /consent/v1/consent-requests/{id}` | beneficiary, any valid token | `status` (pending / approved / denied / expired), `otp_verified_at`, `otp_channel` of a raised request. |
| `GET /consent/v1/partners/{partner_id}/policy` | staff, **CONSENT_MANAGER_ADMIN** | `required_auth_method` (is an OTP needed). 404 = no policy, the OTP is kept. |
| `GET /consent/v1/partners` | staff, **CONSENT_MANAGER_ADMIN** | Partner audience → CM partner id, only when `AGGREGATION_LAYER_CM_PARTNER_IDS` does not have it. Cached per process. |

With `api_audience=all` it is one base URL; split deployments need the partner, beneficiary
and staff APIs reachable from this service.

Auth: Keycloak client-credentials for the client `aggregation-layer`. Its service account
needs **CONSENT_MANAGER_ADMIN**, because that is the only role the CM's policy read and
partner list accept. That role can also edit policies; treat the client secret like an admin
credential (see "Gaps" below).

## How the facts the old CM additions gave are obtained now

| Fact | Before (CM PR #2, closed) | Now |
|---|---|---|
| Which partner sent the seek | `partner_id` / `partner_audience` added to the validate decision | `aud` of the partner's object (the CM looked the binding up by `aud` and verified the signature against that partner's key, so after a permit it is authoritative) → `AGGREGATION_LAYER_CM_PARTNER_IDS` or `GET /consent/v1/partners` |
| Partner by audience for a raised consent | `GET /partners/by-audience/{aud}` | same as above |
| Per-registry grant + reuse | `POST /consent/v1/grants` | `aggregation_grants` in this service's DB; reuse on exact match of partner, subject, registry, scopes, purpose, method, lawful basis and CM consent, with ≥ `aggregator_grant_reuse_min_remaining_sec` left |
| What the subject granted on a raised request | `GET /consent-requests/{id}/granted-scopes` | `POST /consent/v1/validate` again with the partner's stored object: the CM narrows it to the subject's grant (B8) |
| Consent request approved | CM → AL webhook `consent_request.approved` | poll `GET /consent/v1/consent-requests/{id}` |
| Consent withdrawn | CM → AL webhook `consent.withdrawn` | poll `GET /consent/v1/consents/{consent_id}/status`, plus a check right before the fan-out and before the callback |

The poll runs every `AGGREGATION_LAYER_CM_POLL_INTERVAL_SEC` (default 15s) in the API process
(`CM_POLL_IN_APP=true`) and/or `python -m openg2p_aggregation_layer.worker`, and once per
`python -m openg2p_aggregation_layer.reap` run. Parked rows are claimed before they are
worked on, so several pollers are safe.

## The registry hop

The registries still validate every hop through CM `/validate`. The aggregation layer's
registry bindings (`agg-layer-farmer` / `-livestock` / `-cropsown`) are on lawful basis
**`legitimate_interest`**: the CM skips the subject-grant (B8) check for them and caps each
hop at the binding's policy ceiling. The farmer's consent and OTP are enforced here, before
the call: no registry is called without an active `aggregation_grants` row, and not while
the CM reports the partner's consent as anything but `active`.

- Moving a binding from `consent` to `legitimate_interest` is a widening: with AWE enabled
  the new policy version is `pending` until approved (`scripts/register-aggregator.py`
  approves its own tasks).
- The CM caches a partner's policy for `partner_cache_ttl_sec` (60s by default): a binding
  change can take a minute to reach `/validate`.

## CM settings this relies on

| Setting | Why |
|---|---|
| `subject_consent_required=true` | Only then does `/validate` answer `no_subject_consent` for a partner without a grant, which is what triggers the raise-a-consent path. With `false` the CM permits on the policy ceiling and the farmer is never asked. |
| `subject_consent_enabled=true` (default) | The B8 narrowing that turns the re-validate into "what was granted", and the cascade that makes `/consents/{id}/status` report a withdrawal. |
| `replay_freshness_window_sec` (default 300) | A raised consent must be approved within this window of the partner object's `issued_at`, or the re-validate is refused: the row is rejected with `consent_approved_after_replay_window` and the partner seeks again (the CM then permits directly on the new grant). |

## Gaps (CM API not on `develop`; deliberately not added to the CM)

1. **Granted scopes / resulting consent of a consent request.** `GET /consent-requests/{id}`
   does not return the consent artefact it produced (id, `effective_data_scopes`). The
   workaround (re-validate the partner's object) only works inside the replay window.
   Options: (a) accept the window, partner re-seeks after it (current); (b) a generic CM
   change: add `consent_id` + `effective_data_scopes` of the approved artefact to
   `ConsentRequestResponse` — useful to any origination client, not aggregation-specific;
   (c) raise `replay_freshness_window_sec` (weakens replay protection for every partner).
2. **Least-privilege service role.** The policy read and partner list need
   `CONSENT_MANAGER_ADMIN`. Options: accept it; or a generic CM read-only role on those GETs;
   or set `AGGREGATION_LAYER_CM_PARTNER_IDS` (removes the list call) — the policy read still
   needs the role.
3. **Push instead of poll.** The CM has no generic outbound consent-event mechanism, so
   decisions take up to one poll interval to take effect here (a withdrawal still stops any
   fan-out or callback that has not started, via the pre-fan-out / pre-callback checks).
4. **My consents.** The CM no longer holds grants for the aggregator, so the per-registry
   grants are not listed under the farmer's consent; each registry hop leaves an embedded
   artefact on the aggregator's `legitimate_interest` binding, shown as its own top-level row.
