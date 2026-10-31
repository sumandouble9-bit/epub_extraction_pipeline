"""Turn EPUB XHTML documents into an ordered stream of blocks with styled runs."""
from __future__ import annotations

import os
import re
from dataclasses import dataclass, field

from css_resolver import INHERITED, Stylesheet

# Emitted in this order so char_style lists are stable and comparable.
STYLE_ORDER = [
    "bold", "italic", "underline", "strikethrough",
    "allcaps", "smallcaps", "superscript", "subscript",
]
_STYLE_RANK = {name: i for i, name in enumerate(STYLE_ORDER)}

BLOCK_TAGS = {
    "p", "h1", "h2", "h3", "h4", "h5", "h6", "li", "div", "blockquote",
    "caption", "figcaption", "dt", "dd", "pre", "address",
}
HEADING_TAGS = {"h1", "h2", "h3", "h4", "h5", "h6"}
SKIP_TAGS = {"script", "style", "head", "title", "meta", "link"}
LIST_TAGS = {"ul", "ol", "dl"}

# User-agent defaults that the author stylesheet may still override.
TAG_STYLES = {
    "b": {"bold"}, "strong": {"bold"},
    "i": {"italic"}, "em": {"italic"}, "cite": {"italic"}, "var": {"italic"},
    "u": {"underline"}, "ins": {"underline"},
    "s": {"strikethrough"}, "strike": {"strikethrough"}, "del": {"strikethrough"},
    "sup": {"superscript"}, "sub": {"subscript"},
    "h1": {"bold"}, "h2": {"bold"}, "h3": {"bold"},
    "h4": {"bold"}, "h5": {"bold"}, "h6": {"bold"}, "th": {"bold"},
}

_WS = re.compile(r"\s+")

# The newline character used inside assembled block text.
_NL = "\n"


class _HardBreak:
    """Unique sentinel marking a real ``<br/>`` in the inline chunk stream.

    It must NOT be a plain ``"\\n"`` string: pretty-printed EPUB markup leaves
    incidental newlines in element text/tails, and if the sentinel were equal
    to those the assembler would count each of them as a line break (turning a
    single ``<br/>`` into ``\\n\\n\\n``). As a distinct object, only genuine
    ``<br/>`` elements produce a break; formatting whitespace collapses to a
    space like any other whitespace.
    """

    __slots__ = ()

    def __repr__(self):  # pragma: no cover - debugging aid
        return "<BR>"


_BR = _HardBreak()

_UL_TYPES = {"disc", "circle", "square", "none"}
_OL_TYPES = {"decimal", "lower-alpha", "upper-alpha", "lower-roman",
             "upper-roman", "lower-latin", "upper-latin", "none"}

_TEXT_MARKER = re.compile(
    r"^(\(?)"
    r"([a-z]|[A-Z]|[0-9]{1,4}|[ivxlcdm]+|[IVXLCDM]+)"
    r"([).\]])"
    r"\s+")

_ROMAN_SET = set("ivxlcdmIVXLCDM")


def _detect_text_marker(text: str):
    """Detect a), b), 1), i), A) etc. at the start of text.

    Returns (marker_type, clean_text) or (None, text).
    """
    m = _TEXT_MARKER.match(text)
    if not m:
        return None, text
    body = m.group(2)
    if body.isdigit():
        return "decimal", text[m.end():]
    if body.islower():
        if all(c in _ROMAN_SET for c in body) and len(body) > 1:
            return "lower-roman", text[m.end():]
        return "lower-alpha", text[m.end():]
    if body.isupper():
        if all(c in _ROMAN_SET for c in body) and len(body) > 1:
            return "upper-roman", text[m.end():]
        return "upper-alpha", text[m.end():]
    return None, text


