from __future__ import annotations

import json
import os
import re
import sys

from lxml import etree

from epubdoc import Epub

XHTML_NS = "http://www.w3.org/1999/xhtml"
from config import DEFAULT_INPUT_DIR, DEFAULT_OUTPUT_DIR

TITLE_CLASSES = {
    "title", "subtitle", "author", "booktitle", "book-title",
    "halftitle", "half-title", "publisher", "imprint", "edition",
    "dedication", "epigraph", "series", "contributor", "editor",
    "translator", "illustrator", "btext",
}

TITLE_FILENAME_HINTS = {"title", "titlepage", "halftitle", "half-title"}

SKIP_TAGS = {"script", "style", "head", "meta", "link"}

STYLE_TAGS = {"b": "bold", "strong": "bold", "i": "italic", "em": "italic",
              "cite": "italic", "u": "underline", "ins": "underline",
              "sup": "superscript", "sub": "subscript"}


def local_name(tag) -> str | None:
    if not isinstance(tag, str):
        return None
    if "}" in tag:
        return tag.split("}", 1)[1]
    return tag


def _get_classes(el) -> list[str]:
    raw = (el.get("class") or "").strip()
    return raw.split() if raw else []


def _extract_text(el) -> str:
    parts = []
    if el.text:
        parts.append(el.text)
    for child in el:
        tag = local_name(child.tag)
        if tag is None or tag in SKIP_TAGS:
            if child.tail:
                parts.append(child.tail)
            continue
        if tag == "br":
            parts.append("\n")
        else:
            inner = _extract_text(child)
            if inner:
                parts.append(inner)
        if child.tail:
            parts.append(child.tail)
    return "".join(parts)


def _get_char_styles(el) -> list[str]:
    styles = []
    for child in el.iter():
        tag = local_name(child.tag)
        if tag in ("b", "strong") and "bold" not in styles:
            styles.append("bold")
        elif tag in ("i", "em", "cite") and "italic" not in styles:
            styles.append("italic")
        elif tag in ("u", "ins") and "underline" not in styles:
            styles.append("underline")
        elif tag == "sup" and "superscript" not in styles:
            styles.append("superscript")
        elif tag == "sub" and "subscript" not in styles:
            styles.append("subscript")
    return styles


def _parse_inline_style(el) -> dict[str, str]:
    """Extract CSS properties from an element's inline style attribute."""
    raw = el.get("style", "")
    if not raw:
        return {}
    props = {}
    for decl in raw.split(";"):
        decl = decl.strip()
        if ":" in decl:
            k, v = decl.split(":", 1)
            props[k.strip().lower()] = v.strip()
    return props


def _resolve_font_size(el, css_map: dict) -> str | None:
    """Get font-size for an element from inline style or CSS classes."""
    inline = _parse_inline_style(el)
    if "font-size" in inline:
        return inline["font-size"]
    classes = _get_classes(el)
    for cls in classes:
        if cls in css_map:
            return css_map[cls]
    return None


def _build_css_font_size_map(epub: Epub, root, doc_path: str) -> dict[str, str]:
    """Parse stylesheets and build a map of class name -> font-size value."""
    css_map = {}
    try:
        sheets = epub.stylesheets_for(doc_path, root)
        for css_text in sheets:
            for match in re.finditer(r'\.(\w[\w-]*)\s*\{([^}]*)\}', css_text):
                cls_name = match.group(1)
                body = match.group(2)
                for prop in body.split(";"):
                    prop = prop.strip()
                    if prop.lower().startswith("font-size"):
                        _, _, val = prop.partition(":")
                        css_map[cls_name] = val.strip()
    except Exception:
        pass
    return css_map


def _get_inherited_styles(el) -> list[str]:
    """Get style tags from ancestor chain of an element."""
    styles = []
    node = el
    while node is not None:
        tag = local_name(node.tag) if hasattr(node, 'tag') else None
        if tag in STYLE_TAGS and STYLE_TAGS[tag] not in styles:
            styles.append(STYLE_TAGS[tag])
        parent = node.getparent()
        node = parent
    return styles


def _extract_runs(el, css_map: dict, parent_styles: list[str] | None = None) -> list[dict]:
    """Extract styled text runs from an element, preserving font-size variations."""
    if parent_styles is None:
        parent_styles = []

    runs = []
    tag = local_name(el.tag)

    current_styles = list(parent_styles)
    if tag in STYLE_TAGS and STYLE_TAGS[tag] not in current_styles:
        current_styles.append(STYLE_TAGS[tag])

    font_size = _resolve_font_size(el, css_map)

    if el.text:
        run = {"text": el.text, "styles": list(current_styles)}
        if font_size:
            run["font_size"] = font_size
        runs.append(run)

    for child in el:
        child_tag = local_name(child.tag)
        if child_tag is None or child_tag in SKIP_TAGS:
            if child.tail:
                run = {"text": child.tail, "styles": list(current_styles)}
                if font_size:
                    run["font_size"] = font_size
                runs.append(run)
            continue

        if child_tag == "br":
            runs.append({"text": "\n", "styles": list(current_styles)})
        else:
            child_runs = _extract_runs(child, css_map, current_styles)
            runs.extend(child_runs)

        if child.tail:
            run = {"text": child.tail, "styles": list(current_styles)}
            if font_size:
                run["font_size"] = font_size
            runs.append(run)

    return runs


def _has_style_variation(runs: list[dict]) -> bool:
    """Check if runs have any font_size variation."""
    return any("font_size" in r for r in runs)


