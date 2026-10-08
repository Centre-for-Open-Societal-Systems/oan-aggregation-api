"""Message shapes on the aggregation topics.

Every message is a CloudEvents 1.0 envelope, which is the event standard the
platform's Audit Manager already publishes in. Using the same shape means a
consumer, a bridge or a dead-letter inspector written for one is not a special
case for the other, and the ``id``/``subject``/``type`` triple is enough to
trace a single aggregation across all four topics without reading ``data``.

Keying
------
Every message is keyed by the **aggregation id**. Two consequences, both
wanted:

* Work spreads evenly over the partitions, which is the whole point — subjects
  verifying at the same moment must land on different workers.
* A retry of the same aggregation keys to the same partition as the original,
  so the attempts of one request are ordered with respect to each other even
  though requests are unordered with respect to each other.

Keying by partner instead would put a busy partner's whole traffic on one
partition and let it block everyone sharing it.
"""
import json
import uuid
from datetime import datetime, timezone
from typing import Any, Dict, Optional

SOURCE = "openg2p.consent-manager"

# The aggregation cleared its gates (consent, and the OTP if the policy asked
# for one) and is waiting for a worker to query the registries.
TYPE_FANOUT_REQUESTED = "org.openg2p.consent.aggregation.fanout.requested"

# The registries answered. ``data`` carries the signed on-search envelope,
# ready to POST — so a retry never re-queries a registry.
TYPE_DELIVERY_REQUESTED = "org.openg2p.consent.aggregation.delivery.requested"

# A delivery that failed retriably and is waiting out its backoff.
TYPE_DELIVERY_RETRY = "org.openg2p.consent.aggregation.delivery.retry"

# Terminal. Kept so an operator can see what was lost and why.
TYPE_DEAD_LETTER = "org.openg2p.consent.aggregation.dead_letter"


def envelope(event_type: str, aggregation_id: str, data: Dict[str, Any],
             *, event_id: Optional[str] = None) -> Dict[str, Any]:
    """Wrap ``data`` as a CloudEvent about one aggregation.

    ``event_id`` is settable so a retry can keep the id of the delivery it is
    retrying: the partner's callback then sees the same ``message_id`` twice
    rather than two apparently different results, which is what lets it
    de-duplicate an at-least-once delivery.
    """
    return {
        "specversion": "1.0",
        "id": event_id or uuid.uuid4().hex,
        "source": SOURCE,
        "type": event_type,
        "subject": aggregation_id,
        "time": datetime.now(timezone.utc).isoformat(),
        "datacontenttype": "application/json",
        "data": data,
    }


def serialize(event: Dict[str, Any]) -> bytes:
    return json.dumps(event, separators=(",", ":"), default=str).encode("utf-8")


def parse(raw: bytes) -> Dict[str, Any]:
    """Decode a message, raising ValueError on anything unusable.

    A consumer must be able to tell "this message is malformed" from "this
    message failed to process": the first is never worth retrying and goes
    straight to the dead-letter topic.
    """
    try:
        event = json.loads(raw.decode("utf-8"))
    except Exception as exc:  # noqa: BLE001
        raise ValueError("message is not JSON: %s" % exc) from exc
    if not isinstance(event, dict) or not event.get("subject"):
        raise ValueError("message is not a CloudEvent about an aggregation")
    if not isinstance(event.get("data"), dict):
        raise ValueError("message has no data object")
    return event


def key_for(aggregation_id: str) -> bytes:
    return str(aggregation_id).encode("utf-8")
