from __future__ import annotations

import collections
import difflib
import json
import os
import re

from epubdoc import Epub
from epub_parser import extract_blocks, order_styles
from profile import DEFAULT_PROFILE, Profile

_ANCHOR = re.compile(r"\[formula_anchor_([0-9a-zA-Z]+)\]")


def _normalize(text: str) -> str:
    text = re.sub(r"\s+", " ", text or "").strip().lower()
    for src, dst in [("\u2018", "'"), ("\u2019", "'"), ("\u201c", '"'), ("\u201d", '"'),
                     ("\u2013", "-"), ("\u2014", "-"), ("\u2017", "-"), ("\u00d7", "x")]:
        text = text.replace(src, dst)
    text = re.sub(r"[^a-z0-9 ]+", "", text)
    return re.sub(r"\s+", " ", text).strip()


def _restore(item, equations_by_hash) -> str:
    text = item.get("prompt_text") or ""
    if item.get("type") == "formula":
        match = _ANCHOR.fullmatch(text.strip())
        if match and match.group(1) in equations_by_hash:
            return equations_by_hash[match.group(1)]["prompt_text"]
    return _ANCHOR.sub(
        lambda m: equations_by_hash.get(m.group(1), {}).get("latex", m.group(0)), text)


def learn_profile(epub_path: str, reference_path: str, equations_path: str | None = None,
                  name: str | None = None, min_support: int = 2,
                  min_confidence: float = 0.55,
                  implied_threshold: float = 0.9) -> tuple[Profile, dict]:
    epub = Epub(epub_path)
    blocks = extract_blocks(epub)

    with open(reference_path, encoding="utf-8") as handle:
        reference = json.load(handle)
    equations = []
    if equations_path:
        with open(equations_path, encoding="utf-8") as handle:
            equations = json.load(handle)
    by_hash = {e["hash_id"]: e for e in equations}

    left = ["<<TABLE>>" if b.kind == "table" else _normalize(b.text) for b in blocks]
    right = ["<<TABLE>>" if it.get("type") == "table" else _normalize(_restore(it, by_hash))
             for it in reference]

    matcher = difflib.SequenceMatcher(a=left, b=right, autojunk=False)
    pairs = []
    for tag, i1, i2, j1, j2 in matcher.get_opcodes():
        if tag != "equal":
            continue
        for offset in range(i2 - i1):
            pairs.append((blocks[i1 + offset], reference[j1 + offset]))

    para_votes = collections.defaultdict(collections.Counter)
    heading_votes = collections.defaultdict(collections.Counter)
    char_votes = collections.defaultdict(collections.Counter)

    for block, item in pairs:
        if block.kind != "para":
            continue
        keys = Profile.style_keys(block)
        key = keys[0]
        if item.get("applied_style"):
            para_votes[key][item["applied_style"]] += 1
        heading_votes[key][item.get("heading_type") if item.get("type") == "heading" else None] += 1

    implied_votes = collections.defaultdict(collections.Counter)
    implied_totals = collections.Counter()
    for item in reference:
        for run in item.get("runs") or []:
            styles = order_styles(run.get("char_style") or [])
            combo = "+".join(styles)
            idml = run.get("idml_char_style")
            if combo and idml:
                char_votes[combo][idml] += 1
                implied_totals[idml] += 1
                for flag in styles:
                    implied_votes[idml][flag] += 1

    paragraph_styles = {}
    for key, counter in para_votes.items():
        style, votes = counter.most_common(1)[0]
        total = sum(counter.values())
        if (votes >= min_support or total == 1) and votes / total >= min_confidence:
            paragraph_styles[key] = style

    headings = {}
    for key, counter in heading_votes.items():
        value, votes = counter.most_common(1)[0]
        total = sum(counter.values())
        if value and votes >= min_support and votes / total >= min_confidence:
            headings[key] = value

    char_styles = {}
    for combo, counter in char_votes.items():
        value, votes = counter.most_common(1)[0]
        total = sum(counter.values())
        if votes >= min_support and votes / total >= min_confidence:
            char_styles[combo] = value

    char_style_implies = {}
    for idml, flags in implied_votes.items():
        total = implied_totals[idml]
        always = order_styles([f for f, n in flags.items() if n / total >= implied_threshold])
        if always:
            char_style_implies[idml] = list(always)

    allcaps_hits = allcaps_total = 0
    for block, item in pairs:
        if block.kind != "para":
            continue
        letters = [ch for ch in block.text if ch.isalpha()]
        if len(letters) < 3 or not all(ch.isupper() for ch in letters):
            continue
        allcaps_total += 1
        if any("allcaps" in (run.get("char_style") or []) for run in item.get("runs") or []):
            allcaps_hits += 1
    infer_allcaps = allcaps_total > 0 and allcaps_hits / allcaps_total >= 0.5

    baseline_votes = collections.defaultdict(collections.Counter)
    run_style_votes = collections.defaultdict(collections.Counter)
    run_style_agreed = collections.Counter()
    for block, item in pairs:
        style = item.get("applied_style")
        if block.kind != "para" or not style:
            continue
        baseline = set(block.base_styles)
        covering = None
        for run in block.runs:
            if _normalize(run.text) == _normalize(block.text):
                covering = run
                baseline |= set(run.styles)
        if covering is not None and item.get("type") != "heading":
            tag_style = None
            for key in Profile.style_keys(block):
                if key in paragraph_styles:
                    tag_style = paragraph_styles[key]
                    break
            combo = "+".join(order_styles(covering.styles))
            run_style_votes[combo][style] += 1
            if tag_style == style:
                run_style_agreed[combo] += 1
        if not baseline:
            continue
        for run in item.get("runs") or []:
            if _normalize(run.get("text", "")) != _normalize(block.text):
                continue
            present = set(run.get("char_style") or [])
            for flag in baseline:
                baseline_votes[style]["%s%s" % ("+" if flag in present else "-", flag)] += 1

    base_style_overrides = {}
    for style, counter in baseline_votes.items():
        dropped = order_styles([
            vote[1:] for vote in counter
            if vote.startswith("-") and counter[vote] >= min_support
            and not counter["+" + vote[1:]]
        ])
        if dropped:
            base_style_overrides[style] = list(dropped)

    run_style_paragraph_styles = {}
    for combo, counter in run_style_votes.items():
        style, votes = counter.most_common(1)[0]
        total = sum(counter.values())
        if (combo.count("+") >= 1 and votes >= min_support
                and votes / total >= 0.9 and run_style_agreed[combo] / total <= 0.1):
            run_style_paragraph_styles[combo] = style

    def _latex_style(entries) -> str:
        escaped = plain = 0
        for entry in entries:
            latex = entry.get("latex") or ""
            source = entry.get("prompt_text") or ""
            if "\\times" in latex or "\\%" in latex:
                escaped += 1
            elif re.search(r"\d\s*x\s*\d", latex) or ("%" in latex and "%" in source):
                plain += 1
        return "plain" if plain > escaped else "latex"

    latex_style = _latex_style([e for e in equations if e.get("type") != "table_formula"])
    table_latex_style = _latex_style([e for e in equations if e.get("type") == "table_formula"])

    data = dict(DEFAULT_PROFILE)
    data.update({
        "name": name or "learned",
        "books": [os.path.splitext(os.path.basename(epub_path))[0]],
        "latex_style": latex_style,
        "table_latex_style": table_latex_style,
        "infer_allcaps": infer_allcaps,
        "base_style_overrides": dict(sorted(base_style_overrides.items())),
        "run_style_paragraph_styles": dict(sorted(run_style_paragraph_styles.items())),
        "paragraph_styles": dict(sorted(paragraph_styles.items())),
        "headings": dict(sorted(headings.items())),
        "char_styles": dict(sorted(char_styles.items())),
        "char_style_implies": dict(sorted(char_style_implies.items())),
        "message_index_start": reference[0].get("message_index", 10) if reference else 10,
    })

    report = {
        "epub_blocks": len(blocks),
        "reference_items": len(reference),
        "aligned": len(pairs),
        "alignment_pct": round(100.0 * len(pairs) / max(len(reference), 1), 1),
        "paragraph_styles": len(paragraph_styles),
        "headings": len(headings),
        "char_styles": len(char_styles),
        "infer_allcaps": infer_allcaps,
        "allcaps_evidence": "%d/%d uppercase blocks marked allcaps" % (allcaps_hits, allcaps_total),
        "latex_style": "%s (tables: %s)" % (latex_style, table_latex_style),
        "base_style_overrides": base_style_overrides,
        "run_style_paragraph_styles": run_style_paragraph_styles,
        "ambiguous": {
            key: counter.most_common(3)
            for key, counter in para_votes.items()
            if key not in paragraph_styles
        },
    }
    return Profile(data), report
