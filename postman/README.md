# Postman — Aggregation Layer

`OpenG2P-Aggregator.postman_collection.json` drives the Beneficiary-360 fetch against this
service (`agg_url`, default http://localhost:8110):

| Folder | What |
|---|---|
| 0 Health & discovery | `/ping`, `GET /aggregation/v1/registries` |
| 1 Partner - sync search (default) | `POST /dci/registry/sync/search`: `200`, the signed on-search with the Beneficiary-360 response in `reg_records[0]`; saves `{{correlation_id}}` |
| 2 Partner - async search and callback | `POST /dci/registry/async/search`: `202` ack; then the on-search the callback receiver got |
| 3 Guard rails | subject mismatch (403), property bene-360 does not define (400), only PROGRAMS (422), registry not in the catalog (422), async without `sender_uri` (400) |
| 4 One registry only | optional: a sync search per registry (`registryFilter`); re-run `agg-prep.py` before each |

The subject's consent is obtained before any of this, in the Consent Manager: the partner
raises a consent request, the CM sends the subject a one-time code, the partner confirms it
with the CM. `agg-prep.py` does that part for you (as the subject, directly on the CM):

```bash
python postman/agg-prep.py              # active consent + a fresh partner consent object, writes the local environment
python postman/agg-prep.py --no-grant   # no consent: the searches answer 403 no_subject_consent
```

`agg-prep.py` writes `OpenG2P-Aggregator.local.postman_environment.json` (gitignored: it holds
a live token and consent object). `OpenG2P-Aggregator.postman_environment.json` is the empty
template it is based on. See the script's docstring for the partner / subject it uses
(`G2P_PARTNER_AUDIENCE`, `G2P_SUBJECT_USER`, ...).

The consent object is valid for the CM's replay window (300s by default), and the CM answers
it once: re-run `agg-prep.py` (and re-import the environment) when a search answers `replay`
or `409 consent_object_reused`.

## Callback receiver

`callback_receiver.py` is a stand-in partner endpoint, so the async route is observable. The
aggregation layer POSTs its `on-search` envelope (a Beneficiary-360 response in
`reg_records[0]`) to whatever `sender_uri` says, once, as soon as the registries have
answered (no retry). `deploy/docker-compose.yml` runs it as `agg-callback` on :9099.

```bash
curl http://localhost:9099/last    # the most recent envelope
curl http://localhost:9099/all     # every one
```

## Staff search collection

`OpenG2P-Aggregator-Staff.postman_collection.json` drives the staff search (no consent): one
search, then tab by tab, as a staff portal or mobile client would. See
[Staff search](../README.md#staff-search) for the API.

| Folder | What |
|---|---|
| 0 Login | staff token (Keycloak realm `staff`) |
| 1 Search | all registries, then Farmer, Livestock and Crop sown alone; saves `{{search_id}}` and per record `{{<registry>_<register>_register_id}}` / `_record_id` |
| 2 Farmer registry - tabs | one request per tab of the Farmer register |
| 3 Livestock registry - tabs | the Farmer register's tab, then each tab of the Livestock register |
| 4 Crop sown registry - tabs | one request per tab of the CropSown register |
| 5 Every tab from the search answer | sends each tab's `request.body` exactly as the search gave it, looping until none is left: no tab id written in the collection, so a tab added in a registry is covered too |
| 6 Paging a list section | `page` / `pageSize` |
| 7 Guard rails | record not from the search, unknown tab, unknown search, unknown registry, no token |

Folders 2-4 name the tab ids of the reference registries; a deployment that adds or renames
tabs answers `400 unknown_tab` there, and folder 5 is the one that follows the registry.

Set `staff_username` and `staff_password` (the user needs a role from
`AGGREGATION_LAYER_STAFF_SEARCH_ROLES`), or import
`OpenG2P-Aggregator-Staff.postman_environment.json` and fill every value. Run the folders in
order; folder 1 saves what the others need.

```bash
newman run postman/OpenG2P-Aggregator-Staff.postman_collection.json \
  --env-var staff_username=staff --env-var staff_password=<password>
```
