"""Loads use-case files from a directory, validates them, and reloads on change.

Each ``*.yaml`` / ``*.yml`` file holds one use case. A file that fails
validation is reported and skipped; if an earlier version of it loaded, that
version keeps serving until the file is fixed. Template files referenced by
name sit next to the use-case files (a ConfigMap has no subdirectories).
"""

import logging
import os
import re
import threading
from dataclasses import dataclass, field
from typing import Any, Dict, Iterable, List, Optional, Tuple

import yaml
from pydantic import ValidationError

from .expr import ExpressionError, JsonPath, parse_derived
from .models import UseCase
from .templates import TemplateRenderError, compile_template, render_query

_logger = logging.getLogger("agri_composite.loader")

USE_CASE_SUFFIXES = (".yaml", ".yml")
TEMPLATE_SUFFIXES = (".j2", ".jinja", ".jinja2")


class UseCaseConfigError(ValueError):
    pass


def topo_levels(sources) -> List[List[str]]:
    """Group sources into levels: each level depends only on earlier levels."""
    deps = {s.id: set(s.depends_on) for s in sources}
    order = [s.id for s in sources]
    levels, done = [], set()
    while len(done) < len(order):
        level = [i for i in order if i not in done and deps[i] <= done]
        if not level:
            cycle = sorted(i for i in order if i not in done)
            raise UseCaseConfigError(f"depends_on has a cycle among {cycle}")
        levels.append(level)
        done.update(level)
    return levels


@dataclass
class CompiledUseCase:
    spec: UseCase
    path: str
    templates: Dict[str, Any]  # source id -> compiled Jinja template
    levels: List[List[str]]
    mapping: List[Tuple[str, JsonPath]]
    derived: List[Tuple[str, tuple]]
    sources: Dict[str, Any] = field(default_factory=dict)  # id -> SourceSpec
    # output field -> the sources it is built from (directly, or through $.data fields)
    field_sources: Dict[str, frozenset] = field(default_factory=dict)

    @property
    def ref(self) -> str:
        return self.spec.ref

    def describe(self) -> Dict[str, Any]:
        s = self.spec
        return {
            "use_case": s.ref,
            "name": s.use_case,
            "version": s.version,
            "status": s.status,
            "title": s.title,
            "description": s.description,
            "policy": s.policy,
            "purpose": s.purpose,
            "consent": {"required": s.consent.required},
            "input": {
                "subject": {"id_types": s.input.subject.id_types},
                "parameters": {
                    name: p.model_dump(exclude_none=True, exclude_defaults=False)
                    for name, p in s.input.parameters.items()
                },
                "batch": {"max_subjects": s.input.batch.max_subjects},
            },
            "sources": [
                {
                    "id": src.id,
                    "controller": src.controller,
                    "requirement": src.requirement,
                    "depends_on": src.depends_on,
                    "scopes": src.scopes,
                    "optional_scopes": src.optional_scopes,
                }
                for src in s.sources
            ],
            "consent_grants_needed": sorted({src.controller for src in s.sources}),
            # What the consent's grant for each registry must (required) and may
            # (optional) name; registries not listed take the grant as it is.
            "consent_scopes": s.consent_scopes(),
            "output_fields": sorted(list(s.response.mapping) + list(s.response.derived)),
            "source_status": s.response.source_status,
            "partial_response": s.execution.partial_response,
            "rate_per_partner": s.limits.rate_per_partner,
        }


def _is_template_file(value: str) -> bool:
    v = value.strip()
    return "\n" not in v and "{" not in v and v.endswith(TEMPLATE_SUFFIXES)


def _sample_parameters(spec: UseCase, fill_all: bool) -> Dict[str, Any]:
    out = {}
    for name, p in spec.input.parameters.items():
        if p.default is not None:
            out[name] = p.default
        elif not fill_all and not p.required:
            out[name] = None
        elif p.enum:
            out[name] = p.enum[0]
        elif p.type == "integer":
            out[name] = int(p.min) if p.min is not None else 1
        elif p.type == "number":
            out[name] = float(p.min) if p.min is not None else 1.0
        elif p.type == "boolean":
            out[name] = True
        else:
            out[name] = "sample"
    return out