def _strip_text_markers(text, spans):
    """Strip detected text markers (a), b), 1) etc.) from \n-separated lines in cell text."""
    if "\n" not in text:
        return text, spans
    parts = text.split("\n")
    new_parts = []
    offsets = []
    cumulative = 0
    pos = 0
    for part in parts:
        marker, clean = _detect_text_marker(part)
        strip_len = len(part) - len(clean) if marker else 0
        offsets.append((pos, strip_len))
        cumulative += strip_len
        new_parts.append(clean if marker else part)
        pos += len(part) + 1
    if not any(sl for _, sl in offsets):
        return text, spans
    new_text = "\n".join(new_parts)
    new_spans = []
    for s, e, st in spans:
        shift = 0
        for line_pos, sl in offsets:
            if s >= line_pos + sl:
                shift += sl
            elif s >= line_pos:
                shift += s - line_pos
                break
            else:
                break
        ns = s - shift
        shift_e = 0
        for line_pos, sl in offsets:
            if e >= line_pos + sl:
                shift_e += sl
            elif e >= line_pos:
                shift_e += e - line_pos
                break
            else:
                break
        ne = e - shift_e
        if ne > ns:
            new_spans.append((ns, ne, st))
    return new_text, new_spans


def _list_type(list_el, decls: dict) -> str:
    """Return the CSS list-style-type name for a list element."""
    tag = local_name(list_el.tag)
    lst = (decls.get("list-style-type") or "").strip().lower()
    if lst and lst != "none":
        return lst
    if lst == "none":
        return "none"
    return "decimal" if tag == "ol" else "disc"


def local_name(tag) -> str | None:
    if not isinstance(tag, str):
        return None
    return tag.rsplit("}", 1)[-1].lower()


def normalize_space(text: str) -> str:
    return _WS.sub(" ", text or "").strip()


@dataclass
class Run:
    text: str
    styles: tuple[str, ...]
    index: list[int]
    # Character offsets into the parent block text; used for LaTeX conversion,
    # not part of the serialised output.
    start: int = 0
    end: int = 0


@dataclass
class ImageRef:
    src: str
    path: str
    alt: str = ""
    width_attr: str | None = None
    height_attr: str | None = None


@dataclass
class Block:
    kind: str                       # "para" | "table" | "image"
    doc: str
    tag: str = "p"
    classes: tuple[str, ...] = ()
    li_depth: int = 0
    text: str = ""
    runs: list[Run] = field(default_factory=list)
    images: list[ImageRef] = field(default_factory=list)
    base_styles: frozenset = frozenset()
    align: str | None = None
    # table only
    rows: int = 0
    columns: int = 0
    header_rows: int = 0
    cells: list[dict] = field(default_factory=list)


def styles_from_declarations(decls: dict, inherited: frozenset) -> frozenset:
    """Fold CSS declarations into our flag vocabulary, starting from inherited flags."""
    styles = set(inherited)

    weight = decls.get("font-weight")
    if weight:
        weight = weight.strip().lower()
        if weight in ("bold", "bolder") or (weight.isdigit() and int(weight) >= 600):
            styles.add("bold")
        elif weight in ("normal", "lighter") or (weight.isdigit() and int(weight) < 600):
            styles.discard("bold")

    style = decls.get("font-style")
    if style:
        style = style.strip().lower()
        if style in ("italic", "oblique"):
            styles.add("italic")
        elif style == "normal":
            styles.discard("italic")

    decoration = decls.get("text-decoration-line") or decls.get("text-decoration")
    if decoration:
        decoration = decoration.strip().lower()
        if "underline" in decoration:
            styles.add("underline")
        if "line-through" in decoration:
            styles.add("strikethrough")
        if "none" in decoration:
            styles.discard("underline")
            styles.discard("strikethrough")

    transform = (decls.get("text-transform") or "").strip().lower()
    if transform == "uppercase":
        styles.add("allcaps")
    elif transform == "none":
        styles.discard("allcaps")

    variant = (decls.get("font-variant-caps") or decls.get("font-variant") or "").strip().lower()
    if "small-caps" in variant:
        styles.add("smallcaps")

    valign = (decls.get("vertical-align") or "").strip().lower()
    if valign in ("super", "text-top"):
        styles.add("superscript")
    elif valign in ("sub", "text-bottom"):
        styles.add("subscript")

    return frozenset(styles)


def order_styles(styles) -> tuple[str, ...]:
    return tuple(sorted(styles, key=lambda s: _STYLE_RANK.get(s, 99)))


