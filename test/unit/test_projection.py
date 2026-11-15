"""The field-level filter: only the configured paths of a block leave the service."""
import pytest

from openg2p_aggregation_layer.registry_catalog import (
    compile_fields,
    first_scalar,
    project,
    resolve,
    rows_at,
)

BLOCK = {
    "member_identifier": [{"identifier_type": "UIN", "identifier_value": "761"},
                          {"identifier_type": "TAX", "identifier_value": ""}],
    "demographic_info": {
        "name": {"given_name": "Mary", "second_name": "", "surname": "Bell"},
        "phone_number": [""],
        "sex": "female",
    },
    "marital_status": "married",
    "religion": "never listed",
}


def test_keeps_only_listed_paths_with_structure():
    tree = compile_fields(["demographic_info.name.given_name",
                           "demographic_info.name.surname", "marital_status"])
    assert project(BLOCK, tree) == {
        "demographic_info": {"name": {"given_name": "Mary", "surname": "Bell"}},
        "marital_status": "married",
    }


def test_list_segment_maps_over_items_and_drops_empties():
    tree = compile_fields(["member_identifier[].identifier_value"])
    assert project(BLOCK, tree) == {"member_identifier": [{"identifier_value": "761"}]}
    # Two paths under the same list merge per item.
    tree = compile_fields(["member_identifier[].identifier_value",
                           "member_identifier[].identifier_type"])
    assert project(BLOCK, tree)["member_identifier"][1] == {"identifier_type": "TAX"}


def test_empty_values_never_look_like_data():
    tree = compile_fields(["demographic_info.phone_number",
                           "demographic_info.name.second_name", "absent.path"])
    assert project(BLOCK, tree) is None


def test_wildcard_keeps_the_whole_block_and_prefix_wins():
    assert project(BLOCK, compile_fields(["*"])) == BLOCK
    tree = compile_fields(["demographic_info.name", "demographic_info.name.surname"])
    assert project(BLOCK, tree) == {"demographic_info": {"name": BLOCK["demographic_info"]["name"]}}
    tree = compile_fields(["demographic_info.name.surname", "demographic_info.name"])
    assert project(BLOCK, tree) == {"demographic_info": {"name": BLOCK["demographic_info"]["name"]}}


def test_a_scalar_where_an_object_is_expected_is_absent():
    assert project({"a": "x"}, compile_fields(["a.b"])) is None
    assert project({"a": {"b": 1}}, compile_fields(["a[].b"])) is None


@pytest.mark.parametrize("bad", [["a..b"], ["a b"], [""], ["*", "a"], ["a[]", "a.b"]])
def test_invalid_field_lists(bad):
    with pytest.raises(ValueError):
        compile_fields(bad)


def test_resolve_and_rows():
    record = {"blk": BLOCK, "rows": [{"k": 1, "inner": [{"z": 1}, {"z": 2}]}, {"k": 2}]}
    assert resolve(record, "blk.marital_status") == "married"
    assert resolve(record, "blk.member_identifier[].identifier_value") == ["761", ""]
    assert first_scalar(record, "blk.member_identifier[].identifier_value") == "761"
    assert first_scalar(record, "blk.member_identifier") is None   # not a scalar
    assert first_scalar(record, None) is None
    assert rows_at(record, "rows") == record["rows"]
    assert rows_at(record, "rows[].inner") == [{"z": 1}, {"z": 2}]
    assert rows_at(record, "blk") == [BLOCK]
    assert rows_at(record, "missing") == []
