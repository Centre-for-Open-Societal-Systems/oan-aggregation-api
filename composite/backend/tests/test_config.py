"""Use-case loading, strict validation, and reload without restart."""

import os

import pytest
import yaml
from conftest import USE_CASES
from openg2p_agri_composite.core.loader import (
    UseCaseConfigError,
    UseCaseRegistry,
    compile_use_case,
    parse_ref,
    topo_levels,
)

CONTROLLERS = ["farmer-registry", "crop-sown-registry"]


def sample():
    with open(os.path.join(USE_CASES, "loan-profile.yaml")) as fh:
        return yaml.safe_load(fh)


def compile_raw(raw, controllers=CONTROLLERS, path="/tmp/x.yaml"):
    return compile_use_case(raw, path, controllers)


def test_sample_loads_and_describes():
    c = compile_raw(sample())
    assert c.ref == "loan-profile@1"
    assert c.levels == [["farmer"], ["season_summaries", "crop_seasons"]]
    d = c.describe()
    assert d["input"]["subject"]["id_types"] == ["FAYDA_FAN", "FARMER_ID"]
    assert d["input"]["parameters"]["season"]["enum"] == ["SEASON_MEHER", "SEASON_BELG", "SEASON_IRRIGATION"]
    assert "land.total_size" in d["output_fields"] and "farmer.name" in d["output_fields"]
    assert d["consent_grants_needed"] == ["crop-sown-registry", "farmer-registry"]


@pytest.mark.parametrize(
    "mutate, message",
    [
        (lambda r: r.update(colour="red"), "colour: Extra inputs are not permitted"),
        (lambda r: r.update(version="1.0"), "semver"),
        (lambda r: r.update(status="live"), "status"),
        (lambda r: r["input"].update(batch={"max_subjects": 5}), "max_subjects"),
        (lambda r: r["sources"][1].update(depends_on=["nobody"]), "unknown source 'nobody'"),
        (lambda r: r["sources"][1].update(requirement="sometimes"), "requirement"),
        (lambda r: r["sources"][0].update(controller="livestock-registry", scopes=[], optional_scopes=[]),
         "no registry endpoint configured"),
        (lambda r: r["sources"][0].update(scopes=["crop-sown-registry.measures"]),
         "are not scopes of its registry 'farmer-registry'"),
        (lambda r: r["sources"][0].update(optional_scopes=["farmer-registry.land"]),
         "are both in scopes and optional_scopes"),
        (lambda r: r["sources"][0]["dci"].update(query_template="{\"query\": 1}"), "query: {type, value}"),
        (lambda r: r["sources"][0]["dci"].update(query_template="{% if %}"), "does not compile"),
        (lambda r: r["sources"][0]["dci"].update(query_template="not json"), "not JSON"),
        (lambda r: r["sources"][0]["dci"].update(query_template="missing.j2"), "cannot read template file"),
        (lambda r: r["sources"][0]["dci"].update(query_template="../x.j2"), "plain file name"),
        (lambda r: r["response"]["mapping"].update({"a.b": "no-dollar"}), "must start with '$'"),
        (lambda r: r["response"]["derived"].update({"x": "eval($.a)"}), "unknown function 'eval'"),
        (lambda r: r["response"]["derived"].update({"farmer": "count($.a)"}), "both a value and a parent"),
        (lambda r: r["input"]["parameters"]["crop_year"].update(default="2019"), "must be an integer"),
        (lambda r: r["limits"].update(rate_per_partner="lots"), "rate_per_partner"),
        (lambda r: r["response"]["mapping"].update(x="$['sources']['farmer'].records"), "dotted form"),
        (lambda r: r["response"]["mapping"].update(x="$.sources[*].records"), "dotted form"),
        (lambda r: r["response"]["derived"].update(y="count($..area_sown_ha)"), "dotted form"),
    ],
)
def test_validation_errors_are_clear(mutate, message):
    raw = sample()
    mutate(raw)
    with pytest.raises(UseCaseConfigError) as e:
        compile_raw(raw)
    assert message in str(e.value)


def test_cycle_detected():
    raw = sample()
    raw["sources"][0]["depends_on"] = ["crop_seasons"]
    with pytest.raises(UseCaseConfigError, match="cycle"):
        compile_raw(raw)


def test_template_from_file(tmp_path):
    raw = sample()
    (tmp_path / "farmer.json.j2").write_text(raw["sources"][0]["dci"]["query_template"])
    raw["sources"][0]["dci"]["query_template"] = "farmer.json.j2"
    c = compile_use_case(raw, str(tmp_path / "loan-profile.yaml"), CONTROLLERS)
    assert "farmer" in c.templates


def test_design_keys_accepted_but_not_enforced():
    raw = sample()
    raw["owner"] = "agri-stack-platform"
    raw["consent"].update(collection="cm-originated", mode="single")
    raw["response"].update(schema="schemas/x.json", correlate_on="subject", mode="merged")
    raw["execution"]["fan_out"] = "parallel"
    raw["limits"]["daily_quota_per_partner"] = 100
    raw["audit"] = {"events": ["request", "response"]}
    c = compile_raw(raw)
    assert set(c.spec.not_enforced_keys()) >= {"owner", "consent.collection"}


