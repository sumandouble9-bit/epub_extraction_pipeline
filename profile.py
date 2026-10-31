from __future__ import annotations

import json
import os

from epub_parser import order_styles

PROFILE_DIR = os.path.join(os.path.dirname(__file__), "profiles")

DEFAULT_PROFILE = {
    "name": "default",
    "books": [],
    "paragraph_style_template": "ParagraphStyle/{key}",
    "paragraph_styles": {},
    "headings": {},
    "char_styles": {},
    "char_style_implies": {},
    "infer_allcaps": True,
    "base_style_overrides": {},
    "run_style_paragraph_styles": {},
    "latex_style": "latex",
    "table_latex_style": "latex",
    "heading_split": None,
    "message_index_start": 10,
    "emit_chunk_context_summary": True,
    "image_path_template": "output\\images\\{book}\\{name}",
}


class Profile:
    def __init__(self, data: dict | None = None):
        merged = dict(DEFAULT_PROFILE)
        merged.update(data or {})
        self.data = merged
        self.paragraph_styles: dict[str, str] = merged.get("paragraph_styles") or {}
        self.headings: dict[str, str] = merged.get("headings") or {}
        self.char_styles: dict[str, str] = merged.get("char_styles") or {}
        self.char_style_implies: dict[str, list] = merged.get("char_style_implies") or {}
        self.template: str = merged.get("paragraph_style_template") or "ParagraphStyle/{key}"


    @classmethod
    def load(cls, name_or_path: str | None) -> "Profile":
        if not name_or_path:
            return cls(_load_builtin("default"))
        if os.path.isfile(name_or_path):
            with open(name_or_path, encoding="utf-8") as handle:
                return cls(json.load(handle))
        builtin = _load_builtin(name_or_path)
        if builtin is None:
            raise FileNotFoundError("no such profile: %s" % name_or_path)
        return cls(builtin)

    def save(self, path: str) -> None:
        os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
        with open(path, "w", encoding="utf-8") as handle:
            json.dump(self.data, handle, ensure_ascii=False, indent=2)


    @staticmethod
    def style_keys(block) -> list[str]:
        """Candidate profile keys for a block, most specific first."""
        tag = block.tag
        classes = list(block.classes) or [None]
        depth = block.li_depth
        keys: list[str] = []
        for cls in classes:
            if cls:
                if depth:
                    keys.append("%s.%s@%d" % (tag, cls, depth))
                keys.append("%s.%s" % (tag, cls))
                keys.append(".%s" % cls)
        if depth:
            keys.append("%s@%d" % (tag, depth))
        keys.append(tag)
        return keys

    def applied_style(self, block) -> str:
        for key in self.style_keys(block):
            if key in self.paragraph_styles:
                return self.paragraph_styles[key]
        cls = block.classes[0] if block.classes else block.tag
        return self.template.format(key=cls, tag=block.tag, cls=cls)

    def heading_type(self, block) -> str | None:
        """Return 'h1'..'h6' when the block should be emitted as a heading."""
        for key in self.style_keys(block):
            if key in self.headings:
                return self.headings[key]
        if block.tag in ("h1", "h2", "h3", "h4", "h5", "h6"):
            return block.tag
        return None

    def char_style(self, styles) -> str | None:
        if not styles:
            return None
        return self.char_styles.get("+".join(styles))

    def resolve_run(self, styles) -> tuple[tuple[str, ...], str | None]:
        """Return the char_style list and optional InDesign name for an exact match."""
        ordered = order_styles(styles)
        return ordered, self.char_style(ordered)


    @property
    def infer_allcaps(self) -> bool:
        return bool(self.data.get("infer_allcaps", False))

    def baseline_drop(self, applied_style) -> frozenset:
        return frozenset(self.data.get("base_style_overrides", {}).get(applied_style, ()))

    def paragraph_style_for_runs(self, styles) -> str | None:
        """Paragraph style implied by the whole paragraph carrying ``styles``."""
        if not styles:
            return None
        return self.data.get("run_style_paragraph_styles", {}).get("+".join(styles))

    @property
    def latex_style(self) -> str:
        return self.data.get("latex_style") or "latex"

    @property
    def table_latex_style(self) -> str:
        return self.data.get("table_latex_style") or "latex"

    @property
    def heading_split(self):
        return self.data.get("heading_split")

    @property
    def message_index_start(self) -> int:
        return int(self.data.get("message_index_start", 10))

    @property
    def emit_summary(self) -> bool:
        return bool(self.data.get("emit_chunk_context_summary", True))

    @property
    def image_path_template(self) -> str:
        return self.data.get("image_path_template") or "output\\images\\{book}\\{name}"


def _load_builtin(name: str):
    path = os.path.join(PROFILE_DIR, "%s.json" % name)
    if not os.path.isfile(path):
        return None
    with open(path, encoding="utf-8") as handle:
        return json.load(handle)


def find_for_book(epub_path: str) -> "Profile | None":
    """The built-in profile learned from this book, if there is one."""
    stem = os.path.splitext(os.path.basename(epub_path))[0].strip().lower()
    if not stem:
        return None
    for name in list_builtin():
        data = _load_builtin(name) or {}
        for token in data.get("books") or []:
            token = str(token).strip().lower()
            if token and (token == stem or token in stem or stem in token):
                return Profile(data)
    return None


def list_builtin() -> list[str]:
    if not os.path.isdir(PROFILE_DIR):
        return []
    return sorted(f[:-5] for f in os.listdir(PROFILE_DIR) if f.endswith(".json"))
