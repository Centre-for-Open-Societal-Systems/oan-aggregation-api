"""The exact identity check on every record a registry returns.

A registry's partner API searches ``search_text`` by substring, so the catalog's
``search.match`` decides which returned records really are the beneficiary.
"""
import copy

import pytest
from conftest import DISABILITY_RECORD, FARMER_RECORD, NSR_RECORD, WORKER_RECORD

FID = "7615076397"


@pytest.mark.parametrize("stored, expected", [
    (FID, True),
    (" %s " % FID, True),          # padding in the record is not identity
    (FID[:-1], False),             # a prefix of the ID is someone else
    (FID + "0", False),            # so is a longer value containing it
    ("", False),
])
def test_value_must_match_exactly(three, stored, expected):
    record = copy.deepcopy(FARMER_RECORD)
    record["person"]["ids"] = [{"type": "UIN", "value": stored}]
    assert three.get("FARMER_REGISTRY").identifies(record, FID) is expected


def test_identifier_type_must_be_the_catalog_id_type(three):
    farmer = three.get("FARMER_REGISTRY")
    record = copy.deepcopy(FARMER_RECORD)
    record["person"]["ids"] = [{"type": "PASSPORT", "value": FID}]
    assert not farmer.identifies(record, FID)
    record["person"]["ids"].append({"type": "UIN", "value": FID})
    assert farmer.identifies(record, FID)       # any one entry may match


def test_value_only_when_type_key_is_null(three):
    disability = three.get("DISABILITY_REGISTRY")
    assert disability.search.match.type_key is None
    assert disability.identifies(DISABILITY_RECORD, FID)
    assert not disability.identifies(DISABILITY_RECORD, "123")


def test_default_dci_keys_and_single_object(three, four):
    nsr = three.get("NATIONAL_SOCIAL_REGISTRY")
    assert (nsr.search.match.value_key, nsr.search.match.type_key) == \
        ("identifier_value", "identifier_type")
    assert nsr.identifies(NSR_RECORD, FID)
    worker = four.get("WORKER_REGISTRY")         # employment.national_id is an object
    assert worker.identifies(WORKER_RECORD, FID)


def test_record_without_the_identifier_block_is_not_the_beneficiary(three):
    farmer = three.get("FARMER_REGISTRY")
    assert not farmer.identifies({"land": FARMER_RECORD["land"]}, FID)
    assert not farmer.identifies({}, FID)
    assert not farmer.identifies({"person": {"ids": "not-a-list"}}, FID)


def test_hop_scopes_always_include_the_identifier_block(three):
    farmer = three.get("FARMER_REGISTRY")
    assert farmer.hop_scopes(["land"]) == ["land", "person"]
    assert farmer.hop_scopes(["person", "land"]) == ["land", "person"]


def test_strip_prefixes_make_both_spellings_the_same_number(fixtures):
    import yaml
    from openg2p_aggregation_layer.registry_catalog import parse_catalog

    raw = yaml.safe_load((fixtures / "catalogs" / "three-registries.yaml")
                         .read_text(encoding="utf-8"))
    raw["registries"]["FARMER_REGISTRY"]["search"]["match"]["strip_prefixes"] = ["FAN-"]
    farmer = parse_catalog(raw).get("FARMER_REGISTRY")
    fan = "1234567890123456"
    record = copy.deepcopy(FARMER_RECORD)
    for stored, asked in [("FAN-" + fan, fan), (fan, "FAN-" + fan),
                          ("fan-" + fan, "FAN-" + fan), (fan, fan)]:
        record["person"]["ids"] = [{"type": "UIN", "value": stored}]
        assert farmer.identifies(record, asked), (stored, asked)
    record["person"]["ids"] = [{"type": "UIN", "value": "FAN-" + fan + "7"}]
    assert not farmer.identifies(record, fan)
    assert not farmer.identifies(record, "FAN-")          # a bare prefix is no ID
    # The registry is searched without the prefix, so its substring search
    # finds the number stored either way.
    assert farmer.search_value("FAN-" + fan) == fan
    assert three_unchanged(fixtures)


def three_unchanged(fixtures):
    from openg2p_aggregation_layer.registry_catalog import load_catalog

    farmer = load_catalog(str(fixtures / "catalogs" / "three-registries.yaml")).get(
        "FARMER_REGISTRY")
    return farmer.search.match.strip_prefixes == [] and \
        farmer.search_value("FAN-1") == "FAN-1"


def test_deploy_catalog_strips_the_fan_prefix(fixtures):
    from openg2p_aggregation_layer.registry_catalog import load_catalog

    catalog = load_catalog(str(fixtures.parents[1] / "deploy" / "registries.yaml"))
    for code in catalog.codes():
        assert catalog.get(code).search.match.strip_prefixes == ["FAN-"]


@pytest.mark.parametrize("stored, asked", [
    ("FAN-FAN-1111222233334444", "1111222233334444"),     # prefix typed twice
    ("1111222233334444", "FAN-FAN-1111222233334444"),
    ("FAN- 1111222233334444", "fan-1111222233334444"),    # space, case
])
def test_repeated_or_messy_prefix_is_still_the_same_number(fixtures, stored, asked):
    import yaml
    from openg2p_aggregation_layer.registry_catalog import parse_catalog

    raw = yaml.safe_load((fixtures / "catalogs" / "three-registries.yaml")
                         .read_text(encoding="utf-8"))
    raw["registries"]["FARMER_REGISTRY"]["search"]["match"]["strip_prefixes"] = ["FAN-"]
    farmer = parse_catalog(raw).get("FARMER_REGISTRY")
    record = copy.deepcopy(FARMER_RECORD)
    record["person"]["ids"] = [{"type": "UIN", "value": stored}]
    assert farmer.identifies(record, asked)
    assert farmer.search_value(asked) == "1111222233334444"
    # Only declared prefixes go: "FAN" without the hyphen is not a Fayda format.
    record["person"]["ids"] = [{"type": "UIN", "value": "FAN1111222233334444"}]
    assert not farmer.identifies(record, asked)