class DocumentExtractor:
    """Extracts blocks from a single XHTML document."""

    def __init__(self, epub, doc_path: str, sheet: Stylesheet):
        self.epub = epub
        self.doc_path = doc_path
        self.sheet = sheet
        self.blocks: list[Block] = []
        self._cache: dict[tuple, dict] = {}

    # ------------------------------------------------------------ css helper

    def _el_desc(self, el) -> dict:
        return {
            "tag": local_name(el.tag) or "",
            "classes": set((el.get("class") or "").split()),
            "id": el.get("id"),
        }

    def _computed(self, path: list[dict], inherited_decls: dict) -> dict:
        own = self.sheet.declarations_for(path)
        merged = {k: v for k, v in inherited_decls.items() if k in INHERITED}
        merged.update(own)
        return merged

    def _element_styles(self, el, path, inherited_styles, inherited_decls):
        tag = local_name(el.tag)
        own = self.sheet.declarations_for(path)
        inherited_only = {k: v for k, v in inherited_decls.items() if k in INHERITED}
        base = styles_from_declarations(inherited_only, inherited_styles)
        base = frozenset(set(base) | TAG_STYLES.get(tag, set()))
        merged = dict(inherited_only)
        merged.update(own)
        return styles_from_declarations(own, base), merged

    # -------------------------------------------------------------- walking

    def run(self, root) -> list[Block]:
        body = None
        for el in root.iter():
            if local_name(el.tag) == "body":
                body = el
                break
        if body is None:
            body = root
        path = [self._el_desc(body)]
        decls = self.sheet.declarations_for(path)
        styles = styles_from_declarations(decls, frozenset())
        self._walk(body, path, styles, decls, li_depth=0)
        return self.blocks

    def _has_nested_block(self, el) -> bool:
        for child in el.iterdescendants():
            name = local_name(child.tag)
            if name in BLOCK_TAGS or name == "table":
                return True
        return False

    def _walk(self, el, path, styles, decls, li_depth: int):
        for child in el:
            tag = local_name(child.tag)
            if tag is None or tag in SKIP_TAGS:
                continue

            child_path = path + [self._el_desc(child)]
            child_styles, child_decls = self._element_styles(child, child_path, styles, decls)

            if tag == "table":
                self.blocks.append(self._build_table(child, child_path, child_styles, child_decls))
                continue

            if tag == "img":
                self.blocks.append(Block(
                    kind="image", doc=self.doc_path, tag="img",
                    images=[self._image_ref(child)],
                    align=self._align(child_decls),
                ))
                continue

            depth = li_depth + 1 if tag in LIST_TAGS else li_depth

            if tag in BLOCK_TAGS and not self._has_nested_block(child):
                for block in self._build_paras(child, child_path, child_styles, child_decls, depth):
                    self.blocks.append(block)
                continue

            if tag in BLOCK_TAGS and self._has_nested_block(child):
                self._walk_with_tails(child, child_path, child_styles, child_decls, depth)
                continue

            self._walk(child, child_path, child_styles, child_decls, depth)

    def _is_block_or_list(self, tag):
        return tag in BLOCK_TAGS or tag in LIST_TAGS or tag == "table"

    def _walk_with_tails(self, el, path, styles, decls, li_depth: int):
        """Walk a block element that contains nested blocks, capturing inline+tail text."""
        parent_tag = local_name(el.tag) or "p"
        parent_classes = tuple((el.get("class") or "").split())
        align = self._align(decls)
        shared = dict(doc=self.doc_path, tag=parent_tag, classes=parent_classes,
                      li_depth=li_depth, base_styles=styles, align=align)

        children = list(el)
        inline_chunks: list[tuple[str, frozenset]] = []
        inline_images: list[ImageRef] = []

        def _flush_inline():
            if not inline_chunks:
                return
            text, spans = self._assemble(inline_chunks)
            if text.strip():
                spans = self._merge_spans(text, spans)
                self.blocks.append(Block(
                    kind="para", text=text.strip(),
                    runs=self._make_runs(text, spans, styles),
                    images=inline_images[:], **shared))
            inline_chunks.clear()
            inline_images.clear()

        if el.text:
            inline_chunks.append((el.text, styles))

        for child in children:
            tag = local_name(child.tag)
            if tag is None or tag in SKIP_TAGS:
                if child.tail:
                    inline_chunks.append((child.tail, styles))
                continue

            child_path = path + [self._el_desc(child)]
            child_styles, child_decls = self._element_styles(
                child, child_path, styles, decls)

            if self._is_block_or_list(tag):
                _flush_inline()

                if tag == "table":
                    self.blocks.append(self._build_table(
                        child, child_path, child_styles, child_decls))
                elif tag in LIST_TAGS:
                    depth = li_depth + 1 if tag in LIST_TAGS else li_depth
                    self._walk(child, child_path, child_styles, child_decls, depth)
                elif tag in BLOCK_TAGS and not self._has_nested_block(child):
                    for block in self._build_paras(
                            child, child_path, child_styles, child_decls, li_depth):
                        self.blocks.append(block)
                else:
                    self._walk_with_tails(
                        child, child_path, child_styles, child_decls, li_depth)

                if child.tail:
                    inline_chunks.append((child.tail, styles))
            elif tag == "img":
                inline_images.append(self._image_ref(child))
                if child.tail:
                    inline_chunks.append((child.tail, styles))
            elif tag == "br":
                inline_chunks.append((_BR, styles))
                if child.tail:
                    inline_chunks.append((child.tail, styles))
            else:
                part, imgs = self._collect_inline(child, child_path, child_styles, child_decls)
                inline_chunks.extend(part)
                inline_images.extend(imgs)
                if child.tail:
                    inline_chunks.append((child.tail, styles))

        _flush_inline()

    # ------------------------------------------------------------- builders

    @staticmethod
    def _align(decls: dict) -> str | None:
        value = (decls.get("text-align") or "").strip().lower()
        return value or None

    def _image_ref(self, el) -> ImageRef:
        src = el.get("src") or el.get("{http://www.w3.org/1999/xlink}href") or ""
        return ImageRef(
            src=src,
            path=self.epub.resolve(self.doc_path.rsplit("/", 1)[0] if "/" in self.doc_path else "", src),
            alt=el.get("alt") or "",
            width_attr=el.get("width"),
            height_attr=el.get("height"),
        )

    def _collect_inline(self, el, path, styles, decls):
        """Yield (text, styles) chunks and collect images, in document order."""
        chunks: list[tuple[str, frozenset]] = []
        images: list[ImageRef] = []

        def visit(node, node_path, node_styles, node_decls):
            if node.text:
                chunks.append((node.text, node_styles))
            for child in node:
                tag = local_name(child.tag)
                if tag in SKIP_TAGS:
                    if child.tail:
                        chunks.append((child.tail, node_styles))
                    continue
                child_path = node_path + [self._el_desc(child)]
                child_styles, child_decls = self._element_styles(child, child_path, node_styles, node_decls)
                if tag == "img":
                    images.append(self._image_ref(child))
                elif tag == "br":
                    chunks.append((_BR, node_styles))
                elif tag == "table":
                    if child.tail:
                        chunks.append((child.tail, node_styles))
                    continue
                else:
                    if (tag in BLOCK_TAGS or tag in LIST_TAGS) and chunks:
                        chunks.append((_BR, node_styles))
                    if tag in LIST_TAGS:
                        li_num = [0]
                        for li_child in child:
                            li_tag = local_name(li_child.tag)
                            if li_tag in SKIP_TAGS:
                                continue
                            li_num[0] += 1
                            li_child_path = child_path + [self._el_desc(li_child)]
                            li_styles, li_decls = self._element_styles(
                                li_child, li_child_path, child_styles, child_decls)
                            if chunks:
                                chunks.append((_BR, node_styles))
                            visit(li_child, li_child_path, li_styles, li_decls)
                            if li_child.tail:
                                chunks.append((li_child.tail, node_styles))
                    else:
                        visit(child, child_path, child_styles, child_decls)
                if child.tail:
                    chunks.append((child.tail, node_styles))

        visit(el, path, styles, decls)
        return chunks, images

    def _cell_lines(self, cell, path, styles, decls) -> list[dict]:
        """Block-level children of a table cell, extracted one by one.

        A cell often stacks the steps of a calculation in separate paragraphs,
        and each of those is its own formula rather than one long expression.
        Handles <ul>/<ol> by extracting each <li> as a separate line.
        """
        flat = []
        for child in cell:
            tag = local_name(child.tag)
            if tag in BLOCK_TAGS:
                flat.append((child, ""))
            elif tag in LIST_TAGS:
                child_path = path + [self._el_desc(child)]
                _, list_decls = self._element_styles(child, child_path, styles, decls)
                marker_type = _list_type(child, list_decls)
                for li in child:
                    if local_name(li.tag) not in SKIP_TAGS:
                        flat.append((li, marker_type))
        if len(flat) < 2:
            return []
        lines = []
        for child, marker_type in flat:
            child_path = path + [self._el_desc(child)]
            child_styles, child_decls = self._element_styles(child, child_path, styles, decls)
            chunks, _ = self._collect_inline(child, child_path, child_styles, child_decls)
            text, spans = self._assemble(chunks)
            if not text.strip():
                continue
            if not marker_type:
                detected, clean = _detect_text_marker(text)
                if detected:
                    marker_type = detected
                    strip_len = len(text) - len(clean)
                    text = clean
                    spans = [(max(0, s - strip_len), max(0, e - strip_len), st)
                             for s, e, st in spans if e > strip_len]
            spans = self._merge_spans(text, spans)
            line = {"text": text, "runs": self._make_runs(text, spans, child_styles)}
            if marker_type and marker_type != "none":
                line["list_marker"] = marker_type
            lines.append(line)
        return lines

    @staticmethod
    def _assemble(chunks) -> tuple[str, list[tuple[int, int, frozenset]]]:
        """Concatenate chunks into whitespace-normalised text plus styled spans."""
        pieces: list[str] = []
        spans: list[tuple[int, int, frozenset]] = []
        length = 0
        for raw, styles in chunks:
            if raw is _BR:
                # Drop whitespace emitted just before the break so the two
                # lines join cleanly (no trailing " " before the newline).
                while pieces and pieces[-1].endswith(" "):
                    s, e, st = spans[-1]
                    if e - 1 > s:
                        spans[-1] = (s, e - 1, st)
                        pieces[-1] = pieces[-1][:-1]
                        length -= 1
                        break
                    spans.pop()
                    pieces.pop()
                    length -= 1
                pieces.append(_NL)
                spans.append((length, length + 1, styles))
                length += 1
                continue
            piece = _WS.sub(" ", raw)
            if not piece:
                continue
            # Suppress a leading space at the very start, after another space,
            # or immediately after a hard break.
            if piece.startswith(" ") and (
                    length == 0
                    or pieces[-1].endswith(" ")
                    or pieces[-1].endswith(_NL)):
                piece = piece[1:]
            if not piece:
                continue
            pieces.append(piece)
            spans.append((length, length + len(piece), styles))
            length += len(piece)

        text = "".join(pieces)
        trimmed = text.rstrip()
        limit = len(trimmed)
        clipped = []
        for start, end, styles in spans:
            if start >= limit:
                continue
            clipped.append((start, min(end, limit), styles))
        return trimmed, clipped

    @staticmethod
    def _merge_spans(text, spans):
        """Merge neighbouring spans with equal styles, bridging whitespace-only gaps."""
        merged: list[list] = []
        for start, end, styles in spans:
            if merged and merged[-1][2] == styles and merged[-1][1] == start:
                merged[-1][1] = end
                continue
            merged.append([start, end, styles])

        result: list[list] = []
        for span in merged:
            if result and result[-1][2] == span[2]:
                gap = text[result[-1][1]:span[0]]
                if gap.strip() == "" and "\n" not in gap:
                    result[-1][1] = span[1]
                    continue
            result.append(span)
        return result

    @staticmethod
    def _word_spans(text: str) -> list[tuple[int, int]]:
        return [(m.start(), m.end()) for m in re.finditer(r"\S+", text)]

    @classmethod
    def _make_runs(cls, text, spans, base_styles) -> list[Run]:
        words = cls._word_spans(text)
        runs = []
        for start, end, styles in spans:
            if not styles:
                continue
            fragment = text[start:end]
            if not fragment.strip():
                continue
            lead = len(fragment) - len(fragment.lstrip())
            trail = len(fragment) - len(fragment.rstrip())
            start += lead
            end -= trail
            index = [i + 1 for i, (ws, we) in enumerate(words) if ws < end and we > start]
            if not index:
                continue
            runs.append(Run(text=text[start:end], styles=order_styles(styles),
                            index=index, start=start, end=end))
        return runs

    def _build_paras(self, el, path, styles, decls, li_depth) -> list[Block]:
        """One block per visual line; ``<br/>`` starts a new paragraph."""
        chunks, images = self._collect_inline(el, path, styles, decls)
        text, spans = self._assemble(chunks)
        if not text and not images:
            return []

        tag = local_name(el.tag) or "p"
        classes = tuple((el.get("class") or "").split())
        align = self._align(decls)
        shared = dict(doc=self.doc_path, tag=tag, classes=classes,
                      li_depth=li_depth, base_styles=styles, align=align)

        if _NL not in text:
            spans = self._merge_spans(text, spans)
            return [Block(kind="para", text=text,
                          runs=self._make_runs(text, spans, styles),
                          images=images, **shared)]

        out: list[Block] = []
        line_start = 0
        for pos, ch in enumerate(text):
            if ch != _NL:
                continue
            segment = text[line_start:pos]
            if segment.strip():
                seg_spans = self._clip_spans(segment, spans, line_start, pos)
                seg_spans = self._merge_spans(segment, seg_spans)
                out.append(Block(kind="para", text=segment.strip(),
                                 runs=self._make_runs(segment, seg_spans, styles),
                                 images=images if not out else [], **shared))
            line_start = pos + 1
        tail = text[line_start:]
        if tail.strip():
            seg_spans = self._clip_spans(tail, spans, line_start, len(text))
            seg_spans = self._merge_spans(tail, seg_spans)
            out.append(Block(kind="para", text=tail.strip(),
                             runs=self._make_runs(tail, seg_spans, styles),
                             images=[] if out else images, **shared))
        return out or [Block(kind="para", text=text.strip(),
                             runs=self._make_runs(text, self._merge_spans(text, spans), styles),
                             images=images, **shared)]

    @staticmethod
    def _clip_spans(segment, spans, offset, end):
        """Keep only styled spans that fall inside ``segment``."""
        seg_len = len(segment)
        clipped = []
        for start, stop, styles in spans:
            left = max(start, offset) - offset
            right = min(stop, end) - offset
            if left < right and left < seg_len:
                clipped.append((max(0, left), min(seg_len, right), styles))
        return clipped

    def _build_para(self, el, path, styles, decls, li_depth) -> Block | None:
        blocks = self._build_paras(el, path, styles, decls, li_depth)
        return blocks[0] if blocks else None

    def _build_cell(self, cell, cell_path, tr_styles, tr_decls) -> dict:
        """Extract one table cell, preserving any nested tables as structure."""
        cell_styles, cell_decls = self._element_styles(cell, cell_path, tr_styles, tr_decls)
        chunks: list[tuple[str, frozenset]] = []
        images: list[ImageRef] = []
        nested_tables: list[Block] = []

        if cell.text and cell.text.strip():
            chunks.append((cell.text, cell_styles))

        for child in cell:
            tag = local_name(child.tag)
            child_path = cell_path + [self._el_desc(child)]
            if tag == "table":
                nested_tables.append(
                    self._build_table(child, child_path, cell_styles, cell_decls))
            elif tag in LIST_TAGS:
                child_styles, child_decls = self._element_styles(
                    child, child_path, cell_styles, cell_decls)
                for li in child:
                    li_tag = local_name(li.tag)
                    if li_tag in SKIP_TAGS:
                        continue
                    li_path = child_path + [self._el_desc(li)]
                    li_styles, li_decls = self._element_styles(
                        li, li_path, child_styles, child_decls)
                    if chunks:
                        chunks.append((_BR, cell_styles))
                    part, imgs = self._collect_inline(
                        li, li_path, li_styles, li_decls)
                    chunks.extend(part)
                    images.extend(imgs)
            elif tag not in SKIP_TAGS:
                if tag in BLOCK_TAGS and chunks:
                    chunks.append((_BR, cell_styles))
                part, imgs = self._collect_inline(child, child_path, cell_styles, cell_decls)
                chunks.extend(part)
                images.extend(imgs)
            if child.tail and child.tail.strip():
                chunks.append((child.tail, cell_styles))

        text, spans = self._assemble(chunks)
        text, spans = _strip_text_markers(text, spans)
        spans = self._merge_spans(text, spans) if text else []
        return {
            "text": text,
            "runs": self._make_runs(text, spans, cell_styles) if text else [],
            "lines": self._cell_lines(cell, cell_path, cell_styles, cell_decls),
            "nested_tables": nested_tables,
            "colspan": int(cell.get("colspan") or 1),
            "rowspan": int(cell.get("rowspan") or 1),
            "images": images,
        }

    def _build_table(self, el, path, styles, decls) -> Block:
        rows_out: list[list[dict]] = []
        images: list[ImageRef] = []
        header_rows = 0

        row_elements = []

        def find_rows(node, node_path, node_styles, node_decls):
            for child in node:
                tag = local_name(child.tag)
                if tag in SKIP_TAGS:
                    continue
                child_path = node_path + [self._el_desc(child)]
                child_styles, child_decls = self._element_styles(child, child_path, node_styles, node_decls)
                if tag == "tr":
                    row_elements.append((child, child_path, child_styles, child_decls))
                elif tag in ("thead", "tbody", "tfoot", "colgroup"):
                    find_rows(child, child_path, child_styles, child_decls)

        find_rows(el, path, styles, decls)

        for row_index, (tr, tr_path, tr_styles, tr_decls) in enumerate(row_elements):
            cells = []
            all_header = True
            for cell in tr:
                tag = local_name(cell.tag)
                if tag not in ("td", "th"):
                    continue
                if tag != "th":
                    all_header = False
                cell_path = tr_path + [self._el_desc(cell)]
                built = self._build_cell(cell, cell_path, tr_styles, tr_decls)
                images.extend(built.pop("images", []))
                cells.append(built)
            if not cells:
                continue
            if all_header and not rows_out:
                header_rows += 1
            rows_out.append(cells)

        flat = []
        for row_index, cells in enumerate(rows_out):
            col_index = 0
            for cell in cells:
                flat.append({
                    "row_index": row_index,
                    "col_index": col_index,
                    "text": cell["text"],
                    "runs": cell["runs"],
                    "lines": cell["lines"],
                    "nested_tables": cell["nested_tables"],
                })
                col_index += max(cell["colspan"], 1)

        return Block(
            kind="table",
            doc=self.doc_path,
            tag="table",
            classes=tuple((el.get("class") or "").split()),
            rows=len(rows_out),
            columns=max((len(r) for r in rows_out), default=0),
            header_rows=header_rows,
            cells=flat,
            images=images,
            base_styles=styles,
        )