def compile_use_case(raw: Dict[str, Any], path: str, known_controllers: Optional[Iterable[str]] = None) -> CompiledUseCase:
    if not isinstance(raw, dict):
        raise UseCaseConfigError("file must hold one YAML mapping (a use case)")
    try:
        spec = UseCase.model_validate(raw)
    except ValidationError as e:
        lines = []
        for err in e.errors():
            loc = ".".join(str(p) for p in err["loc"]) or "(root)"
            lines.append(f"{loc}: {err['msg']}")
        raise UseCaseConfigError("; ".join(lines)) from None

    if known_controllers is not None:
        known = set(known_controllers)
        missing = sorted({s.controller for s in spec.sources} - known)
        if missing:
            raise UseCaseConfigError(
                f"sources name controllers with no registry endpoint configured: {missing} "
                f"(configured: {sorted(known)})"
            )

    levels = topo_levels(spec.sources)
    base_dir = os.path.dirname(os.path.abspath(path))

    templates = {}
    for src in spec.sources:
        text = src.dci.query_template
        if _is_template_file(text):
            name = text.strip()
            if os.path.isabs(name) or ".." in name.replace("\\", "/").split("/") or "/" in name:
                raise UseCaseConfigError(f"source '{src.id}': template file '{name}' must be a plain file name")
            tpath = os.path.join(base_dir, name)
            try:
                with open(tpath, encoding="utf-8") as fh:
                    text = fh.read()
            except OSError as e:
                raise UseCaseConfigError(f"source '{src.id}': cannot read template file '{name}': {e.strerror}") from None
        try:
            templates[src.id] = compile_template(text)
        except TemplateRenderError as e:
            raise UseCaseConfigError(f"source '{src.id}': {e}") from None

    # The templates must render valid DCI queries for sample input.
    for fill_all in (False, True):
        params = _sample_parameters(spec, fill_all)
        for id_type in spec.input.subject.id_types:
            for src in spec.sources:
                ctx = {
                    "subject": {"type": id_type, "value": "SAMPLE-1"},
                    "parameters": params,
                    "sources": {d: {"status": "ok", "records": []} for d in src.depends_on},
                    "request_id": "sample",
                    "today": "2026-01-01",
                    "use_case": spec.ref,
                }
                try:
                    render_query(templates[src.id], ctx)
                except TemplateRenderError as e:
                    raise UseCaseConfigError(
                        f"source '{src.id}': {e} (sample subject type {id_type}, "
                        f"{'all' if fill_all else 'default'} parameters)"
                    ) from None

    mapping, derived = [], []
    for out, path_text in spec.response.mapping.items():
        try:
            mapping.append((out, JsonPath(path_text)))
        except ExpressionError as e:
            raise UseCaseConfigError(f"response.mapping.{out}: {e}") from None
    for out, expr in spec.response.derived.items():
        try:
            derived.append((out, parse_derived(expr)))
        except ExpressionError as e:
            raise UseCaseConfigError(f"response.derived.{out}: {e}") from None

    return CompiledUseCase(
        spec=spec,
        path=path,
        templates=templates,
        levels=levels,
        mapping=mapping,
        derived=derived,
        sources={s.id: s for s in spec.sources},
        field_sources=_field_sources_checked(spec.response.mapping, spec.response.derived),
    )


_SOURCE_REF = re.compile(r"\$\.sources\.([A-Za-z0-9_-]+)")
# Forms whose sources cannot be told from the text: bracket or wildcard steps right
# after $ / $.sources / $.data, and recursive descent. Rejected at load, so a field
# built from a failed source is always known (and null).
_UNTRACEABLE = re.compile(r"\$\s*\[|\$\.\.|\$\.\*|\$\.(sources|data)\s*(\[|\.\*|\.\.)")


def _field_sources_checked(mapping: Dict[str, str], derived: Dict[str, str]) -> Dict[str, frozenset]:
    for kind, items in (("mapping", mapping), ("derived", derived)):
        for name, text in items.items():
            if _UNTRACEABLE.search(text or ""):
                raise UseCaseConfigError(
                    f"response.{kind}.{name}: name sources and outputs in dotted form "
                    "($.sources.<id>..., $.data.<path>), not with [...], * or .. right after $, "
                    "$.sources or $.data (needed to null fields built from a failed source)")
    return field_sources(mapping, derived)
_DATA_REF = re.compile(r"\$\.data\.([A-Za-z0-9_.-]+)")


def field_sources(mapping: Dict[str, str], derived: Dict[str, str]) -> Dict[str, frozenset]:
    """Which sources each output field is built from: its own $.sources.<id> references, plus those
    of the $.data fields it reads (mapping first, then derived in order, as they are evaluated)."""
    out: Dict[str, frozenset] = {}
    for name, text in list(mapping.items()) + list(derived.items()):
        refs = set(_SOURCE_REF.findall(text or ""))
        for dep in _DATA_REF.findall(text or ""):
            for known, srcs in out.items():
                if dep == known or dep.startswith(known + ".") or known.startswith(dep + "."):
                    refs |= srcs
        out[name] = frozenset(refs)
    return out


