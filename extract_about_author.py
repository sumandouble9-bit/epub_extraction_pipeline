from __future__ import annotations

import json
import os
import sys

from epub_parser import (
    Block, DocumentExtractor, local_name,
    HEADING_TAGS, _ABOUT_AUTHOR,
)
from css_resolver import Stylesheet
from builder import Builder, _apply_word_allcaps
from epubdoc import Epub
from profile import Profile, find_for_book
import formulas

_EPUB_NS = "http://www.idpf.org/2007/ops"


def _is_about_author_doc(epub, path: str) -> bool:
    """True when the spine document is an 'About the Author' section."""
    root = epub.parse_xhtml(path)
    if root is None:
        return False
    for el in root.iter():
        tag = local_name(el.tag)
        if tag is None:
            continue
        if tag in HEADING_TAGS:
            heading_text = (el.text or "") + "".join(
                c.text or "" for c in el.iter() if c is not el)
            if _ABOUT_AUTHOR.search(heading_text.strip()):
                return True
    return False


def _extract_about_author_blocks(epub) -> list[Block]:
    """Return blocks only from 'About the Author' documents."""
    blocks: list[Block] = []
    sheet_cache: dict[tuple, Stylesheet] = {}

    for item in epub.spine:
        path = item["path"]
        if not epub.exists(path):
            continue
        if not _is_about_author_doc(epub, path):
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

    return blocks


def extract_about_author(epub, profile: Profile = None) -> list[dict]:
    """Build about_author items in the same format as text.json."""
    blocks = _extract_about_author_blocks(epub)
    if not blocks:
        return []

    if profile is None:
        name = os.path.splitext(os.path.basename(epub.path))[0].strip()
        profile = find_for_book(name)

    items: list[dict] = []
    psr_id = -1
    previous_style = object()
    previous_doc = None

    for block in blocks:
        if block.kind == "image":
            continue
        if block.kind == "table":
            continue

        style = profile.applied_style(block)
        if style != previous_style or block.doc != previous_doc:
            psr_id += 1
        previous_style, previous_doc = style, block.doc

        text = block.text
        if not text.strip():
            continue

        runs = _apply_word_allcaps(text, block.runs, block.base_styles)
        runs = Builder._consolidate_runs(text, runs)

        prompt = formulas.normalize_prompt(text)
        heading_type = profile.heading_type(block)

        item: dict = {"type": "heading" if heading_type else "text"}
        if heading_type:
            item["heading_type"] = heading_type
        item["applied_style"] = style
        item["psr_id"] = psr_id
        item["token_count"] = len(prompt.split())
        item["prompt_text"] = prompt

        styled_runs = []
        for r in runs:
            if not r.styles:
                continue
            styles_list, idml = profile.resolve_run(r.styles)
            payload = {"text": r.text, "char_style": list(styles_list)}
            if idml:
                payload["idml_char_style"] = idml
            payload["index"] = r.index
            styled_runs.append(payload)
        item["runs"] = styled_runs
        item["message_index"] = 0
        items.append(item)

    for i, it in enumerate(items):
        it["message_index"] = i

    return items


def extract_and_save(epub_path: str, out_dir: str = None) -> str | None:
    """Extract about_author from an EPUB and save as *_about_author.json."""
    epub = Epub(epub_path)
    name = os.path.splitext(os.path.basename(epub_path))[0].strip()
    profile = find_for_book(name)

    items = extract_about_author(epub, profile)
    if not items:
        return None

    if out_dir is None:
        out_dir = os.path.dirname(epub_path)
    os.makedirs(out_dir, exist_ok=True)

    out_path = os.path.join(out_dir, "%s_about_author.json" % name)
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(items, f, ensure_ascii=False, indent=1)

    return out_path


if __name__ == "__main__":
    from config import DEFAULT_INPUT_DIR, DEFAULT_OUTPUT_DIR

    if len(sys.argv) > 1:
        paths = [sys.argv[1]]
    else:
        paths = sorted(
            os.path.join(DEFAULT_INPUT_DIR, f)
            for f in os.listdir(DEFAULT_INPUT_DIR)
            if f.lower().endswith(".epub")
        )

    if not paths:
        print("No EPUB files found.")
        sys.exit(1)

    print("=" * 60)
    print("  About the Author Extractor")
    print("=" * 60)
    print("  Found %d EPUB file(s)\n" % len(paths))

    skipped = 0
    extracted = 0
    no_content = 0
    for path in paths:
        name = os.path.splitext(os.path.basename(path))[0]
        book_out = os.path.join(DEFAULT_OUTPUT_DIR, name)
        about_path = os.path.join(book_out, "%s_about_author.json" % name)
        if os.path.isfile(about_path):
            skipped += 1
            continue
        if not os.path.isdir(book_out):
            skipped += 1
            continue
        try:
            result = extract_and_save(path, book_out)
            if result:
                extracted += 1
                print("  %s -> %s" % (os.path.basename(path), os.path.basename(result)))
            else:
                no_content += 1
                print("  %s -> no about-author section found" % os.path.basename(path))
        except Exception as e:
            print("  %s -> ERROR: %s" % (os.path.basename(path), e))

    print("\n  Done: %d extracted, %d no content, %d skipped (already exist or no output folder)" % (extracted, no_content, skipped))