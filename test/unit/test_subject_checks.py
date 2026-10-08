"""The two subject checks: the beneficiary asked about is the one who consented,
and only that subject - authenticated by token - can release the data."""
import asyncio
from types import SimpleNamespace

import pytest

from openg2p_aggregation_layer.schemas.common import SubjectId
from openg2p_aggregation_layer.services.aggregator_service import (
    AggregationError,
    AggregatorService,
)


def test_foundational_id_must_be_the_consent_subject():
    AggregatorService._check_subject({"foundationalId": "761"}, "761")
    for value in ("762", None, ""):
        with pytest.raises(AggregationError) as caught:
            AggregatorService._check_subject({"foundationalId": "761"}, value)
        assert (caught.value.status, caught.value.reason) == (403, "subject_mismatch")


@pytest.fixture
def service():
    svc = object.__new__(AggregatorService)
    row = SimpleNamespace(id="agg-1", subject_id_type="national_id", subject_id_value="761")
    released = []

    async def get(aggregation_id):
        return row if aggregation_id == "agg-1" else None

    async def verify(request_id, code):
        released.append((request_id, code))
        return row

    svc.get, svc._verify_otp, svc.released = get, verify, released
    return svc


def _release(svc, caller, subject_id=None, aggregation_id="agg-1"):
    return asyncio.run(svc.verify_for_subject(
        aggregation_id=aggregation_id, code="123456", caller=caller, subject_id=subject_id))


@pytest.mark.parametrize("caller", [None, {}, {"subject_id_value": ""}])
def test_release_needs_an_authenticated_subject(service, caller):
    # A body subject_id cannot stand in for the token.
    with pytest.raises(AggregationError) as caught:
        _release(service, caller, SubjectId(type="national_id", value="761"))
    assert caught.value.status == 401
    assert service.released == []


def test_release_by_someone_else_looks_like_an_unknown_id(service):
    other = {"subject_id_type": "national_id", "subject_id_value": "999"}
    with pytest.raises(AggregationError) as caught:
        _release(service, other)
    assert (caught.value.status, caught.value.reason) == (404, "not_found")
    me = {"subject_id_type": "national_id", "subject_id_value": "761"}
    with pytest.raises(AggregationError) as caught:
        _release(service, me, SubjectId(type="national_id", value="999"))
    assert caught.value.status == 404
    with pytest.raises(AggregationError) as caught:
        _release(service, me, aggregation_id="nope")
    assert caught.value.status == 404
    assert service.released == []


def test_release_by_the_subject(service):
    me = {"subject_id_type": "national_id", "subject_id_value": "761"}
    _release(service, me, SubjectId(type="national_id", value="761"))
    assert service.released == [("agg-1", "123456")]


def test_subject_check_ignores_a_declared_prefix(fixtures):
    import yaml
    from openg2p_aggregation_layer.registry_catalog import parse_catalog

    raw = yaml.safe_load((fixtures / "catalogs" / "three-registries.yaml")
                         .read_text(encoding="utf-8"))
    raw["registries"]["FARMER_REGISTRY"]["search"]["match"]["strip_prefixes"] = ["FAN-"]
    same = parse_catalog(raw).same_identifier
    fan = "1111222233334444"
    for asked, subject in [("FAN-" + fan, fan), (fan, "FAN-" + fan),
                           ("FAN-FAN-" + fan, fan), (" fan-" + fan, "FAN-" + fan)]:
        AggregatorService._check_subject({"foundationalId": asked}, subject, same)
    for asked, subject in [("FAN-" + fan, fan + "5"), ("FAN-", "FAN-"), ("", "")]:
        with pytest.raises(AggregationError):
            AggregatorService._check_subject({"foundationalId": asked}, subject, same)
