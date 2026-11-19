"""Response assembly: JSONPath mapping and a small, safe derived-value language.

Mapping values are JSONPath expressions (jsonpath-ng, extended syntax with
filters) evaluated over::

    {"subject": {...}, "parameters": {...},
     "sources": {"<id>": {"status": "ok", "records": [...]}}}

A path with a wildcard, slice, filter or recursive descent yields the list of
all matches; any other path yields its single value (or null).

Derived values are function calls over JSONPaths and numbers, parsed here by a
tiny recursive-descent parser — no ``eval``. Functions: sum, count, min, max,
first, round. Derived expressions also see the mapped output under ``$.data``.
"""

from typing import Any, Dict, List, Tuple

from jsonpath_ng.ext import parse as _jsonpath_parse

FUNCTIONS = ("sum", "count", "min", "max", "first", "round")


class ExpressionError(ValueError):
    pass


class JsonPath:
    __slots__ = ("text", "_parsed", "multi")

    def __init__(self, text: str):
        text = (text or "").strip()
        if not text.startswith("$"):
            raise ExpressionError(f"JSONPath must start with '$': {text!r}")
        try:
            self._parsed = _jsonpath_parse(text)
        except Exception as e:  # jsonpath-ng raises assorted lexer/parser errors
            raise ExpressionError(f"invalid JSONPath {text!r}: {e}") from e
        self.text = text
        self.multi = any(tok in text for tok in ("*", "..", "[?", ":"))

    def matches(self, data: Any) -> List[Any]:
        return [m.value for m in self._parsed.find(data)]

    def value(self, data: Any) -> Any:
        found = self.matches(data)
        if self.multi:
            return found
        return found[0] if found else None


# ── derived expressions ──────────────────────────────────────────────────────

Node = Tuple  # ("num", v) | ("str", v) | ("path", JsonPath) | ("call", name, [Node])


class _Parser:
    def __init__(self, text: str):
        self.s = text
        self.i = 0

    def error(self, msg):
        raise ExpressionError(f"{msg} at position {self.i} in {self.s!r}")

    def ws(self):
        while self.i < len(self.s) and self.s[self.i].isspace():
            self.i += 1

    def parse(self) -> Node:
        node = self.expr()
        self.ws()
        if self.i != len(self.s):
            self.error("unexpected text")
        return node

    def expr(self) -> Node:
        self.ws()
        if self.i >= len(self.s):
            self.error("expression ended early")
        c = self.s[self.i]
        if c == "$":
            return ("path", JsonPath(self.path_text()))
        if c.isdigit() or c in "-.":
            return self.number()
        if c in "'\"":
            return ("str", self.string())
        if c.isalpha() or c == "_":
            name = self.ident()
            if name not in FUNCTIONS:
                self.error(f"unknown function '{name}' (allowed: {', '.join(FUNCTIONS)})")
            self.ws()
            if self.i >= len(self.s) or self.s[self.i] != "(":
                self.error(f"expected '(' after {name}")
            self.i += 1
            args = []
            self.ws()
            if self.i < len(self.s) and self.s[self.i] == ")":
                self.i += 1
            else:
                while True:
                    args.append(self.expr())
                    self.ws()
                    if self.i < len(self.s) and self.s[self.i] == ",":
                        self.i += 1
                        continue
                    if self.i < len(self.s) and self.s[self.i] == ")":
                        self.i += 1
                        break
                    self.error("expected ',' or ')'")
            _check_arity(name, args, self)
            return ("call", name, args)
        self.error(f"unexpected character {c!r}")

    def ident(self) -> str:
        start = self.i
        while self.i < len(self.s) and (self.s[self.i].isalnum() or self.s[self.i] == "_"):
            self.i += 1
        return self.s[start:self.i]

    def number(self) -> Node:
        start = self.i
        if self.s[self.i] == "-":
            self.i += 1
        while self.i < len(self.s) and (self.s[self.i].isdigit() or self.s[self.i] == "."):
            self.i += 1
        text = self.s[start:self.i]
        try:
            return ("num", int(text) if text.lstrip("-").isdigit() else float(text))
        except ValueError:
            self.error(f"bad number {text!r}")

    def string(self) -> str:
        quote = self.s[self.i]
        end = self.s.find(quote, self.i + 1)
        if end < 0:
            self.error("unterminated string")
        value = self.s[self.i + 1:end]
        self.i = end + 1
        return value

    def path_text(self) -> str:
        """A JSONPath runs until a ',' or ')' outside brackets, parentheses and quotes."""
        start, depth, quote = self.i, 0, None
        while self.i < len(self.s):
            c = self.s[self.i]
            if quote:
                if c == quote:
                    quote = None
            elif c in "'\"":
                quote = c
            elif c in "[(":
                depth += 1
            elif c in "])":
                if depth == 0:
                    break
                depth -= 1
            elif c == "," and depth == 0:
                break
            self.i += 1
        return self.s[start:self.i].strip()


def _check_arity(name, args, parser):
    if name in ("count", "first") and len(args) != 1:
        parser.error(f"{name}() takes one argument")
    if name == "round" and len(args) not in (1, 2):
        parser.error("round() takes a value and optional digits")
    if name in ("sum", "min", "max") and not args:
        parser.error(f"{name}() needs at least one argument")


def parse_derived(text: str) -> Node:
    if not isinstance(text, str) or not text.strip():
        raise ExpressionError("derived expression is empty")
    return _Parser(text.strip()).parse()


def _as_list(v) -> list:
    if v is None:
        return []
    return list(v) if isinstance(v, (list, tuple)) else [v]


def _numbers(values) -> list:
    return [v for v in values if isinstance(v, (int, float)) and not isinstance(v, bool)]


def evaluate(node: Node, data: Any) -> Any:
    kind = node[0]
    if kind in ("num", "str"):
        return node[1]
    if kind == "path":
        return node[1].value(data)
    name, args = node[1], node[2]
    if name == "round":
        value = evaluate(args[0], data)
        digits = evaluate(args[1], data) if len(args) > 1 else 0
        if not isinstance(value, (int, float)) or isinstance(value, bool):
            return None
        return round(value, int(digits or 0))
    if name == "count":
        return len(_as_list(evaluate(args[0], data)))
    if name == "first":
        values = _as_list(evaluate(args[0], data))
        return values[0] if values else None
    flat = []
    for a in args:
        flat.extend(_as_list(evaluate(a, data)))
    nums = _numbers(flat)
    if name == "sum":
        total = sum(nums)
        return round(total, 10) if isinstance(total, float) else total
    if not nums:
        return None
    return min(nums) if name == "min" else max(nums)


# ── output assembly ──────────────────────────────────────────────────────────

def set_path(out: Dict[str, Any], dotted: str, value: Any) -> None:
    parts = dotted.split(".")
    node = out
    for p in parts[:-1]:
        node = node.setdefault(p, {})
    node[parts[-1]] = value