# Spine documents that are never part of the book body.
_ABOUT_AUTHOR = re.compile(
    r"about\s+the\s+author|author\s+bio|biography", re.I)


_TITLE_PAGE_CLASSES = {"title", "booktitle", "book-title", "halftitle"}
_COVER_CLASSES = {"cover", "cover_image", "cover-image", "coverimage"}
_TOC_CLASSES = {"toc", "tocch", "contents", "toc1", "nonetoc"}

_EPUB_NS = "http://www.idpf.org/2007/ops"
_EPUB_FRONT_TYPES = {"frontmatter", "cover", "titlepage", "toc",
                     "copyright-page", "colophon", "dedication"}

# Filenames that identify a front-matter / about-author document even when the
# markup carries no epub:type and uses only visual CSS classes.
_FRONT_FILENAME_HINTS = {
    "titlepage", "title", "halftitle", "half-title", "cover",
    "copy", "copyright", "contents", "toc", "dedication", "colophon",
    "imprint", "epigraph",
}
_ABOUT_AUTHOR_FILENAME_HINTS = {
    "bio", "biography", "author", "aboutauthor", "about_author",
    "about-the-author", "abouttheauthor",
}

# Class names that mark a copyright page (copyhead, copy0, copy1 ...).
_COPYRIGHT_CLASS_PREFIXES = ("copy",)
# Strong textual signals of a copyright page.
_COPYRIGHT_TEXT = re.compile(
    r"all rights reserved|copyright\s*(?:©|\(c\))|©\s*\d{4}|"
    r"isbn\b|no part of this (?:publication|book) may be reproduced",
    re.I)
