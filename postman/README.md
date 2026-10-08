# Postman — Aggregation Layer

`OpenG2P-Aggregator.postman_collection.json` + `OpenG2P-Aggregator.postman_environment.json`
drive the aggregated fetch against this service (`agg_url`, default http://localhost:8110)
and the Consent Manager (`cm_url`).

```bash
python postman/agg-prep.py      # signs a fresh partner consent object, writes the environment
```

The consent object is valid for the CM's replay window (300s by default): re-run
`agg-prep.py` when a seek answers `replay`. The same window applies to a consent the
aggregation layer raises for the farmer — approve it on the CM consent screen within 300s
of the object's `issued_at`, or the aggregation is rejected with
`consent_approved_after_replay_window` and the partner has to seek again.

Approval, denial and withdrawal on the CM are not pushed to the aggregation layer; it polls
the CM every `AGGREGATION_LAYER_CM_POLL_INTERVAL_SEC` (15s by default). Allow for that before
expecting the callback or a `rejected` status.

## Callback receiver

`callback_receiver.py` is a stand-in partner endpoint, so the callback half of the flow is
observable. The aggregation layer POSTs its `on-search` envelope to whatever `sender_uri`
says. `deploy/docker-compose.yml` runs it as `agg-callback` on :9099.

```bash
curl http://localhost:9099/last    # the most recent envelope
curl http://localhost:9099/all     # every one
```
