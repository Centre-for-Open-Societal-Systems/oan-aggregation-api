"""DCI query templates: sandboxed Jinja rendering a JSON object.

A template renders the query part of a DCI ``search_criteria``::

    {"query_type": "expression",
     "query": {"type": "expression", "value": {...}},
     "pagination": {...}, "sort": [...]}         # optional

The composite adds ``version``, ``reg_type``, ``reg_record_type`` and the
consent. Template context: ``subject``, ``parameters``, ``sources`` (the
results of the source's dependencies: ``{id: {status, records}}``),
``request_id``, ``today`` (ISO date) and ``use_case``.
"""

import json
from typing import Any, Dict

from jinja2 import ChainableUndefined, TemplateError
from jinja2.sandbox import ImmutableSandboxedEnvironment

ALLOWED_KEYS = {"query_type", "query", "pagination", "sort"}

_env = ImmutableSandboxedEnvironment(
    autoescape=False,
    undefined=ChainableUndefined,  # sources.farmer.records on a missing source is empty, not an error
    keep_trailing_newline=False,
)


class TemplateRenderError(ValueError):
    pass


def compile_template(text: str):
    try:
        return _env.from_string(text)
    except TemplateError as e:
        raise TemplateRenderError(f"template does not compile: {e}") from e


def render_query(template, context: Dict[str, Any]) -> Dict[str, Any]:
    try:
        text = template.render(**context)
    except Exception as e:  # sandbox violations, runtime errors in the template
        raise TemplateRenderError(f"template failed to render: {type(e).__name__}: {e}") from e
    try:
        rendered = json.loads(text)
    except json.JSONDecodeError as e:
        raise TemplateRenderError(f"template output is not JSON: {e.msg} (line {e.lineno})") from e
    if not isinstance(rendered, dict):
        raise TemplateRenderError("template output must be a JSON object")
    extra = set(rendered) - ALLOWED_KEYS
    if extra:
        raise TemplateRenderError(f"template output has unexpected keys {sorted(extra)}; allowed {sorted(ALLOWED_KEYS)}")
    query = rendered.get("query")
    if not isinstance(query, dict) or not query.get("type") or "value" not in query:
        raise TemplateRenderError("template output needs query: {type, value}")
    rendered.setdefault("query_type", query["type"])
    return rendered