# A table-of-contents heading ("Contents" / "Table of Contents").
_TOC_HEADING = re.compile(r"^\s*(?:table of\s+)?contents\s*$", re.I)


def _classify_doc(epub, path: str) -> str:
    """Classify a spine document as 'front', 'about_author', or 'body'
    by inspecting epub:type attributes, heading text, and element classes.
    """
    root = epub.parse_xhtml(path)
    if root is None:
        return "body"

    all_classes = set()
    all_headings = []
    text_parts = []
    has_epub_front = False
    has_epub_body = False
    block_count = 0

    for el in root.iter():
        tag = local_name(el.tag)
        if tag is None:
            continue

        epub_type = el.get("{%s}type" % _EPUB_NS, "") or el.get("epub:type", "")
        if epub_type:
            types = set(epub_type.lower().split())
            if types & _EPUB_FRONT_TYPES:
                has_epub_front = True
            if "bodymatter" in types or "chapter" in types:
                has_epub_body = True

        el_classes = set((el.get("class") or "").lower().split())
        all_classes.update(el_classes)

        if tag in HEADING_TAGS:
            heading_text = (el.text or "") + "".join(
                c.text or "" for c in el.iter() if c is not el)
            all_headings.append(heading_text.strip())

        if tag in BLOCK_TAGS:
            block_count += 1
            if len(text_parts) < 40 and el.text:
                text_parts.append(el.text)

    fname = os.path.splitext(os.path.basename(path))[0].lower()
    all_text = " ".join(text_parts)
    has_copyright_class = any(
        c.startswith(_COPYRIGHT_CLASS_PREFIXES) for c in all_classes)

    # --- about the author -------------------------------------------------
    for ht in all_headings:
        if _ABOUT_AUTHOR.search(ht):
            return "about_author"
    if fname in _ABOUT_AUTHOR_FILENAME_HINTS:
        return "about_author"

    # --- front matter: epub:type, filename, classes, or text -------------
    if has_epub_front:
        return "front"

    if fname in _FRONT_FILENAME_HINTS:
        return "front"

    if all_classes & _COVER_CLASSES and block_count <= 2:
        return "front"

    if all_classes & _TITLE_PAGE_CLASSES:
        return "front"

    if all_classes & _TOC_CLASSES:
        return "front"

    # Short page whose heading is "Contents" -> table of contents.
    for ht in all_headings:
        if _TOC_HEADING.match(ht):
            return "front"

    # Copyright page: copy* classes, or copyright text on a short page.
    if has_copyright_class:
        return "front"
    if block_count <= 25 and _COPYRIGHT_TEXT.search(all_text):
        return "front"

    return "body"


