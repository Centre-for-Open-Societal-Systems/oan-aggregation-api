"""Loading and validating the registry catalog: good files load, bad ones fail
fast with a message that names the problem."""
import copy
import json

import pytest
import yaml

from openg2p_aggregation_layer.registry_catalog import (
    CatalogError,
    RegistryCatalog,
    load_catalog,
    parse_catalog,
)


def test_good_catalog_loads(three):
    assert three.codes() == ["FARMER_REGISTRY", "DISABILITY_REGISTRY",
                             "NATIONAL_SOCIAL_REGISTRY"]
    farmer = three.get("FARMER_REGISTRY")
    assert farmer.partner_api.base_url == "http://farmer.registry.test"
    assert farmer.search.id_type == "UIN"
    assert farmer.search.match.scope == "person"
    assert farmer.scopes["person"].register_mnemonic == "FARMER"
    assert farmer.scopes["land"].tables[0].table == "CROP"
    assert three.get("NATIONAL_SOCIAL_REGISTRY").root_register("valuations") == \
        "SOCIAL_BENEFICIARY"


def test_reference_catalog_in_deploy_loads(fixtures):
    catalog = load_catalog(str(fixtures.parents[1] / "deploy" / "registries.yaml"))
    assert len(catalog.codes()) == 3


def test_scope_ids_round_trip(three):
    ids = three.scope_ids()
    assert "FARMER_REGISTRY.person" in ids and "NATIONAL_SOCIAL_REGISTRY.valuations" in ids
    assert three.scope_ids(["DISABILITY_REGISTRY"]) == ["DISABILITY_REGISTRY.case"]
    split = three.split_scope_ids(
        ["FARMER_REGISTRY.land", "FARMER_REGISTRY.person", "FARMER_REGISTRY.land",
         "FARMER_REGISTRY.unknown", "NOPE.case", "no-separator"])
    # Unknown ids are dropped, never guessed at; the rest come back sorted.
    assert split == {"FARMER_REGISTRY": ["land", "person"]}


def test_describe_carries_no_urls_or_bindings(three):
    described = three.describe()
    text = json.dumps(described)
    assert "registry.test" not in text and "agg-farmer" not in text
    assert "farmer_registry" not in text   # binding controller_id
    farmer = described[0]
    assert farmer["registryCode"] == "FARMER_REGISTRY"
    land = next(s for s in farmer["scopes"] if s["scope"] == "land")
    assert land["scopeId"] == "FARMER_REGISTRY.land"
    assert land["placement"] == {"type": "table", "tableMnemonic": "LAND", "parent": "FARMER"}
    assert land["tables"][0]["tableMnemonic"] == "CROP"


def _raw(fixtures):
    return yaml.safe_load((fixtures / "catalogs" / "three-registries.yaml")
                          .read_text(encoding="utf-8"))


