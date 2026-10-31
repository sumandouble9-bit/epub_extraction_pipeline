from __future__ import annotations

import dataclasses
import hashlib
import io
import os
import posixpath
import re

import formulas
from epub_parser import Run, extract_blocks, order_styles
from profile import Profile

_LIST_MARKER = re.compile(r"^\s*\(?[0-9a-z]{1,3}[).\]]\s+", re.I)
_WORD_RE = re.compile(r"[^\s—–\-_/|\\]+")
_ALLCAPS_MIN = 2

try:
    from PIL import Image
except ImportError:  # pragma: no cover - Pillow is an optional nicety
    Image = None


def _apply_script_markers(text: str, runs: list[Run]) -> str:
    """Insert ^(...) for superscript and _(...) for subscript runs into text."""
    script_spans: dict[str, list[tuple[int, int]]] = {"^": [], "_": []}
    for r in runs:
        if not r.styles:
            continue
        styles = set(r.styles) if not isinstance(r.styles, set) else r.styles
        if "superscript" in styles:
            script_spans["^"].append((r.start, r.end))
        elif "subscript" in styles:
            script_spans["_"].append((r.start, r.end))
    merged = []
    for marker, spans in script_spans.items():
        if not spans:
            continue
        spans.sort()
        cur_start, cur_end = spans[0]
        for s, e in spans[1:]:
            gap = text[cur_end:s]
            if s <= cur_end or not any(ch.isalpha() for ch in gap):
                cur_end = max(cur_end, e)
            else:
                merged.append((cur_start, cur_end, marker))
                cur_start, cur_end = s, e
        merged.append((cur_start, cur_end, marker))
    if not merged:
        return text
    merged.sort(key=lambda s: s[0])
    parts = []
    prev = 0
    for start, end, marker in merged:
        parts.append(text[prev:start])
        content = text[start:end].strip()
        if len(content) == 1:
            parts.append(marker + content)
        elif content.startswith("(") and content.endswith(")"):
            parts.append(marker + content)
        else:
            parts.append(marker + "(" + content + ")")
        prev = end
    parts.append(text[prev:])
    return "".join(parts)


def _is_caps_word(word: str) -> bool:
    letters = [ch for ch in word if ch.isalpha()]
    return len(letters) >= _ALLCAPS_MIN and all(ch.isupper() for ch in letters)


def _apply_word_allcaps(text: str, runs: list[Run],
                        base_styles: frozenset = frozenset()) -> list[Run]:
    """Detect per-word allcaps and split runs so each caps span gets the flag."""
    words = list(_WORD_RE.finditer(text))
    if not words:
        return runs

    caps_set = {i for i, m in enumerate(words) if _is_caps_word(m.group())}
    if not caps_set:
        return runs

    if len(caps_set) == len(words):
        if not runs:
            return [Run(text=text,
                        styles=order_styles(set(base_styles) | {"allcaps"}),
                        index=list(range(1, len(words) + 1)),
                        start=0, end=len(text))]
        return [dataclasses.replace(r, styles=order_styles(set(r.styles) | {"allcaps"}))
                for r in runs]

    if not runs:
        return _create_caps_runs(text, words, caps_set, base_styles)
    return _split_caps_runs(text, runs, words, caps_set)


def _create_caps_runs(text, words, caps_set, base_styles):
    result = []
    i = 0
    n = len(words)
    while i < n:
        is_caps = i in caps_set
        start_i = i
        while i < n and (i in caps_set) == is_caps:
            i += 1
        char_start = 0 if start_i == 0 else words[start_i].start()
        char_end = len(text) if i >= n else words[i].start()
        styles = set(base_styles)
        if is_caps:
            styles.add("allcaps")
        result.append(Run(
            text=text[char_start:char_end],
            styles=order_styles(styles),
            index=list(range(start_i + 1, i + 1)),
            start=char_start, end=char_end,
        ))
    return result


