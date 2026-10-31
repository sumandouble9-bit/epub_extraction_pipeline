"""Extract all links (footnotes, references, etc.) from an EPUB and map to message_index.

Outputs a JSON array with fields: message_index, link_id, href_link, text
"""
import json
import os
import posixpath
import sys

from lxml import etree
from epubdoc import Epub


def _resolve_href(href, doc_path):
    """Resolve a relative href to the full EPUB document path (no fragment).

    '#fn-3' in 'OEBPS/xhtml/chapter6.xhtml' -> 'OEBPS/xhtml/chapter6.xhtml'
    'chapter7.xhtml#x' in 'OEBPS/xhtml/chapter6.xhtml' -> 'OEBPS/xhtml/chapter7.xhtml'
    """
    if not href or href.startswith("mailto:") or href.startswith("http"):
        return href
    if href.startswith("javascript:"):
        return href

    if "#" in href:
        href_part = href.split("#", 1)[0]
    else:
        href_part = href

    if not href_part:
        return doc_path
    doc_dir = posixpath.dirname(doc_path)
    return posixpath.normpath(posixpath.join(doc_dir, href_part))


def _resolve_data_href(el):
    """Extract real link target from data-* attributes (Calibre viewer replaces
    href with javascript:void(0) and stores the real target in a data-* attr)."""
    for k, v in el.attrib.items():
        if not k.startswith("data-"):
            continue
        try:
            obj = json.loads(v)
            if isinstance(obj, dict) and "name" in obj:
                return obj["name"]
        except (json.JSONDecodeError, TypeError):
            continue
    return ""


def _local_name(tag):
    if not isinstance(tag, str):
        return None
    return tag.rsplit("}", 1)[-1].lower()


def _text_content(el):
    return "".join(el.itertext()).strip()


def _get_paragraph_text(el):
    """Walk up to the nearest block-level parent and get its full text."""
    p_el = el
    while p_el is not None:
        pt = _local_name(p_el.tag)
        if pt in ("p", "h1", "h2", "h3", "h4", "h5", "h6", "li", "td", "th",
                   "blockquote", "div"):
            return "".join(p_el.itertext()).strip()
        p_el = p_el.getparent()
    return ""


def _extract_links_from_epub(epub):
    """Parse all XHTML docs and extract every <a> tag."""
    links = []
    for doc in epub.spine:
        path = doc["path"]
        root = epub.parse_xhtml(path)
        for el in root.iter():
            tag = _local_name(el.tag)
            if tag != "a":
                continue
            link_id = el.get("id", "")
            raw_href = el.get("href", "")
            text = _text_content(el)
            if not link_id and not raw_href:
                continue

            data_link = _resolve_href(raw_href, path)
            if raw_href.startswith("javascript:"):
                real = _resolve_data_href(el)
                if real:
                    data_link = real

            parent = el.getparent()
            parent_tag = _local_name(parent.tag) if parent is not None else ""
            is_sup = parent_tag == "sup"
            is_sub = parent_tag == "sub"

            link_type = "link"
            if is_sup:
                link_type = "superscript"
            elif is_sub:
                link_type = "subscript"
            elif "footnote" in link_id.lower() or "fn-" in link_id.lower() or "fn_" in link_id.lower():
                link_type = "footnote"
            elif "footnote" in raw_href.lower() or "fn-" in raw_href.lower() or "fn_" in raw_href.lower():
                link_type = "footnote"

            para_text = _get_paragraph_text(el)

            links.append({
                "doc": path,
                "link_id": link_id,
                "href_link": raw_href,
                "data_link": data_link,
                "text": text,
                "link_type": link_type,
                "para_text": para_text,
            })
    return links


def _normalize_ws(text):
    """Replace non-breaking spaces and collapse whitespace for matching."""
    return " ".join(text.replace("\xa0", " ").split())


