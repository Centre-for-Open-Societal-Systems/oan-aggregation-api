"""``authorize.consent_id``: a consent the CM holds (e.g. granted when the
subject's one-time code was verified), presented with the partner's
partner-realm token. The CM is a fake; partner tokens are signed with a test key."""
import asyncio
import time
from types import SimpleNamespace

import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import ec
from fastapi import HTTPException
from pydantic import ValidationError

from openg2p_aggregation_layer import auth
from openg2p_aggregation_layer.config import Settings
from openg2p_aggregation_layer.controllers import aggregator_controller
from openg2p_aggregation_layer.schemas.aggregation import SeekAuthorize, SeekEnvelope
from openg2p_aggregation_layer.services.aggregator_service import AggregationError
from test_sync_async import FID, QUERY, service  # noqa: F401 - fixture

_config = Settings.get_config()
ISSUER = "http://kc.test/realms/partner"
KEY = ec.generate_private_key(ec.SECP256R1())


class StoredCM:
    """The CM for a stored consent: permits only for the partner it was given to."""

    def __init__(self, owner="bank-a", subject=FID, purpose="credit-assessment"):
        self.owner, self.subject, self.purpose, self.calls = owner, subject, purpose, []

    async def validate(self, consent_jws, requested_scopes, purpose=None):
        raise AssertionError("consent_jws path not expected")

    async def validate_stored(self, consent_id, partner_id, requested_scopes, purpose=None):
        self.calls.append((consent_id, partner_id, requested_scopes))
        if partner_id != self.owner:
            return {"decision": "deny", "reason_code": "partner_mismatch",
                    "detail": "consent_partner_id is not the partner the consent was given to"}
        return {"decision": "permit", "consent_id": consent_id,
                "subject_id": {"type": "national_id", "value": self.subject},
                "purpose": {"code": self.purpose} if self.purpose else None,
                "effective_data_scopes": requested_scopes}

    async def consent_status(self, consent_id):
        return "active"


def _sync(svc, partner="bank-a"):
    return asyncio.run(svc.search_sync(
        consent_id="c-77", partner_id=partner, query=QUERY, transaction_id="t-1",
        reference_id="r-1", purpose=None))


# ── service ──────────────────────────────────────────────────────────────────

def test_stored_consent_answers_for_its_partner(service, four):
    service.cm = StoredCM()
    envelope = _sync(service)
    assert envelope["header"]["receiver_id"] == "bank-a"
    assert service.cm.calls == [("c-77", "bank-a", four.scope_ids(four.codes()))]


def test_stored_consent_of_another_partner_is_refused(service):
    service.cm = StoredCM(owner="bank-b")
    with pytest.raises(AggregationError) as exc:
        _sync(service)
    assert (exc.value.status, exc.value.reason) == (403, "partner_mismatch")


def test_stored_consent_for_another_subject_is_refused(service):
    service.cm = StoredCM(subject="9999999999")
    with pytest.raises(AggregationError) as exc:
        _sync(service)
    assert exc.value.reason == "subject_mismatch"


def test_async_checks_the_stored_consent_before_the_callback(service):
    service.cm = StoredCM()

    async def run():
        agg = await service.search_async(
            consent_id="c-77", partner_id="bank-a", query=QUERY,
            callback_url="http://partner.test/cb", transaction_id="t", reference_id="r",
            purpose=None)
        await asyncio.gather(*service._tasks)
        return agg

    agg = asyncio.run(run())
    assert agg.cm_consent_id == "c-77" and agg.partner_audience == "bank-a"
    assert service.posted and service.posted[0][1]["header"]["status"] == "succ"


# ── schema ───────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("body", [{}, {"consent_jws": "a.b.c", "consent_id": "c-1"}])
def test_exactly_one_consent(body):
    with pytest.raises(ValidationError):
        SeekAuthorize(**body)


def test_consent_id_alone_is_valid():
    assert SeekAuthorize(consent_id="c-1").consent_id == "c-1"


# ── partner token ────────────────────────────────────────────────────────────

@pytest.fixture
def partner_realm(monkeypatch):
    monkeypatch.setattr(_config, "partner_auth_issuer", ISSUER)
    monkeypatch.setattr(auth, "_partner_jwk_client", SimpleNamespace(
        get_signing_key_from_jwt=lambda t: SimpleNamespace(key=KEY.public_key())))


def token(**over):
    now = int(time.time())
    claims = {"iss": ISSUER, "sub": "sa-1", "iat": now, "exp": now + 300,
              "partner_id": "bank-a"}
    claims.update(over)
    return "Bearer " + jwt.encode(claims, KEY, algorithm="ES256")


