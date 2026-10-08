"""The alias -> registry/scope/path map the aggregator projects with.

A partner asks for friendly, cross-registry field names::

    farmer.firstname, farmer.lastname, farmer.mobile,
    livestock.UIN, livestock.animal_details

None of those are keys a registry can filter on. Each registry renders one JSON
record from its outgest template, and the platform's consent clamp
(``_clamp_record_fields`` in the registry partner API) is a strict allow-list
over that record's **top-level keys only**. ``farmer.firstname`` lives at
``farmer_personal_details.demographic_info.name.given_name`` - four levels down.

So each alias carries three things:

``registry``
    which registry holds it, and therefore which consent object and which
    ``/dci/registry/sync/search`` call it needs.
``scope``
    the top-level template key that must be in the consent's
    ``effective_data_scopes`` for the registry to return the block at all.
    Several aliases usually share one scope - asking for both
    ``farmer.firstname`` and ``farmer.lastname`` consents to
    ``farmer_personal_details`` once.
``path``
    the dotted path *within the record* that the aggregator projects out.

**Data minimisation happens here, not at the registry.** Because the registry
clamps by top-level key, asking for a first name means the registry hands the
aggregator the whole ``farmer_personal_details`` block; the aggregator then
keeps only the requested leaf before the record ever reaches the partner. The
partner sees exactly what it asked for. The trust boundary is the aggregator,
and that is worth stating plainly rather than implying the registry filtered it.

Adding a field is a data change, not a code change: add a row here. Paths were
read from the live outgest templates, not guessed.
"""
from typing import Any, Dict, List, NamedTuple, Optional, Tuple


class FieldSpec(NamedTuple):
    registry: str   # farmer | livestock | cropsown
    scope: str      # top-level template key the consent must carry
    path: str       # dotted path within the rendered record


# Aliases are matched case-insensitively; the canonical spelling is the key.
CATALOG: Dict[str, FieldSpec] = {
    # ── Farmer registry ────────────────────────────────────────────────────
    "farmer.uin": FieldSpec(
        "farmer", "farmer_personal_details",
        "farmer_personal_details.member_identifier[].identifier_value"),
    "farmer.firstname": FieldSpec(
        "farmer", "farmer_personal_details",
        "farmer_personal_details.demographic_info.name.given_name"),
    "farmer.middlename": FieldSpec(
        "farmer", "farmer_personal_details",
        "farmer_personal_details.demographic_info.name.second_name"),
    "farmer.lastname": FieldSpec(
        "farmer", "farmer_personal_details",
        "farmer_personal_details.demographic_info.name.surname"),
    "farmer.mobile": FieldSpec(
        "farmer", "farmer_personal_details",
        "farmer_personal_details.demographic_info.phone_number"),
    "farmer.gender": FieldSpec(
        "farmer", "farmer_personal_details",
        "farmer_personal_details.demographic_info.sex"),
    "farmer.birthdate": FieldSpec(
        "farmer", "farmer_personal_details",
        "farmer_personal_details.demographic_info.birth_date"),
    "farmer.marital_status": FieldSpec(
        "farmer", "farmer_personal_details", "farmer_personal_details.marital_status"),
    "farmer.education_level": FieldSpec(
        "farmer", "farmer_personal_details", "farmer_personal_details.education_level"),
    "farmer.registration_date": FieldSpec(
        "farmer", "farmer_personal_details", "farmer_personal_details.registration_date"),
    # Whole blocks - the path IS the top-level key.
    "farmer.family_details": FieldSpec("farmer", "family_details", "family_details"),
    "farmer.farm_details": FieldSpec("farmer", "farm_details", "farm_details"),

    # ── Livestock registry ─────────────────────────────────────────────────
    "livestock.uin": FieldSpec(
        "livestock", "livestock_details",
        "livestock_details.record_identifier[].identifier_value"),
    "livestock.oan_id": FieldSpec(
        "livestock", "livestock_details", "livestock_details.oan_id"),
    "livestock.status": FieldSpec(
        "livestock", "livestock_details", "livestock_details.status"),
    "livestock.total_animals": FieldSpec(
        "livestock", "livestock_details", "livestock_details.total_animals"),
    "livestock.registration_date": FieldSpec(
        "livestock", "livestock_details", "livestock_details.registration_date"),
    "livestock.farmer_firstname": FieldSpec(
        "livestock", "livestock_details",
        "livestock_details.farmer_info.demographic_info.name.given_name"),
    "livestock.farmer_lastname": FieldSpec(
        "livestock", "livestock_details",
        "livestock_details.farmer_info.demographic_info.name.surname"),
    "livestock.farmer_mobile": FieldSpec(
        "livestock", "livestock_details", "livestock_details.farmer_info.phone_number"),
    "livestock.place": FieldSpec(
        "livestock", "livestock_details", "livestock_details.place"),
    # Whole blocks.
    "livestock.animal_details": FieldSpec(
        "livestock", "animal_details", "animal_details"),
    "livestock.health_event_details": FieldSpec(
        "livestock", "health_event_details", "health_event_details"),
    "livestock.vaccination_details": FieldSpec(
        "livestock", "vaccination_details", "vaccination_details"),
    "livestock.vital_event_details": FieldSpec(
        "livestock", "vital_event_details", "vital_event_details"),
    "livestock.breeding_details": FieldSpec(
        "livestock", "breeding_details", "breeding_details"),

    # ── Crop Sown registry ─────────────────────────────────────────────────
    "cropsown.crop_sown_details": FieldSpec(
        "cropsown", "crop_sown_details", "crop_sown_details"),
    "cropsown.crop_production_details": FieldSpec(
        "cropsown", "crop_production_details", "crop_production_details"),
    "cropsown.farm_details": FieldSpec("cropsown", "farm_details", "farm_details"),
    "cropsown.infestation_details": FieldSpec(
        "cropsown", "infestation_details", "infestation_details"),
    "cropsown.cluster_details": FieldSpec(
        "cropsown", "cluster_details", "cluster_details"),
}