def _mutations():
    def farmer(raw):
        return raw["registries"]["FARMER_REGISTRY"]

    def nsr(raw):
        return raw["registries"]["NATIONAL_SOCIAL_REGISTRY"]

    def m(name, fn, message):
        return pytest.param(fn, message, id=name)

    return [
        m("misspelt key", lambda r: farmer(r)["scopes"]["person"].update(
            feilds=["x"]), "feilds"),
        m("register and table", lambda r: farmer(r)["scopes"]["person"].update(
            table="T", parent="FARMER"), "exactly one of 'register' or 'table'"),
        m("table without parent", lambda r: farmer(r)["scopes"]["land"].pop("parent"),
          "needs a 'parent'"),
        m("unknown register", lambda r: farmer(r)["scopes"]["person"].update(
            register="NOPE"), "register 'NOPE' is not under 'registers'"),
        m("unknown parent", lambda r: farmer(r)["scopes"]["land"].update(parent="NOPE"),
          "neither a register nor a table"),
        m("table parent without join", lambda r: nsr(r)["scopes"]["valuations"].pop("join"),
          "'join' is required"),
        m("duplicate mnemonic", lambda r: farmer(r)["scopes"]["household"].update(
            table="LAND"), "defined twice"),
        m("unused register", lambda r: farmer(r)["registers"].update(SPARE={}),
          "not used by any scope"),
        m("dot in registry code", lambda r: r["registries"].update(
            {"BAD.CODE": r["registries"].pop("DISABILITY_REGISTRY")}),
          "invalid registry code"),
        m("shared binding audience", lambda r: nsr(r)["binding"].update(
            audience="agg-farmer"), "share the binding audience"),
        m("wildcard with nested tables", lambda r: farmer(r)["scopes"]["land"].update(
            fields=["*"]), "cannot be combined with nested 'tables'"),
        m("wildcard with fields", lambda r: farmer(r)["scopes"]["household"].update(
            fields=["*", "size"]), "'*' cannot be combined"),
        m("list and value", lambda r: farmer(r)["scopes"]["person"].update(
            fields=["ids[].value", "ids.value"]), "both as a list"),
        m("bad path", lambda r: farmer(r)["scopes"]["person"].update(
            fields=["name..given"]), "invalid path"),
        m("non-http url", lambda r: farmer(r)["partner_api"].update(
            base_url="ftp://x"), "http(s) URL"),
        m("missing search", lambda r: farmer(r).pop("search"), "search"),
        m("missing identifier match", lambda r: farmer(r)["search"].pop("match"), "match"),
        m("identifier match outside the scopes", lambda r: farmer(r)["search"]["match"]
          .update(path="nowhere.ids[]"), "must start with one of this registry's scopes"),
        m("bad identifier key", lambda r: farmer(r)["search"]["match"]
          .update(value_key="a b"), "invalid key"),
        m("no scopes", lambda r: farmer(r).update(scopes={}), "scopes"),
        m("wrong version", lambda r: r.update(version=2), "version"),
        m("register metadata on a register scope", lambda r: farmer(r)["scopes"]["person"]
          .update(internal_record_id="x"), "applies to a table placement"),
        m("parent cycle", lambda r: nsr(r)["scopes"].update({
            "assets": {"table": "ASSET", "parent": "VALUATION", "fields": ["kind"],
                       "join": {"field": "a", "parent_field": "b"}}}),
          "cycle"),
    ]


@pytest.mark.parametrize("mutate, message", _mutations())
def test_bad_catalog_is_rejected(fixtures, mutate, message):
    raw = copy.deepcopy(_raw(fixtures))
    mutate(raw)
    with pytest.raises(CatalogError) as caught:
        parse_catalog(raw, "test.yaml")
    assert message in str(caught.value), str(caught.value)
    assert "invalid registry catalog test.yaml" in str(caught.value)


def test_every_problem_is_listed_at_once(fixtures):
    raw = copy.deepcopy(_raw(fixtures))
    raw["registries"]["FARMER_REGISTRY"].pop("search")
    raw["registries"]["DISABILITY_REGISTRY"].pop("binding")
    with pytest.raises(CatalogError) as caught:
        parse_catalog(raw)
    text = str(caught.value)
    assert "FARMER_REGISTRY.search" in text and "DISABILITY_REGISTRY.binding" in text


def test_bad_files(tmp_path):
    with pytest.raises(CatalogError, match="no registry catalog configured"):
        load_catalog("")
    with pytest.raises(CatalogError, match="not found"):
        load_catalog(str(tmp_path / "missing.yaml"))
    broken = tmp_path / "broken.yaml"
    broken.write_text("registries: [unclosed", encoding="utf-8")
    with pytest.raises(CatalogError, match="not readable YAML"):
        load_catalog(str(broken))
    empty = tmp_path / "empty.yaml"
    empty.write_text("", encoding="utf-8")
    with pytest.raises(CatalogError, match="invalid registry catalog"):
        load_catalog(str(empty))


def test_catalog_model_is_strict_about_unknown_top_level_keys():
    with pytest.raises(CatalogError, match="registires"):
        parse_catalog({"version": 1, "registires": {}})
    assert RegistryCatalog.model_config["extra"] == "forbid"