def _match_message_index(links, items):
    """Match each link to message_index by finding its paragraph text in items."""
    norm_prompts = []
    for item in items:
        prompt = item.get("prompt_text", "")
        norm_prompts.append((_normalize_ws(prompt), item.get("message_index", "")))

    for link in links:
        link["message_index"] = ""
        para = _normalize_ws(link["para_text"])

        if not para:
            continue

        for norm_prompt, idx in norm_prompts:
            if not norm_prompt:
                continue
            if para in norm_prompt:
                link["message_index"] = idx
                break


def extract_links(epub_path, text_json_path=None):
    """Main extraction: returns list of link dicts."""
    epub = Epub(epub_path)
    links = _extract_links_from_epub(epub)

    if text_json_path and os.path.isfile(text_json_path):
        with open(text_json_path, "r", encoding="utf-8") as f:
            items = json.load(f)
        _match_message_index(links, items)

    result = []
    for link in links:
        result.append({
            "message_index": link.get("message_index", ""),
            "link_id": link["link_id"],
            "href_link": link["href_link"],
            "data_link": link["data_link"],
            "text": link["text"],
            "link_type": link["link_type"],
        })
    return result


from config import DEFAULT_INPUT_DIR, DEFAULT_OUTPUT_DIR
# DEFAULT_INPUT_DIR = "input"
# DEFAULT_OUTPUT_DIR = "output"
# DEFAULT_INPUT_DIR = r"\\192.168.4.28\Common\Uday\3698 Files\Output epub"
# DEFAULT_OUTPUT_DIR = r"\\192.168.4.28\Common\Suman Kumar\extracted_epub_josn_files1"


def main():
    if len(sys.argv) > 1 and sys.argv[1] == "--all":
        return _extract_all()

    if len(sys.argv) > 1:
        epub_path = sys.argv[1]
        name = os.path.splitext(os.path.basename(epub_path))[0].strip()
        text_json = os.path.join(DEFAULT_OUTPUT_DIR, name, "%s_text.json" % name)
        out_path = os.path.join(DEFAULT_OUTPUT_DIR, name, "%s_links.json" % name)
    else:
        print("Usage: python extract_links.py <epub_path>")
        print("   or: python extract_links.py --all  (extract links for all books)")
        return 1

    links = extract_links(epub_path, text_json)
    os.makedirs(os.path.dirname(out_path), exist_ok=True)
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(links, f, ensure_ascii=False, indent=1)
    print("%s -> %d links -> %s" % (name, len(links), os.path.basename(out_path)))
    return 0


def _extract_all():
    """Extract links for all books that have _text.json in output."""
    if not os.path.isdir(DEFAULT_OUTPUT_DIR):
        print("Output dir not found: %s" % DEFAULT_OUTPUT_DIR)
        return 1

    total = 0
    for folder_name in sorted(os.listdir(DEFAULT_OUTPUT_DIR)):
        folder = os.path.join(DEFAULT_OUTPUT_DIR, folder_name)
        if not os.path.isdir(folder):
            continue
        text_json = os.path.join(folder, "%s_text.json" % folder_name)
        links_json = os.path.join(folder, "%s_links.json" % folder_name)
        if not os.path.isfile(text_json):
            continue
        if os.path.isfile(links_json):
            continue
        epub_path = os.path.join(DEFAULT_INPUT_DIR, "%s.epub" % folder_name)
        if not os.path.isfile(epub_path):
            epub_path = os.path.join(DEFAULT_INPUT_DIR, "%s .epub" % folder_name)
            if not os.path.isfile(epub_path):
                continue

        try:
            links = extract_links(epub_path, text_json)
            with open(links_json, "w", encoding="utf-8") as f:
                json.dump(links, f, ensure_ascii=False, indent=1)
            print("%s -> %d links" % (folder_name, len(links)))
            total += 1
        except Exception as e:
            print("FAILED %s: %s" % (folder_name, e))

    print("\nDone: %d books processed" % total)
    return 0


if __name__ == "__main__":
    sys.exit(main())