def _split_caps_runs(text, runs, words, caps_set):
    result = []
    for run in runs:
        run_words = [(i, m) for i, m in enumerate(words)
                     if m.start() >= run.start and m.end() <= run.end]
        if not run_words:
            result.append(run)
            continue
        has_caps = any(i in caps_set for i, _ in run_words)
        all_caps = all(i in caps_set for i, _ in run_words)
        if not has_caps:
            result.append(run)
        elif all_caps:
            result.append(dataclasses.replace(
                run, styles=order_styles(set(run.styles) | {"allcaps"})))
        else:
            j = 0
            while j < len(run_words):
                is_caps = run_words[j][0] in caps_set
                start_j = j
                while j < len(run_words) and (run_words[j][0] in caps_set) == is_caps:
                    j += 1
                char_start = run.start if start_j == 0 else run_words[start_j][1].start()
                char_end = run.end if j >= len(run_words) else run_words[j][1].start()
                styles = set(run.styles)
                if is_caps:
                    styles.add("allcaps")
                result.append(Run(
                    text=text[char_start:char_end],
                    styles=order_styles(styles),
                    index=[wi + 1 for wi, _ in run_words[start_j:j]],
                    start=char_start, end=char_end,
                ))
    return result

ALIGN_MAP = {
    "center": "CenterAlign",
    "right": "RightAlign",
    "justify": "LeftJustified",
    "left": "LeftAlign",
    "start": "LeftAlign",
    "end": "RightAlign",
}


class Result:
    def __init__(self, items, equations, images, stats):
        self.items = items
        self.equations = equations
        self.images = images
        self.stats = stats