def _clean_runs(runs: list[dict]) -> list[dict]:
    """Merge adjacent runs with identical styles and clean up whitespace."""
    if not runs:
        return []

    cleaned = []
    for run in runs:
        text = run["text"]
        if not text:
            continue
        text = re.sub(r"[ \t]+", " ", text)
        run = dict(run)
        run["text"] = text
        if cleaned and cleaned[-1].get("styles") == run.get("styles") and \
           cleaned[-1].get("font_size") == run.get("font_size"):
            cleaned[-1]["text"] += run["text"]
        else:
            cleaned.append(run)

    result = []
    for run in cleaned:
        text = run["text"]
        if text.strip() or text == "\n":
            result.append(run)
        elif result:
            result[-1]["text"] += text

    return result


def _title_doc_candidates(epub: Epub) -> list[tuple]:
    """Return title-page candidate (root, path) pairs in priority order."""
    candidates: list[tuple] = []
    seen: set[str] = set()

    for doc in epub.spine[:6]:
        fname = os.path.splitext(os.path.basename(doc["path"]))[0].lower()
        if fname in TITLE_FILENAME_HINTS or "title" in fname:
            if doc["path"] in seen:
                continue
            root = epub.parse_xhtml(doc["path"])
            if root is not None:
                seen.add(doc["path"])
                candidates.append((root, doc["path"]))

    for doc in epub.spine[:6]:
        if doc["path"] in seen:
            continue
        root = epub.parse_xhtml(doc["path"])
        if root is None:
            continue
        body = root.find(".//{%s}body" % XHTML_NS)
        if body is None:
            for el in root.iter():
                if local_name(el.tag) == "body":
                    body = el
                    break
        if body is None:
            continue
        for el in body.iter():
            classes = set(_get_classes(el))
            if classes & TITLE_CLASSES:
                seen.add(doc["path"])
                candidates.append((root, doc["path"]))
                break
    return candidates


def _find_body_content(root):
    body = root.find(".//{%s}body" % XHTML_NS)
    if body is None:
        for el in root.iter():
            if local_name(el.tag) == "body":
                body = el
                break
    if body is None:
        return []
    elements = []
    _collect_block_elements(body, elements)
    return elements


def _collect_block_elements(parent, result):
    for child in parent:
        tag = local_name(child.tag)
        if tag is None or tag in SKIP_TAGS:
            continue
        if tag in ("article", "section", "div", "main", "header", "nav"):
            _collect_block_elements(child, result)
        elif tag in ("p", "h1", "h2", "h3", "h4", "h5", "h6", "blockquote", "figure", "figcaption"):
            result.append(child)
        elif tag == "img":
            result.append(child)


def _extract_items(epub: Epub, root, doc_path: str) -> list[dict]:
    elements = _find_body_content(root)
    css_map = _build_css_font_size_map(epub, root, doc_path)

    items = []
    for el in elements:
        tag = local_name(el.tag)
        if tag == "img":
            src = el.get("src", "")
            alt = el.get("alt", "")
            items.append({"class": "image", "tag": "img", "text": alt or src})
            continue

        classes = _get_classes(el)
        text = _extract_text(el).strip()
        text = re.sub(r"[ \t]+", " ", text)
        text = re.sub(r" *\n *", "\n", text)
        if not text:
            continue

        char_styles = _get_char_styles(el)
        class_str = " ".join(classes) if classes else tag

        item = {
            "class": class_str,
            "tag": tag,
            "text": text,
        }
        if char_styles:
            item["char_style"] = char_styles

        runs = _extract_runs(el, css_map)
        runs = _clean_runs(runs)
        if _has_style_variation(runs) and len(runs) > 1:
            item["runs"] = runs

        items.append(item)

    return items


def extract_title_page(epub: Epub) -> tuple[list[dict], str | None]:
    """Extract the title page and report which source file it came from."""
    for root, doc_path in _title_doc_candidates(epub):
        items = _extract_items(epub, root, doc_path)
        if items:
            return items, os.path.basename(doc_path)
    return [], None


def main():
    if len(sys.argv) > 1:
        paths = sys.argv[1:]
    else:
        paths = sorted(
            os.path.join(DEFAULT_INPUT_DIR, f)
            for f in os.listdir(DEFAULT_INPUT_DIR)
            if f.lower().endswith(".epub")
        )

    if not paths:
        print("No EPUB files found.", file=sys.stderr)
        return 1

    extracted = 0
    empty = 0
    for path in paths:
        if not os.path.isfile(path):
            print("skip (not found): %s" % path, file=sys.stderr)
            continue

        name = os.path.splitext(os.path.basename(path))[0].strip()
        try:
            epub = Epub(path)
            items, source = extract_title_page(epub)
        except Exception as e:
            print("FAILED %s: %s" % (name, e), file=sys.stderr)
            continue

        out_dir = os.path.join(DEFAULT_OUTPUT_DIR, name)
        os.makedirs(out_dir, exist_ok=True)
        out_path = os.path.join(out_dir, "%s_title.json" % name)

        output = {"metadata": source, "items": items}
        with open(out_path, "w", encoding="utf-8") as f:
            json.dump(output, f, ensure_ascii=False, indent=1)

        if items:
            extracted += 1
            print("%s -> %d elements (%s) -> %s"
                  % (name, len(items), source, os.path.basename(out_path)))
        else:
            empty += 1
            print("%s -> NO title page found" % name)

    print("\nDone: %d extracted, %d empty" % (extracted, empty))
    return 0


if __name__ == "__main__":
    sys.exit(main() or 0)
