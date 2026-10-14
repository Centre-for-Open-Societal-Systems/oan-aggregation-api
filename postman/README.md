# Postman — Aggregation Layer

`OpenG2P-Aggregator.postman_collection.json` drives the Beneficiary-360 fetch against this
service (`agg_url`, default http://localhost:8110): catalog discovery, the seek, the
subject's OTP release, status, the delivered on-search, and the guard rails.

```bash
python postman/agg-prep.py      # signs a fresh partner consent object, writes the local environment
```

`agg-prep.py` writes `OpenG2P-Aggregator.local.postman_environment.json` (gitignored: it holds
a live token and consent object). `OpenG2P-Aggregator.postman_environment.json` is the empty
template it is based on. See the script's docstring for the partner / subject it uses
(`G2P_PARTNER_AUDIENCE`, `G2P_SUBJECT_USER`, ...).

The consent object is valid for the CM's replay window (300s by default): re-run
`agg-prep.py` when a seek answers `replay`. The same window applies to a consent the
aggregation layer raises for the subject — approve it on the CM consent screen within 300s
of the object's `issued_at`, or the aggregation is rejected with
`consent_approved_after_replay_window` and the partner has to seek again.

Approval, denial and withdrawal on the CM are not pushed to the aggregation layer; it polls
the CM every `AGGREGATION_LAYER_CM_POLL_INTERVAL_SEC` (15s by default). Allow for that before
expecting the callback or a `rejected` status.

## Callback receiver

`callback_receiver.py` is a stand-in partner endpoint, so the callback half of the flow is
observable. The aggregation layer POSTs its `on-search` envelope (a Beneficiary-360 response
in `reg_records[0]`) to whatever `sender_uri` says. `deploy/docker-compose.yml` runs it as
`agg-callback` on :9099.

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