def test_partner_token_gives_the_partner(partner_realm):
    assert auth.partner_from_token(token()) == "bank-a"


@pytest.mark.parametrize("header,status", [
    (None, 401),
    ("Bearer not-a-jwt", 401),
    ("expired", 401),
    ("wrong-issuer", 401),
    ("no-claim", 403),
])
def test_partner_token_refused(partner_realm, header, status):
    header = {"expired": token(exp=1), "wrong-issuer": token(iss="http://kc.test/realms/staff"),
              "no-claim": token(partner_id=None)}.get(header, header)
    with pytest.raises(HTTPException) as exc:
        auth.partner_from_token(header)
    assert exc.value.status_code == status


def test_partner_auth_not_configured(monkeypatch):
    monkeypatch.setattr(_config, "partner_auth_issuer", "")
    with pytest.raises(HTTPException) as exc:
        auth.partner_from_token(token())
    assert exc.value.status_code == 503


# ── controller wiring ────────────────────────────────────────────────────────

def _envelope(sender="bank-a", **authorize):
    return SeekEnvelope(**{
        "header": {"sender_id": sender, "receiver_id": "aggregation-layer"},
        "message": {"transaction_id": "t-1", "search_request": [{
            "reference_id": "r-1",
            "search_criteria": {"query_type": "beneficiary360", "query": QUERY,
                                "authorize": authorize or {"consent_id": "c-77"}}}]}})


def test_controller_passes_the_token_partner(partner_realm):
    call = aggregator_controller._call(_envelope(), token())
    assert (call["consent_id"], call["partner_id"], call["consent_jws"]) == ("c-77", "bank-a", None)


def test_controller_refuses_a_sender_that_is_not_the_token_partner(partner_realm):
    with pytest.raises(AggregationError) as exc:
        aggregator_controller._call(_envelope(sender="bank-b"), token())
    assert exc.value.reason == "sender_mismatch"


def test_controller_consent_jws_needs_no_token():
    call = aggregator_controller._call(_envelope(consent_jws="a.b.c"), None)
    assert call["consent_jws"] == "a.b.c" and call["partner_id"] is None


# ── purpose: the use is bound to the consent ─────────────────────────────────

def _prepare(svc, purpose, **kw):
    return asyncio.run(svc.prepare(
        consent_id="c-77", partner_id="bank-a", query=QUERY, transaction_id="t",
        reference_id="r", purpose=purpose, mode="sync", **kw))


class PurposeCM(StoredCM):
    """Records the purpose the AL asked the CM about."""

    async def validate_stored(self, consent_id, partner_id, requested_scopes, purpose=None):
        self.asked = purpose
        return await super().validate_stored(consent_id, partner_id, requested_scopes)


def test_purpose_is_asked_of_the_cm_and_carried_to_the_registries(service):
    service.cm = PurposeCM()
    agg = _prepare(service, {"code": "credit-assessment"})
    assert service.cm.asked == {"code": "credit-assessment"}
    # What the registry hop is signed with: the consent's purpose.
    assert agg.purpose == {"code": "credit-assessment"}


def test_another_purpose_is_refused(service):
    service.cm = PurposeCM()
    with pytest.raises(AggregationError) as exc:
        _prepare(service, {"code": "marketing"})
    assert (exc.value.status, exc.value.reason) == (403, "purpose_mismatch")


def test_left_out_purpose_defaults_to_the_consents(service):
    service.cm = PurposeCM()
    assert _prepare(service, None).purpose == {"code": "credit-assessment"}
    assert service.cm.asked is None


def test_purpose_the_cm_does_not_confirm_is_refused(service):
    service.cm = StoredCM(purpose=None)        # a CM that returns no purpose
    with pytest.raises(AggregationError) as exc:
        _prepare(service, {"code": "credit-assessment"})
    assert exc.value.reason == "purpose_unverified"
    assert _prepare(service, None).purpose == {}   # nothing claimed, nothing to check


def test_signed_consent_purpose_comes_from_its_claims(service):
    from test_sync_async import _jws

    jws = _jws({"aud": "partner-x", "purpose": {"code": "credit-assessment"},
                "subject_id": {"type": "national_id", "value": FID}})
    agg = asyncio.run(service.prepare(consent_jws=jws, query=QUERY, transaction_id="t",
                                      reference_id="r", purpose=None, mode="sync"))
    assert agg.purpose == {"code": "credit-assessment"}
    with pytest.raises(AggregationError) as exc:
        asyncio.run(service.prepare(consent_jws=jws, query=QUERY, transaction_id="t",
                                    reference_id="r", purpose={"code": "marketing"},
                                    mode="sync"))
    assert exc.value.reason == "purpose_mismatch"