class Builder:
    def __init__(self, epub, profile: Profile, book_name: str,
                 table_formula_prompt: str = "latex",
                 max_formula_words: int = 15,
                 detect_formulas: bool = True,
                 skip_front_matter: bool = True,
                 infer_allcaps: bool | None = None):
        self.epub = epub
        self.profile = profile
        self.book_name = book_name
        self.table_formula_prompt = table_formula_prompt
        self.max_formula_words = max_formula_words
        self.detect_formulas = detect_formulas
        self.skip_front_matter = skip_front_matter
        self.infer_allcaps = (profile.infer_allcaps if infer_allcaps is None
                              else infer_allcaps)
        self._hashes: set[str] = set()
        self._image_cache: dict[str, dict] = {}


    def build(self) -> Result:
        items: list[dict] = []
        equations: list[dict] = []
        used_images: dict[str, str] = {}
        stats = {"images": 0}

        psr_id = -1
        previous_style = object()
        previous_doc = None
        table_index = 0
        pending_images: list = []

        for block in extract_blocks(self.epub, skip_front_matter=self.skip_front_matter):
            if block.kind == "image":
                pending_images.extend(block.images)
                continue

            if block.kind == "table":
                psr_id += 1
                produced = [self._table_item(block, table_index, psr_id)]
                table_index += 1
            else:
                style = self._resolve_style(block)
                if style != previous_style or block.doc != previous_doc:
                    psr_id += 1
                previous_style, previous_doc = style, block.doc
                produced = self._paragraph_items(block, style, psr_id)

            if not produced:
                continue

            images = pending_images + list(block.images)
            pending_images = []
            if images:
                payload = [p for p in (self._image_payload(ref, produced[0][0], block)
                                       for ref in images) if p]
                if payload:
                    self._attach_image(produced[0][0], payload)
                    stats["images"] += len(payload)
                    for ref in images:
                        used_images[ref.path] = posixpath.basename(ref.path)

            for item, item_equations in produced:
                items.append(item)
                equations.extend(item_equations)
                stats[item["type"]] = stats.get(item["type"], 0) + 1

        self._number_messages(items, equations)
        stats["table_formula"] = sum(1 for e in equations if e["type"] == "table_formula")
        return Result(items, equations, used_images, stats)

    def _number_messages(self, items, equations) -> None:
        """message_index runs sequentially over the final item list."""
        start = self.profile.message_index_start
        for offset, item in enumerate(items):
            item["message_index"] = start + offset
        for equation in equations:
            owner = equation.pop("_owner", None)
            if owner is not None and "message_index" in equation:
                equation["message_index"] = owner["message_index"]


    def _resolve_style(self, block) -> str:
        return self.profile.applied_style(block)

    def _paragraph_items(self, block, style, psr_id):
        produced = []
        for text, runs in self._split_heading(block):
            if not text.strip() and not block.images:
                continue
            produced.append(self._one_paragraph(block, text, runs, style, psr_id))
        return produced

    @staticmethod
    def _consolidate_runs(text, runs):
        """Merge consecutive runs that share a dominant style; emit extras separately."""
        styled = [r for r in runs if r.styles]
        if not styled:
            return []

        style_counts = {}
        for r in styled:
            style_counts[r.styles] = style_counts.get(r.styles, 0) + len(r.text)

        dominant = max(style_counts, key=style_counts.get)
        dominant_set = set(dominant)

        segments = []
        merge_group = []

        def _flush_merge():
            if not merge_group:
                return
            combined_index = []
            for r in merge_group:
                combined_index.extend(r.index)
            start = merge_group[0].start
            end = merge_group[-1].end
            segments.append(Run(
                text=text[start:end],
                styles=dominant,
                index=combined_index,
                start=start,
                end=end,
            ))
            for r in merge_group:
                extras = order_styles(
                    [s for s in r.styles if s not in dominant_set])
                if extras:
                    segments.append(Run(
                        text=r.text,
                        styles=extras,
                        index=r.index,
                        start=r.start,
                        end=r.end,
                    ))
            merge_group.clear()

        for r in styled:
            is_superset = dominant_set <= set(r.styles)
            if is_superset:
                if merge_group:
                    prev = merge_group[-1]
                    gap = text[prev.end:r.start] if prev.end <= r.start else ""
                    if any(c.isalnum() for c in gap):
                        _flush_merge()
                merge_group.append(r)
            else:
                _flush_merge()
                segments.append(r)

        _flush_merge()
        return segments

    def _one_paragraph(self, block, text, runs, style, psr_id):
        runs = _apply_word_allcaps(text, runs, block.base_styles)
        original_runs = runs
        runs = self._consolidate_runs(text, runs)
        prompt = formulas.normalize_prompt(text)
        heading_type = self.profile.heading_type(block)
        if self.detect_formulas and not heading_type:
            classification = formulas.classify(text, original_runs, self.max_formula_words)
        else:
            classification = formulas.Classification(formulas.TEXT)

        item: dict = {"type": "heading" if heading_type else classification.kind}
        if heading_type:
            item["heading_type"] = heading_type
        item["applied_style"] = style
        item["psr_id"] = psr_id
        item["token_count"] = len(prompt.split())
        item["prompt_text"] = prompt
        item["runs"] = [self._run_payload(r) for r in runs if r.styles]
        item["message_index"] = self.profile.message_index_start
        if self.profile.emit_summary and prompt.strip():
            item["chunk_context_summary"] = ""

        equations = []
        if not heading_type and classification.kind == formulas.FORMULA:
            hash_id = self._hash("%s|%s|%s" % (block.doc, style, text))
            item["hash_id"] = hash_id
            item["prompt_text"] = formulas.anchor(hash_id)
            latex = formulas.runs_to_latex(text, original_runs)
            equations.append({
                "hash_id": hash_id,
                "type": formulas.FORMULA,
                "applied_style": style,
                "psr_id": psr_id,
                "message_index": 0,
                "prompt_text": prompt,
                "latex": latex,
                "_owner": item,
            })
        elif not heading_type and classification.kind == formulas.TEXT_WITH_FORMULA:
            all_spans = formulas.extract_all_equations(text)
            if not all_spans:
                all_spans = [(classification.start, classification.end)]

            hash_ids = []
            parts = []
            prev_end = 0
            for span_start, span_end in all_spans:
                hash_id = self._hash("%s|%s|%s|%d" % (block.doc, style, text, span_start))
                hash_ids.append(hash_id)
                parts.append(formulas.normalize_prompt(text[prev_end:span_start]))
                parts.append(formulas.anchor(hash_id))
                prev_end = span_end
                equations.append({
                    "hash_id": hash_id,
                    "type": formulas.TEXT_WITH_FORMULA,
                    "applied_style": style,
                    "psr_id": psr_id,
                    "message_index": 0,
                    "prompt_text": prompt,
                    "latex": formulas.runs_to_latex(text, original_runs, span_start, span_end),
                    "_owner": item,
                })
            parts.append(formulas.normalize_prompt(text[prev_end:]))
            item["prompt_text"] = "".join(parts)
            item["hash_id"] = hash_ids
        return item, equations

    def _split_heading(self, block):
        marker = self.profile.heading_split
        if not marker or not self.profile.heading_type(block) or marker not in block.text:
            return [(block.text, block.runs)]
        head, _, tail = block.text.partition(marker)
        head, tail = head.strip(), tail.strip()
        if not head or not tail:
            return [(block.text, block.runs)]
        return [(head, []), (tail, [])]

    def _run_payload(self, run) -> dict:
        styles, idml = self.profile.resolve_run(run.styles)
        payload = {"text": run.text, "char_style": list(styles)}
        if idml:
            payload["idml_char_style"] = idml
        payload["index"] = run.index
        return payload


    def _cell_equations(self, cell, table_index, nested_path="") -> list[dict]:
        """One equation per line of a cell, including nested tables inside it."""
        out = []
        prefix = ("%s/" % nested_path) if nested_path else ""
        lines = cell.get("lines") or [{"text": cell["text"], "runs": cell["runs"]}]
        for line_index, line in enumerate(lines):
            text = line["text"]
            if not text.strip():
                continue
            classification = formulas.classify(text, line["runs"],
                                               self.max_formula_words, table_cell=True)
            if classification.kind not in (formulas.FORMULA, formulas.TEXT_WITH_FORMULA):
                continue
            if classification.kind == formulas.FORMULA:
                eq_text = formulas.strip_equation_label(
                    formulas.runs_to_latex(text, line["runs"]))
            else:
                eq_text = formulas.strip_equation_label(
                    formulas.runs_to_latex(text, line["runs"],
                                           classification.start, classification.end))
            out.append({
                "hash_id": self._hash("t%d|%s%d|%d|%d|%s" % (
                    table_index, prefix, cell["row_index"], cell["col_index"], line_index, text)),
                "type": "table_formula",
                "table_index": table_index,
                "row_index": cell["row_index"],
                "col_index": cell["col_index"],
                "prompt_text": eq_text if self.table_formula_prompt == "latex"
                else formulas.normalize_prompt(text),
                "latex": eq_text,
            })
        for nest_index, nested in enumerate(cell.get("nested_tables") or []):
            for ncell in nested.cells:
                out.extend(self._cell_equations(
                    ncell, table_index,
                    nested_path="%s%d" % (prefix, nest_index)))
        return out

    def _serialize_cell(self, cell, table_index=None) -> dict:
        payload = {
            "row_index": cell["row_index"],
            "col_index": cell["col_index"],
            "text": cell["text"],
        }
        runs = list(cell["runs"])
        text = cell["text"]
        if text.strip():
            runs = _apply_word_allcaps(text, runs)
        runs = self._consolidate_runs(text, runs)
        styled = [r for r in runs if r.styles]
        if styled:
            payload["table_runs"] = []
            for r in styled:
                styles, idml = self.profile.resolve_run(r.styles)
                entry = {"text": r.text, "char_style": list(styles), "index": r.index}
                if idml:
                    entry["idml_char_style"] = idml
                payload["table_runs"].append(entry)
        lines = cell.get("lines") or []
        if any(line.get("list_marker") for line in lines):
            payload["lines"] = []
            for line in lines:
                entry = {"text": line["text"]}
                if line.get("list_marker"):
                    entry["list_marker"] = line["list_marker"]
                payload["lines"].append(entry)
        nested = cell.get("nested_tables") or []
        if nested:
            payload["nested_tables"] = [
                self._serialize_table(nb, nested_index=i)[0]
                for i, nb in enumerate(nested)
            ]
        return payload

    def _serialize_table(self, block, table_index=None, nested_index=None):
        cells = [self._serialize_cell(cell, table_index) for cell in block.cells]
        item = {
            "rows": block.rows,
            "columns": block.columns,
            "header_rows": block.header_rows,
            "cells": cells,
        }
        if nested_index is not None:
            item["nested_index"] = nested_index
        elif table_index is not None:
            item["table_index"] = table_index
        return item, cells

    def _cell_prompt(self, cell) -> str:
        parts = []
        if cell["text"].strip():
            parts.append(formulas.normalize_prompt(cell["text"]))
        for nested in cell.get("nested_tables") or []:
            grid: dict[int, list[str]] = {}
            for ncell in nested.cells:
                grid.setdefault(ncell["row_index"], []).append(self._cell_prompt(ncell))
            parts.append("\n".join(" | ".join(grid[row]) for row in sorted(grid)).strip())
        return " ".join(p for p in parts if p).strip()

    def _table_item(self, block, table_index, psr_id):
        cells = []
        equations = []
        grid: dict[int, list[str]] = {}

        for cell in block.cells:
            payload = self._serialize_cell(cell, table_index)
            cells.append(payload)
            grid.setdefault(cell["row_index"], []).append(self._cell_prompt(cell))

            if self.detect_formulas:
                equations.extend(self._cell_equations(cell, table_index))

        prompt_text = "\n".join(" | ".join(grid[row]) for row in sorted(grid)).strip()

        item = {
            "type": "table",
            "table_index": table_index,
            "psr_id": psr_id,
            "columns": block.columns,
            "rows": block.rows,
            "header_rows": block.header_rows,
            "cells": cells,
            "prompt_text": prompt_text,
            "message_index": self.profile.message_index_start,
        }
        if self.profile.emit_summary and prompt_text.strip():
            item["chunk_context_summary"] = ""
        return item, equations


    @staticmethod
    def _attach_image(item, payload) -> None:
        """Insert the image key just before message_index, matching the reference order."""
        value = payload[0] if len(payload) == 1 else payload
        rebuilt = {}
        for key, existing in item.items():
            if key == "message_index":
                rebuilt["image"] = value
            rebuilt[key] = existing
        item.clear()
        item.update(rebuilt)

    def _image_payload(self, ref, item, block) -> dict | None:
        if not ref.path or not self.epub.exists(ref.path):
            return None
        name = posixpath.basename(ref.path)
        info = self._image_cache.get(ref.path)
        if info is None:
            info = self._measure(ref)
            self._image_cache[ref.path] = info
        style = item.get("applied_style") or ""
        return {
            "type": "image",
            "link_name": name,
            "image_path": self.profile.image_path_template.format(book=self.book_name, name=name),
            "width_pt": info["width_pt"],
            "height_pt": info["height_pt"],
            "ppi": info["ppi"],
            "placement": "inline",
            "justification": ALIGN_MAP.get(block.align or "", "LeftAlign"),
            "paragraph_style": style.split("/", 1)[-1] if style else "",
            "caption": ref.alt or "",
            "space_before_pt": 0.0,
            "space_after_pt": 0.0,
        }

    def _measure(self, ref) -> dict:
        width_px = height_px = None
        ppi = 96.0
        if Image is not None:
            try:
                with Image.open(io.BytesIO(self.epub.read(ref.path))) as im:
                    width_px, height_px = im.size
                    dpi = im.info.get("dpi")
                    if dpi and dpi[0]:
                        ppi = float(dpi[0])
            except Exception:
                pass
        if width_px is None:
            try:
                width_px = float(ref.width_attr) if ref.width_attr else None
                height_px = float(ref.height_attr) if ref.height_attr else None
            except (TypeError, ValueError):
                width_px = height_px = None
        if not width_px or not height_px:
            return {"width_pt": 0.0, "height_pt": 0.0, "ppi": round(ppi)}
        return {
            "width_pt": round(width_px / ppi * 72.0, 2),
            "height_pt": round(height_px / ppi * 72.0, 2),
            "ppi": round(ppi),
        }


    def _hash(self, seed: str) -> str:
        candidate = hashlib.sha1(seed.encode("utf-8")).hexdigest()[:12]
        salt = 0
        while candidate in self._hashes:
            salt += 1
            candidate = hashlib.sha1(("%s#%d" % (seed, salt)).encode("utf-8")).hexdigest()[:12]
        self._hashes.add(candidate)
        return candidate


def extract_images(epub, images: dict, out_dir: str) -> int:
    os.makedirs(out_dir, exist_ok=True)
    count = 0
    for path, name in images.items():
        try:
            data = epub.read(path)
        except KeyError:
            continue
        with open(os.path.join(out_dir, name), "wb") as handle:
            handle.write(data)
        count += 1
    return count