def parse_ref(ref: str) -> Tuple[str, Optional[int]]:
    """'loan-profile@1' -> ('loan-profile', 1); 'loan-profile' -> ('loan-profile', None)."""
    name, sep, major = (ref or "").partition("@")
    if not sep:
        return name, None
    if not major.isdigit():
        raise ValueError(f"bad use-case version '{major}' (expected a major number, e.g. {name}@1)")
    return name, int(major)


class UseCaseRegistry:
    """The loaded use cases of one directory; thread-safe swaps on reload."""

    def __init__(self, directory: str, known_controllers: Optional[Iterable[str]] = None):
        self.directory = directory
        self.known_controllers = list(known_controllers) if known_controllers is not None else None
        self._lock = threading.Lock()
        self._signature: Optional[tuple] = None
        self._by_file: Dict[str, CompiledUseCase] = {}  # last good version per file
        self._errors: Dict[str, str] = {}
        self._published: Dict[Tuple[str, int], CompiledUseCase] = {}

    # --- loading -------------------------------------------------------------

    def _scan(self) -> tuple:
        entries = []
        try:
            names = sorted(os.listdir(self.directory))
        except OSError as e:
            _logger.error("Use-case directory %s is not readable: %s", self.directory, e.strerror)
            return ()
        for name in names:
            if name.startswith("."):
                continue  # hidden files and a ConfigMap's ..data links
            full = os.path.join(self.directory, name)
            try:
                st = os.stat(full)  # follows the ConfigMap symlinks
            except OSError:
                continue
            if os.path.isfile(full):
                entries.append((name, st.st_mtime_ns, st.st_size))
        return tuple(entries)

    def reload_if_changed(self) -> bool:
        signature = self._scan()
        if signature == self._signature:
            return False
        self._load(signature)
        return True

    def load(self) -> None:
        self._load(self._scan())

    def _load(self, signature: tuple) -> None:
        by_file = dict(self._by_file)
        errors: Dict[str, str] = {}
        present = set()
        for name, _mtime, _size in signature:
            if not name.endswith(USE_CASE_SUFFIXES):
                continue
            path = os.path.join(self.directory, name)
            present.add(path)
            try:
                with open(path, encoding="utf-8") as fh:
                    raw = yaml.safe_load(fh)
                compiled = compile_use_case(raw, path, self.known_controllers)
            except (UseCaseConfigError, yaml.YAMLError, OSError) as e:
                errors[name] = str(e)
                kept = " (keeping the previously loaded version)" if path in by_file else ""
                _logger.error("Use case file %s is invalid%s: %s", name, kept, e)
                continue
            by_file[path] = compiled
            for key in compiled.spec.not_enforced_keys():
                _logger.info("Use case %s: '%s' is accepted but not enforced by this service", compiled.ref, key)
        for path in list(by_file):
            if path not in present:
                del by_file[path]

        published: Dict[Tuple[str, int], CompiledUseCase] = {}
        for compiled in by_file.values():
            spec = compiled.spec
            if spec.status != "published":
                continue
            key = (spec.use_case, spec.major)
            other = published.get(key)
            if other is not None:
                newer = max(other, compiled, key=lambda c: tuple(int(x) for x in c.spec.version.split(".")))
                older = other if newer is compiled else compiled
                msg = f"{spec.ref} is defined twice (versions {other.spec.version}, {spec.version}); serving {newer.spec.version}"
                errors[os.path.basename(older.path)] = msg
                _logger.error(msg)
                compiled = newer
            published[key] = compiled

        with self._lock:
            self._by_file = by_file
            self._errors = errors
            self._published = published
            self._signature = signature
        _logger.info(
            "Loaded use cases from %s: published=%s, invalid files=%d",
            self.directory, sorted(f"{n}@{m}" for n, m in published), len(errors),
        )

    # --- lookups -------------------------------------------------------------

    def get(self, ref: str, major: Optional[int] = None) -> Optional[CompiledUseCase]:
        name, ref_major = parse_ref(ref)
        if ref_major is not None and major is not None and ref_major != major:
            raise ValueError(f"use case version in the path (@{ref_major}) differs from the header ({major})")
        wanted = ref_major if ref_major is not None else major
        with self._lock:
            if wanted is not None:
                return self._published.get((name, wanted))
            candidates = [c for (n, _m), c in self._published.items() if n == name]
        if not candidates:
            return None
        return max(candidates, key=lambda c: c.spec.major)

    def published(self) -> List[CompiledUseCase]:
        with self._lock:
            return sorted(self._published.values(), key=lambda c: (c.spec.use_case, c.spec.major))

    def errors(self) -> Dict[str, str]:
        with self._lock:
            return dict(self._errors)
