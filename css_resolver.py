"""A small CSS cascade resolver.

Only the subset of CSS that EPUBs actually use for text formatting is supported:
type / class / id selectors combined with descendant and child combinators.
"""
from __future__ import annotations

import re

_COMMENT = re.compile(r"/\*.*?\*/", re.S)
_AT_RULE = re.compile(r"@[\w-]+[^{;]*(\{(?:[^{}]|\{[^{}]*\})*\}|;)", re.S)
_RULE = re.compile(r"([^{}]+)\{([^{}]*)\}", re.S)
_SIMPLE = re.compile(
    r"""
    (?P<tag>[a-zA-Z][\w-]*)
  | \.(?P<cls>[\w-]+)
  | \#(?P<id>[\w-]+)
  | \[(?P<attr>[^\]]*)\]
  | (?P<pseudo>::?[\w-]+(?:\([^)]*\))?)
  | (?P<star>\*)
    """,
    re.X,
)

# Properties that cascade down to descendants.
INHERITED = {
    "font-weight", "font-style", "font-variant", "font-variant-caps",
    "text-transform", "font-size", "font-family", "text-align",
    "text-indent", "line-height", "color", "letter-spacing",
}


class Compound:
    """One compound selector such as ``table.border`` or ``.noindent``."""

    __slots__ = ("tag", "classes", "id", "combinator")

    def __init__(self, tag=None, classes=None, id_=None, combinator=" "):
        self.tag = tag
        self.classes = classes or set()
        self.id = id_
        self.combinator = combinator

    def matches(self, el) -> bool:
        if self.tag and self.tag != el["tag"]:
            return False
        if self.id and self.id != el.get("id"):
            return False
        return self.classes <= el["classes"]


def _parse_compound(text: str, combinator: str) -> Compound | None:
    tag = id_ = None
    classes = set()
    saw = False
    for m in _SIMPLE.finditer(text):
        saw = True
        if m.group("tag"):
            tag = m.group("tag").lower()
        elif m.group("cls"):
            classes.add(m.group("cls"))
        elif m.group("id"):
            id_ = m.group("id")
    if not saw:
        return None
    return Compound(tag, classes, id_, combinator)


def parse_selector(sel: str) -> list[Compound] | None:
    """Parse one selector into a left-to-right list of compounds."""
    sel = sel.strip()
    if not sel:
        return None
    parts = re.split(r"\s*([>+~])\s*|\s+", sel)
    parts = [p for p in parts if p]
    compounds: list[Compound] = []
    combinator = " "
    for part in parts:
        if part in (">", "+", "~"):
            combinator = part
            continue
        c = _parse_compound(part, combinator)
        if c is None:
            return None
        compounds.append(c)
        combinator = " "
    return compounds or None


def specificity(compounds: list[Compound]) -> tuple[int, int, int]:
    ids = sum(1 for c in compounds if c.id)
    cls = sum(len(c.classes) for c in compounds)
    tags = sum(1 for c in compounds if c.tag)
    return (ids, cls, tags)


def _parse_declarations(body: str) -> dict[str, tuple[str, bool]]:
    decls: dict[str, tuple[str, bool]] = {}
    for chunk in body.split(";"):
        if ":" not in chunk:
            continue
        name, _, value = chunk.partition(":")
        name = name.strip().lower()
        value = value.strip()
        if not name or not value:
            continue
        important = False
        if value.lower().endswith("!important"):
            important = True
            value = value[: -len("!important")].strip()
        decls[name] = (value, important)
    return decls


class Stylesheet:
    """A parsed stylesheet that can compute declarations for an element path."""

    def __init__(self):
        self.rules: list[tuple[tuple[int, int, int], int, list[Compound], dict]] = []
        self._order = 0

    def add_css(self, text: str) -> None:
        text = _COMMENT.sub("", text or "")
        text = _AT_RULE.sub(lambda m: m.group(1) if m.group(1).startswith("{") else "", text)
        for sel_group, body in _RULE.findall(text):
            decls = _parse_declarations(body)
            if not decls:
                continue
            for sel in sel_group.split(","):
                compounds = parse_selector(sel)
                if not compounds:
                    continue
                self.rules.append((specificity(compounds), self._order, compounds, decls))
                self._order += 1

    @staticmethod
    def _matches(compounds: list[Compound], path: list[dict]) -> bool:
        """Match a selector against an element path (root first, target last)."""
        if not compounds:
            return False
        if not compounds[-1].matches(path[-1]):
            return False
        ci = len(compounds) - 2
        pi = len(path) - 2
        while ci >= 0:
            comp = compounds[ci + 1]
            if comp.combinator == ">":
                if pi < 0 or not compounds[ci].matches(path[pi]):
                    return False
                pi -= 1
            else:
                while pi >= 0 and not compounds[ci].matches(path[pi]):
                    pi -= 1
                if pi < 0:
                    return False
                pi -= 1
            ci -= 1
        return True

    def declarations_for(self, path: list[dict]) -> dict[str, str]:
        """Return the winning declarations for the last element of ``path``."""
        winning: dict[str, tuple[tuple, int, str]] = {}
        for spec, order, compounds, decls in self.rules:
            if not self._matches(compounds, path):
                continue
            for name, (value, important) in decls.items():
                key = ((1 if important else 0),) + spec
                prev = winning.get(name)
                if prev is None or (key, order) >= (prev[0], prev[1]):
                    winning[name] = (key, order, value)
        return {k: v[2] for k, v in winning.items()}