def test_request_scopes_is_the_earlier_name_of_scopes():
    raw = sample()
    src = raw["sources"][0]
    src["request_scopes"] = src.pop("scopes")
    c = compile_raw(raw)
    assert c.spec.sources[0].scopes == raw["sources"][0]["request_scopes"]
    assert "sources[].request_scopes" not in c.spec.not_enforced_keys()


def test_consent_scopes_per_registry():
    c = compile_raw(sample())
    scopes = c.spec.consent_scopes()
    assert scopes["farmer-registry"]["required"] == sorted(
        f"farmer-registry.{n}" for n in ("farmer_identifiers", "personal_details", "land", "main_crops"))
    assert scopes["farmer-registry"]["optional"] == ["farmer-registry.household_location", "farmer-registry.land_location"]
    assert scopes["crop-sown-registry"]["optional"] == ["crop-sown-registry.location"]
    described = c.describe()
    assert described["consent_scopes"] == scopes
    assert described["sources"][0]["scopes"] == c.spec.sources[0].scopes


def test_parse_ref():
    assert parse_ref("loan-profile@1") == ("loan-profile", 1)
    assert parse_ref("loan-profile") == ("loan-profile", None)
    with pytest.raises(ValueError):
        parse_ref("loan-profile@v1")


def _write(path, raw):
    path.write_text(yaml.safe_dump(raw, sort_keys=False))
    # make sure the mtime moves even on coarse-grained filesystems
    st = os.stat(path)
    os.utime(path, ns=(st.st_atime_ns, st.st_mtime_ns + 1_000_000_000))


def test_registry_reload(tmp_path):
    f = tmp_path / "loan-profile.yaml"
    _write(f, sample())
    (tmp_path / "..data").mkdir()  # ConfigMap internals are ignored
    reg = UseCaseRegistry(str(tmp_path), CONTROLLERS)
    reg.load()
    assert [c.ref for c in reg.published()] == ["loan-profile@1"]
    assert reg.reload_if_changed() is False

    # A new minor version replaces the old one in place.
    raw = sample()
    raw["version"] = "1.1.0"
    _write(f, raw)
    assert reg.reload_if_changed() is True
    assert reg.get("loan-profile").spec.version == "1.1.0"

    # A broken edit keeps serving the last good version and reports the error.
    f.write_text("use_case: loan-profile\nversion: [")
    st = os.stat(f)
    os.utime(f, ns=(st.st_atime_ns, st.st_mtime_ns + 2_000_000_000))
    assert reg.reload_if_changed() is True
    assert reg.get("loan-profile@1").spec.version == "1.1.0"
    assert "loan-profile.yaml" in reg.errors()

    # A second major version in another file; the unversioned ref picks the highest major.
    raw2 = sample()
    raw2["version"] = "2.0.0"
    _write(tmp_path / "loan-profile-v2.yaml", raw2)
    reg.reload_if_changed()
    assert reg.get("loan-profile").spec.major == 2
    assert reg.get("loan-profile@1").spec.version == "1.1.0"
    with pytest.raises(ValueError):
        reg.get("loan-profile@1", major=2)

    # Drafts are not served; deleting a file removes its use case.
    raw2["status"] = "draft"
    _write(tmp_path / "loan-profile-v2.yaml", raw2)
    reg.reload_if_changed()
    assert reg.get("loan-profile@2") is None
    os.remove(f)
    reg.reload_if_changed()
    assert reg.published() == []


def test_duplicate_major_serves_newest(tmp_path):
    a, b = sample(), sample()
    b["version"] = "1.2.0"
    _write(tmp_path / "a.yaml", a)
    _write(tmp_path / "b.yaml", b)
    reg = UseCaseRegistry(str(tmp_path), CONTROLLERS)
    reg.load()
    assert reg.get("loan-profile@1").spec.version == "1.2.0"
    assert "a.yaml" in reg.errors()


def test_missing_directory_is_not_fatal(tmp_path):
    reg = UseCaseRegistry(str(tmp_path / "nope"), CONTROLLERS)
    reg.load()
    assert reg.published() == []


class _S:
    def __init__(self, id, deps=()):
        self.id, self.depends_on = id, list(deps)


def test_dag_levels():
    srcs = [_S("a"), _S("b", ["a"]), _S("c", ["a"]), _S("d", ["b", "c"]), _S("e")]
    assert topo_levels(srcs) == [["a", "e"], ["b", "c"], ["d"]]
    with pytest.raises(UseCaseConfigError):
        topo_levels([_S("a", ["b"]), _S("b", ["a"])])


def test_helm_values_carry_the_sample_use_case():
    values_path = os.path.join(USE_CASES, "..", "charts", "openg2p-agri-composite", "values.yaml")
    with open(values_path) as fh:
        values = yaml.safe_load(fh)
    with open(os.path.join(USE_CASES, "loan-profile.yaml")) as fh:
        assert values["composite"]["useCases"]["loan-profile"] == fh.read()
    assert set(values["composite"]["registries"]) == set(CONTROLLERS)
