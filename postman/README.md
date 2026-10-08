# Postman — superseded

The aggregated flow is no longer a separate collection. It is **folder 10** of the
main one:

```
E:\Komal\OAN_ETHOPIA\OPENG2P\postman\OpenG2P-API-Only.postman_collection.json
```

11 folders, 81 requests: auth, partner onboarding, policy, AWE approval, consent
lifecycle, per-registry DCI search, and then the aggregated fetch as the finale.
One environment, one refresh button — `0 Auth > Mint consent objects` mints the
three per-registry consent objects **and** the cross-registry one.

See [`../../postman/README.md`](../../postman/README.md), section *Folder 10*.

## What is still used here

`callback_receiver.py` — a stand-in partner endpoint, so the callback half of the
flow is observable. The aggregator POSTs its `on-search` envelope to whatever
`sender_uri` says; in a test there is nobody on the other end.

```bash
docker run -d --name agg-callback --network openg2p-developer_default \
  -p 9099:9099 -v ~/agg-cb:/cb:ro consent-manager-backend python /cb/callback_receiver.py

curl http://localhost:9099/last    # the most recent envelope
curl http://localhost:9099/all     # every one
```

## What is left here and should not be used

`OpenG2P-Aggregator.postman_collection.json`, its environment, and `agg-prep.py`
were the standalone version. They still work against `:8100`, but they duplicate
folder 10 and have their own environment to keep fresh — which is exactly the
split the merge removed. Prefer the main collection.