class UnknownFieldError(ValueError):
    """Raised for an alias not in the catalog. Deliberately fail-fast: silently
    dropping an unknown name would hand the partner a short record with no
    explanation, and look identical to 'the farmer has no data'."""

    def __init__(self, unknown: List[str]):
        self.unknown = unknown
        super().__init__("unknown field(s): %s" % ", ".join(sorted(unknown)))


def resolve(fields: List[str]) -> Dict[str, List[Tuple[str, FieldSpec]]]:
    """Group requested aliases by registry.

    Returns ``{registry: [(alias, spec), ...]}``. Raises UnknownFieldError
    listing every bad alias at once, rather than failing on the first.
    """
    unknown, by_registry = [], {}
    for raw in fields:
        alias = (raw or "").strip()
        spec = CATALOG.get(alias.lower())
        if spec is None:
            unknown.append(raw)
            continue
        by_registry.setdefault(spec.registry, []).append((alias, spec))
    if unknown:
        raise UnknownFieldError(unknown)
    return by_registry


def scopes_for(specs: List[Tuple[str, FieldSpec]]) -> List[str]:
    """The distinct top-level scopes a registry's consent object must carry to
    satisfy these aliases. Sorted so the consent object is reproducible."""
    return sorted({spec.scope for _alias, spec in specs})


def _empty(value: Any) -> bool:
    """Treat "", [], {} and None alike as 'not present'.

    The outgest templates emit an empty string rather than omitting a field a
    farmer has not supplied - phone_number renders as [""] for a farmer with no
    phone. Passing that through would hand the partner a key that looks like
    data and is not, so absence is normalised here instead.
    """
    return value is None or value == "" or value == [] or value == {}


def _clean(value: Any) -> Any:
    """Drop empty members from a list result.

    A path with no [] segment returns its list whole, so a phone_number
    rendered as [""] would survive _empty(). Cleaning here keeps 'no data'
    looking the same however the path was written.
    """
    if isinstance(value, list):
        kept = [v for v in value if not _empty(v)]
        return kept or None
    return value


def _walk(node: Any, parts: List[str]) -> Any:
    """Follow a dotted path, where a ``[]`` segment maps over a list.

    ``member_identifier[].identifier_value`` over a list of identifier objects
    yields a list of the values. Returns None for a path that does not exist,
    which is normal - a farmer with no phone number is not an error.
    """
    if not parts:
        return node
    head, rest = parts[0], parts[1:]
    if head.endswith("[]"):
        key = head[:-2]
        seq = node.get(key) if isinstance(node, dict) else None
        if not isinstance(seq, list):
            return None
        out = [v for v in (_walk(item, rest) for item in seq) if not _empty(v)]
        return out or None
    if isinstance(node, dict):
        return _walk(node.get(head), rest) if head in node else None
    return None


def project(record: Dict[str, Any], specs: List[Tuple[str, FieldSpec]]) -> Dict[str, Any]:
    """Pull the requested aliases out of one rendered registry record.

    The result is keyed by the alias the partner asked for, flat - so a partner
    that asked for ``farmer.firstname`` gets ``{"farmer.firstname": "Abebe"}``
    and never has to know the template's nesting. An alias whose path is absent
    is omitted rather than returned as null, so a short record means 'no such
    data' and an absent key never looks like a deliberate empty value.
    """
    out: Dict[str, Any] = {}
    for alias, spec in specs:
        value = _clean(_walk(record, spec.path.split(".")))
        if not _empty(value):
            out[alias] = value
    return out


def describe() -> List[Dict[str, Optional[str]]]:
    """The catalog as data, for the discovery endpoint - so a partner can find
    out what it may ask for without reading this file."""
    return [
        {"field": alias, "registry": spec.registry, "scope": spec.scope}
        for alias, spec in sorted(CATALOG.items())
    ]
