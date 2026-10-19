"""The subject check: the beneficiary asked about is the one who consented;
and what the CM's permit covers."""
import pytest

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


FARMER = ["FARMER_REGISTRY.farmer_personal_details", "FARMER_REGISTRY.farm_details"]
LIVESTOCK = ["LIVESTOCK_REGISTRY.livestock_details", "LIVESTOCK_REGISTRY.animal_details"]


def test_granted_scopes_are_the_requested_ones_the_permit_covers():
    granted = AggregatorService._granted_scopes(FARMER + LIVESTOCK, FARMER[:1] + LIVESTOCK)
    assert granted == FARMER[:1] + LIVESTOCK


def test_a_reused_consent_object_is_named_as_such():
    # The CM returns the first seek's permit (farmer) for a livestock-only seek.
    with pytest.raises(AggregationError) as caught:
        AggregatorService._granted_scopes(LIVESTOCK, FARMER)
    assert (caught.value.status, caught.value.reason) == (409, "consent_object_reused")
    assert "FARMER_REGISTRY" in caught.value.detail
    assert "LIVESTOCK_REGISTRY" in caught.value.detail


@pytest.mark.parametrize("effective", [None, []])
def test_a_permit_without_scopes_is_no_scope_permitted(effective):
    with pytest.raises(AggregationError) as caught:
        AggregatorService._granted_scopes(LIVESTOCK, effective)
    assert (caught.value.status, caught.value.reason) == (403, "no_scope_permitted")
    assert "LIVESTOCK_REGISTRY" in caught.value.detail