def trim_to_body_start(blocks: list[Block], epub) -> list[Block]:
    """Drop front matter and 'about the author' documents.

    Any document classified as front matter (title page, cover, copyright,
    table of contents, dedication ...) or 'about the author' is removed,
    wherever it appears in the spine. Dropping by document type rather than
    by position means a single document that slips past the classifier can
    only leak itself -- it can no longer switch trimming off and drag every
    following front-matter document into the body (which is what let the
    copyright/TOC/bio pages reach text.json).

    Epigraphs or dedications that live inside a body chapter document are
    kept, because that document is classified 'body'.
    """
    doc_types = {}
    for b in blocks:
        if b.doc not in doc_types:
            doc_types[b.doc] = _classify_doc(epub, b.doc)

    return [b for b in blocks
            if doc_types[b.doc] not in ("front", "about_author")]


def extract_blocks(epub, skip_front_matter: bool = True) -> list[Block]:
    """Extract every block from an EPUB in spine (reading) order."""
    blocks: list[Block] = []
    sheet_cache: dict[tuple, Stylesheet] = {}
    for item in epub.spine:
        path = item["path"]
        if not epub.exists(path):
            continue
        root = epub.parse_xhtml(path)
        css_texts = epub.stylesheets_for(path, root)
        key = tuple(hash(c) for c in css_texts)
        sheet = sheet_cache.get(key)
        if sheet is None:
            sheet = Stylesheet()
            for css in css_texts:
                sheet.add_css(css)
            sheet_cache[key] = sheet
        blocks.extend(DocumentExtractor(epub, path, sheet).run(root))
    if skip_front_matter:
        blocks = trim_to_body_start(blocks, epub)
    return blocks
