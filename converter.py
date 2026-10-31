from __future__ import annotations

import argparse
import csv
import glob
import json
import os
import sys
from threading import Thread

from builder import Builder, extract_images
from epubdoc import Epub
from config import DEFAULT_INPUT_DIR, DEFAULT_OUTPUT_DIR, ISBN_FILTER_EXCEL, ISBN_COLUMN
from extract_links import extract_links
from extract_title_page import extract_title_page
from extract_about_author import extract_about_author
from learn import learn_profile
from equation_refiner import refine_equations_via_llm
from summary_generator import add_chunk_summaries
from profile import Profile, find_for_book, list_builtin


class _ErrorCapture(Thread):
    """Thread wrapper that captures exceptions for the caller to inspect."""

    def __init__(self, target, args=(), kwargs=None):
        super().__init__(target=target, args=args, kwargs=kwargs or {})
        self.error = None

    def run(self):
        try:
            super().run()
        except Exception as e:
            self.error = e


_CSV_COLUMNS = [
    "book_name", "total_items", "text_count", "heading_count",
    "table_count", "formula_count", "equations_count", "images_count",
    "has_subscript", "has_superscript", "has_title_page",
    "has_error", "error_message", "empty_summary_count", "time_min",
]


def _csv_log_path(base_out: str) -> str:
    os.makedirs(base_out, exist_ok=True)
    return os.path.join(base_out, "extraction_report.csv")


def _read_csv_log(csv_path: str) -> dict[str, dict]:
    """Read existing CSV log into a dict keyed by book_name."""
    rows = {}
    if not os.path.isfile(csv_path):
        return rows
    with open(csv_path, "r", encoding="utf-8", newline="") as f:
        reader = csv.DictReader(f)
        for row in reader:
            rows[row["book_name"]] = row
    return rows


def _write_csv_log(csv_path: str, rows: dict[str, dict]) -> None:
    """Write full CSV log from dict."""
    sorted_names = sorted(rows.keys())
    try:
        with open(csv_path, "w", encoding="utf-8", newline="") as f:
            writer = csv.DictWriter(f, fieldnames=_CSV_COLUMNS)
            writer.writeheader()
            for name in sorted_names:
                writer.writerow(rows[name])
    except PermissionError:
        print("WARNING: CSV log is locked (close Excel?): %s" % csv_path,
              file=sys.stderr)


def _append_csv_row(csv_path: str, row: dict) -> None:
    """Append a single row to the CSV, creating headers if file is new."""
    try:
        write_header = not os.path.isfile(csv_path) or os.path.getsize(csv_path) == 0
        with open(csv_path, "a", encoding="utf-8", newline="") as f:
            writer = csv.DictWriter(f, fieldnames=_CSV_COLUMNS)
            if write_header:
                writer.writeheader()
            writer.writerow(row)
    except PermissionError:
        print("WARNING: CSV log is locked (close Excel?): %s" % csv_path,
              file=sys.stderr)


def _detect_script_styles(items):
    """Scan items for subscript/superscript in runs and table_runs."""
    has_sub = False
    has_sup = False
    for item in items:
        for run in item.get("runs") or []:
            styles = run.get("char_style") or []
            if "subscript" in styles:
                has_sub = True
            if "superscript" in styles:
                has_sup = True
        for cell in item.get("cells") or []:
            for run in cell.get("table_runs") or []:
                styles = run.get("char_style") or []
                if "subscript" in styles:
                    has_sub = True
                if "superscript" in styles:
                    has_sup = True
        if has_sub and has_sup:
            break
    return has_sub, has_sup


def _count_empty_summaries(items):
    return sum(1 for i in items
               if "chunk_context_summary" in i and not i["chunk_context_summary"])


def _csv_row_from_stats(name, stats, result, has_title, elapsed_sec=0, error=None):
    has_sub, has_sup = _detect_script_styles(result.items) if result else (False, False)
    empty_sum = _count_empty_summaries(result.items) if result else 0
    return {
        "book_name": name,
        "total_items": len(result.items) if result else 0,
        "text_count": stats.get("text", 0),
        "heading_count": stats.get("heading", 0),
        "table_count": stats.get("table", 0),
        "formula_count": stats.get("formula", 0) + stats.get("text with formula", 0),
        "equations_count": len(result.equations) if result else 0,
        "images_count": stats.get("images", 0),
        "has_subscript": "yes" if has_sub else "no",
        "has_superscript": "yes" if has_sup else "no",
        "has_title_page": "yes" if has_title else "no",
        "has_error": "yes" if error else "no",
        "error_message": str(error) if error else "",
        "empty_summary_count": empty_sum,
        "time_min": "%.1f" % (elapsed_sec / 60),
    }


def _csv_row_from_existing(name, book_dir):
    """Build a CSV row by inspecting an already-extracted book folder."""
    text_path = os.path.join(book_dir, "%s_text.json" % name)
    eq_path = os.path.join(book_dir, "%s_equations.json" % name)
    title_path = os.path.join(book_dir, "%s_title.json" % name)
    img_dir = os.path.join(book_dir, "images")

    text_count = heading_count = table_count = formula_count = 0
    total_items = 0
    images_count = 0
    equations_count = 0
    items = []

    has_sub = False
    has_sup = False
    if os.path.isfile(text_path):
        try:
            with open(text_path, "r", encoding="utf-8") as f:
                items = json.load(f)
            total_items = len(items)
            for item in items:
                t = item.get("type", "")
                if t == "text":
                    text_count += 1
                elif t == "heading":
                    heading_count += 1
                elif t == "table":
                    table_count += 1
                elif t in ("formula", "text with formula"):
                    formula_count += 1
            has_sub, has_sup = _detect_script_styles(items)
        except Exception:
            pass

    if os.path.isfile(eq_path):
        try:
            with open(eq_path, "r", encoding="utf-8") as f:
                equations_count = len(json.load(f))
        except Exception:
            pass

    if os.path.isdir(img_dir):
        images_count = len([f for f in os.listdir(img_dir)
                           if os.path.isfile(os.path.join(img_dir, f))])

    has_title = os.path.isfile(title_path)

    return {
        "book_name": name,
        "total_items": total_items,
        "text_count": text_count,
        "heading_count": heading_count,
        "table_count": table_count,
        "formula_count": formula_count,
        "equations_count": equations_count,
        "images_count": images_count,
        "has_subscript": "yes" if has_sub else "no",
        "has_superscript": "yes" if has_sup else "no",
        "has_title_page": "yes" if has_title else "no",
        "has_error": "no",
        "error_message": "",
        "empty_summary_count": _count_empty_summaries(items) if total_items else 0,
        "time_min": "",
    }


def _backfill_csv_log(csv_path: str, base_out: str) -> dict[str, dict]:
    """Load CSV log and backfill rows for previously extracted books."""
    rows = _read_csv_log(csv_path)
    if not os.path.isdir(base_out):
        return rows
    added = 0
    for folder_name in sorted(os.listdir(base_out)):
        folder = os.path.join(base_out, folder_name)
        if not os.path.isdir(folder):
            continue
        text_path = os.path.join(folder, "%s_text.json" % folder_name)
        if not os.path.isfile(text_path):
            continue
        if folder_name in rows:
            continue
        rows[folder_name] = _csv_row_from_existing(folder_name, folder)
        added += 1
    if added:
        _write_csv_log(csv_path, rows)
        print("CSV log: backfilled %d previously extracted books" % added)
    return rows




def _write_json(path: str, payload) -> None:
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    with open(path, "w", encoding="utf-8") as handle:
        json.dump(payload, handle, ensure_ascii=False, indent=1)


def _write_extraction_log(out_dir, name, epub_path, profile, result, stats, log_data):
    """Write a comprehensive extraction log file."""
    os.makedirs(out_dir, exist_ok=True)
    log_path = os.path.join(out_dir, "%s_log.txt" % name)

    items = result.items
    equations = result.equations

    # --- char_style analysis ---
    items_with_runs = 0
    items_without_runs = 0
    no_runs_details = []
    for i, item in enumerate(items):
        if item.get("type") in ("table",):
            continue
        runs = item.get("runs", [])
        if runs:
            items_with_runs += 1
        else:
            items_without_runs += 1
            prompt = item.get("prompt_text", "")
            text_sample = prompt[:80] if prompt else "(empty)"
            item_type = item.get("type", "")
            style = item.get("applied_style", "")

            if not prompt.strip():
                reason = "empty prompt_text (image-only or blank paragraph)"
            elif item_type == "formula":
                reason = "formula item (text replaced by formula_anchor)"
            elif item_type == "heading":
                reason = "heading — no bold/italic/underline styling detected in source"
            else:
                has_alpha = any(ch.isalpha() for ch in prompt)
                if not has_alpha:
                    reason = "no alphabetic characters (numbers/symbols only)"
                else:
                    reason = "plain text — no bold, italic, underline, or allcaps in EPUB source"

            no_runs_details.append({
                "index": i,
                "type": item_type,
                "style": style,
                "reason": reason,
                "text": text_sample,
            })

    # --- table char_style analysis ---
    tables_with_runs = 0
    tables_without_runs = 0
    for item in items:
        if item.get("type") != "table":
            continue
        has_any = any(cell.get("table_runs") for cell in (item.get("cells") or [])
                      if isinstance(cell, dict))
        if has_any:
            tables_with_runs += 1
        else:
            tables_without_runs += 1

    with open(log_path, "w", encoding="utf-8") as f:
        f.write("=" * 90 + "\n")
        f.write("EXTRACTION LOG: %s\n" % name)
        f.write("=" * 90 + "\n")
        f.write("Source  : %s\n" % epub_path)
        f.write("Profile : %s\n" % profile.data.get("name", "default"))
        f.write("\n")

        # --- Item counts ---
        f.write("-" * 90 + "\n")
        f.write("ITEM COUNTS\n")
        f.write("-" * 90 + "\n")
        f.write("  Total items     : %d\n" % len(items))
        f.write("  text            : %d\n" % stats.get("text", 0))
        f.write("  heading         : %d\n" % stats.get("heading", 0))
        f.write("  formula         : %d\n" % stats.get("formula", 0))
        f.write("  text+formula    : %d\n" % stats.get("text with formula", 0))
        f.write("  table           : %d\n" % stats.get("table", 0))
        f.write("  equations       : %d (paragraph=%d, table=%d)\n"
                % (len(equations),
                   len(equations) - stats.get("table_formula", 0),
                   stats.get("table_formula", 0)))
        f.write("  images          : %d\n" % stats.get("images", 0))
        f.write("\n")

        # --- Equation Refinement ---
        eq_log = log_data.get("equations", {})
        f.write("-" * 90 + "\n")
        f.write("EQUATION REFINEMENT (LLM)\n")
        f.write("-" * 90 + "\n")
        if eq_log.get("status"):
            f.write("  Status: %s\n" % eq_log["status"])
            if eq_log.get("error"):
                f.write("  Error : %s\n" % eq_log["error"])
        else:
            f.write("  Total sent      : %d\n" % eq_log.get("total", 0))
            f.write("  Refined (LLM)   : %d\n" % eq_log.get("refined", 0))
            f.write("  Rejected (kept) : %d\n" % eq_log.get("rejected", 0))
            f.write("  Workers         : %d\n" % eq_log.get("workers", 0))
            f.write("  Time            : %.1fs\n" % eq_log.get("elapsed_s", 0))

            eq_failures = eq_log.get("failures", [])
            if eq_failures:
                f.write("\n  Rejected equations:\n")
                f.write("  %-8s %-40s %s\n" % ("Index", "Reason", "LaTeX (first 100 chars)"))
                f.write("  %-8s %-40s %s\n" % ("-" * 8, "-" * 40, "-" * 40))
                for entry in eq_failures[:50]:
                    f.write("  %-8s %-40s %s\n"
                            % (entry["eq_index"], entry["reason"], entry["latex"]))
                if len(eq_failures) > 50:
                    f.write("  ... and %d more\n" % (len(eq_failures) - 50))
        # --- Equation Port Health ---
        eq_ph = log_data.get("eq_port_health")
        if eq_ph:
            f.write("  Ports UP  : %s\n" % ", ".join(str(p) for p in eq_ph["up"]))
            if eq_ph["down"]:
                f.write("  Ports DOWN: %s\n" % ", ".join(
                    "%d (%s)" % (p, r) for p, r in eq_ph["down"]))
        eq_shifts = log_data.get("eq_port_shifts", [])
        if eq_shifts:
            f.write("\n  Port shift events (%d):\n" % len(eq_shifts))
            for ev in eq_shifts[:100]:
                f.write("    %s\n" % ev)
            if len(eq_shifts) > 100:
                f.write("    ... and %d more\n" % (len(eq_shifts) - 100))
        f.write("\n")

        # --- Context Summary ---
        sum_log = log_data.get("summaries", {})
        f.write("-" * 90 + "\n")
        f.write("CHUNK CONTEXT SUMMARY (LLM)\n")
        f.write("-" * 90 + "\n")
        if sum_log.get("status"):
            f.write("  Status: %s\n" % sum_log["status"])
            if sum_log.get("error"):
                f.write("  Error : %s\n" % sum_log["error"])
        else:
            f.write("  Total sent      : %d\n" % sum_log.get("total", 0))
            f.write("  Success         : %d\n" % sum_log.get("success", 0))
            f.write("  Empty (failed)  : %d\n" % sum_log.get("empty", 0))
            f.write("  Workers         : %d\n" % sum_log.get("workers", 0))
            f.write("  Time            : %.1fs\n" % sum_log.get("elapsed_s", 0))

        # --- Summary Port Health ---
        sum_ph = log_data.get("port_health")
        if sum_ph:
            f.write("  Ports UP  : %s\n" % ", ".join(str(p) for p in sum_ph["up"]))
            if sum_ph["down"]:
                f.write("  Ports DOWN: %s\n" % ", ".join(
                    "%d (%s)" % (p, r) for p, r in sum_ph["down"]))
        sum_shifts = log_data.get("port_shifts", [])
        if sum_shifts:
            f.write("\n  Port shift events (%d):\n" % len(sum_shifts))
            for ev in sum_shifts[:200]:
                f.write("    %s\n" % ev)
            if len(sum_shifts) > 200:
                f.write("    ... and %d more\n" % (len(sum_shifts) - 200))

        if not sum_log.get("status"):
            sum_failures = sum_log.get("failures", [])
            if sum_failures:
                reason_counts = {}
                for entry in sum_failures:
                    r = entry["reason"]
                    reason_counts[r] = reason_counts.get(r, 0) + 1
                f.write("\n  Failure reasons:\n")
                f.write("  %-50s %s\n" % ("Reason", "Count"))
                f.write("  %-50s %s\n" % ("-" * 50, "-" * 6))
                for reason, cnt in sorted(reason_counts.items(), key=lambda x: -x[1]):
                    f.write("  %-50s %d\n" % (reason, cnt))

                f.write("\n  Failed items:\n")
                f.write("  %-8s %-12s %-35s %s\n"
                        % ("Index", "Type", "Reason", "Prompt Text"))
                f.write("  %-8s %-12s %-35s %s\n"
                        % ("-" * 8, "-" * 12, "-" * 35, "-" * 40))
                for entry in sorted(sum_failures, key=lambda x: x["item_index"])[:100]:
                    f.write("  %-8s %-12s %-35s %s\n"
                            % (entry["item_index"], entry["type"],
                               entry["reason"], entry["prompt_text"]))
                if len(sum_failures) > 100:
                    f.write("  ... and %d more\n" % (len(sum_failures) - 100))
        f.write("\n")

        # --- char_style analysis ---
        f.write("-" * 90 + "\n")
        f.write("CHAR_STYLE (RUNS) ANALYSIS\n")
        f.write("-" * 90 + "\n")
        f.write("  Paragraphs with runs (styled)    : %d\n" % items_with_runs)
        f.write("  Paragraphs without runs (plain)  : %d\n" % items_without_runs)
        f.write("  Tables with table_runs           : %d\n" % tables_with_runs)
        f.write("  Tables without table_runs        : %d\n" % tables_without_runs)

        if no_runs_details:
            reason_counts = {}
            for d in no_runs_details:
                reason_counts[d["reason"]] = reason_counts.get(d["reason"], 0) + 1
            f.write("\n  Why items have no char_style (runs):\n")
            f.write("  %-65s %s\n" % ("Reason", "Count"))
            f.write("  %-65s %s\n" % ("-" * 65, "-" * 6))
            for reason, cnt in sorted(reason_counts.items(), key=lambda x: -x[1]):
                f.write("  %-65s %d\n" % (reason, cnt))

            f.write("\n  Sample items without runs (first 50):\n")
            f.write("  %-8s %-12s %-55s %s\n"
                    % ("Index", "Type", "Reason", "Text"))
            f.write("  %-8s %-12s %-55s %s\n"
                    % ("-" * 8, "-" * 12, "-" * 55, "-" * 30))
            for d in no_runs_details[:50]:
                f.write("  %-8s %-12s %-55s %s\n"
                        % (d["index"], d["type"], d["reason"], d["text"][:30]))
            if len(no_runs_details) > 50:
                f.write("  ... and %d more\n" % (len(no_runs_details) - 50))

        f.write("\n" + "=" * 90 + "\n")

    print("   log: %s" % log_path)


def _book_name(epub: Epub, path: str, override: str | None) -> str:
    if override:
        return override
    stem = os.path.splitext(os.path.basename(path))[0].strip()
    return stem


# DEFAULT_INPUT_DIR = "input"
# DEFAULT_OUTPUT_DIR = "output"
# DEFAULT_INPUT_DIR = r"\\192.168.4.28\Common\Uday\3698 Files\Output epub"
# DEFAULT_OUTPUT_DIR = r"\\192.168.4.28\Common\Suman Kumar\extracted_epub_josn_files1"


def _download_google_sheet(url: str) -> str:
    """Download a Google Sheet as xlsx to a temp file and return the path."""
    import re as _re
    import tempfile
    import urllib.request as _urlreq
    m = _re.search(r"/d/([a-zA-Z0-9_-]+)", url)
    if not m:
        print("Invalid Google Sheets URL: %s" % url, file=sys.stderr)
        return ""
    sheet_id = m.group(1)
    export_url = "https://docs.google.com/spreadsheets/d/%s/export?format=xlsx" % sheet_id
    tmp = tempfile.NamedTemporaryFile(suffix=".xlsx", delete=False)
    tmp.close()
    try:
        print("Downloading Google Sheet ...")
        _urlreq.urlretrieve(export_url, tmp.name)
        print("Downloaded -> %s" % tmp.name)
        return tmp.name
    except Exception as e:
        print("Failed to download Google Sheet: %s" % e, file=sys.stderr)
        return ""


def _load_isbn_filter(excel_path: str) -> tuple[set[str], set[str]] | None:
    """Read Excel/Google Sheet and return (spanish_isbns, all_eng_isbns).

    spanish_isbns: ENG ISBNs that have a Spanish counterpart (extract first).
    all_eng_isbns: all ENG ISBNs in the sheet.
    """
    if not excel_path:
        return None

    is_url = excel_path.startswith("http")
    local_path = excel_path
    if is_url:
        local_path = _download_google_sheet(excel_path)
        if not local_path:
            # fallback = r"C:\Users\User\Downloads\Spanish Data_2000_ISBN.xlsx"
            fallback = ISBN_FILTER_EXCEL
            if os.path.isfile(fallback):
                print("Using local fallback: %s" % fallback)
                local_path = fallback
            else:
                print("No local fallback found. Make the Google Sheet public or download it manually.",
                      file=sys.stderr)
                return None

    try:
        import openpyxl
    except ImportError:
        print("openpyxl not installed — run: pip install openpyxl", file=sys.stderr)
        return None
    if not os.path.isfile(local_path):
        print("Excel file not found: %s" % local_path, file=sys.stderr)
        return None

    wb = openpyxl.load_workbook(local_path, read_only=True, data_only=True)
    ws = wb.active

    header = [str(c.value or "").strip().lower() for c in next(ws.iter_rows(min_row=1, max_row=1))]
    eng_col = spa_col = None
    for i, h in enumerate(header):
        if h == ISBN_COLUMN or ("eng" in h and "isbn" in h) or h == "eisbn":
            eng_col = i
        if "spanish" in h and "isbn" in h:
            spa_col = i
    if eng_col is None:
        print("Excel column not found — need '%s' or 'ENG ePub ISBN'" % ISBN_COLUMN,
              file=sys.stderr)
        wb.close()
        return None

    all_eng = set()
    with_spanish = set()
    for row in ws.iter_rows(min_row=2, values_only=True):
        eng_val = str(row[eng_col]).strip() if row[eng_col] else ""
        spa_val = str(row[spa_col]).strip() if (spa_col is not None and row[spa_col]) else ""
        if eng_val:
            all_eng.add(eng_val)
            if spa_val and spa_val.lower() != "none":
                with_spanish.add(eng_val)
    wb.close()

    if is_url:
        try:
            os.unlink(local_path)
        except OSError:
            pass

    print("ISBN sheet: %d total, %d with Spanish ISBN (priority)" % (len(all_eng), len(with_spanish)))
    return with_spanish, all_eng


def _expand(patterns) -> list[str]:
    """Resolve files, globs and directories. Directories are searched recursively."""
    paths = []
    for pattern in patterns:
        if os.path.isdir(pattern):
            paths.extend(sorted(glob.glob(os.path.join(pattern, "**", "*.epub"),
                                          recursive=True)))
        elif any(ch in pattern for ch in "*?["):
            paths.extend(sorted(glob.glob(pattern, recursive=True)))
        else:
            paths.append(pattern)
    seen = set()
    unique = []
    for path in paths:
        key = os.path.normcase(os.path.abspath(path))
        if key not in seen:
            seen.add(key)
            unique.append(path)
    return unique


def cmd_convert(args) -> int:
    import time as _time
    _convert_start = _time.time()
    inputs = args.inputs or [DEFAULT_INPUT_DIR]
    paths = _expand(inputs)
    if not paths:
        if not args.inputs and not os.path.isdir(DEFAULT_INPUT_DIR):
            os.makedirs(DEFAULT_INPUT_DIR, exist_ok=True)
            print("Created '%s/'. Put your .epub files there and run this command again."
                  % DEFAULT_INPUT_DIR, file=sys.stderr)
        else:
            print("no EPUB files found in: %s" % ", ".join(inputs), file=sys.stderr)
        return 1

    isbn_result = _load_isbn_filter(getattr(args, "isbn_filter", None))
    if isbn_result is not None:
        spanish_isbns, all_eng_isbns = isbn_result
        all_stems = {os.path.splitext(os.path.basename(p))[0].strip() for p in paths}
        spanish_paths = []
        other_paths = []
        for p in paths:
            stem = os.path.splitext(os.path.basename(p))[0].strip()
            if stem in spanish_isbns:
                spanish_paths.append(p)
            elif stem in all_eng_isbns:
                other_paths.append(p)
        paths = spanish_paths + other_paths
        print("Extraction order: %d Spanish ISBN (priority) + %d other = %d total"
              % (len(spanish_paths), len(other_paths), len(paths)))

        # Log mismatches: EPUBs not in Excel, and Excel ISBNs without EPUB files
        not_in_excel = all_stems - all_eng_isbns
        not_in_input_spanish = spanish_isbns - all_stems
        not_in_input_other = (all_eng_isbns - spanish_isbns) - all_stems
        if not_in_excel:
            print("WARNING: %d EPUB(s) NOT in Excel sheet:" % len(not_in_excel))
            for s in sorted(not_in_excel):
                print("  %s" % s)
        if not_in_input_spanish or not_in_input_other:
            print("WARNING: %d Excel ISBN(s) have NO matching EPUB file (%d Spanish, %d other):"
                  % (len(not_in_input_spanish) + len(not_in_input_other),
                     len(not_in_input_spanish), len(not_in_input_other)))
            for s in sorted(not_in_input_spanish):
                print("  %s (Spanish)" % s)
            for s in sorted(not_in_input_other):
                print("  %s" % s)

    explicit_profile = Profile.load(args.profile) if args.profile else None
    fallback_profile = Profile.load(None)
    failures = 0

    base_out = args.output or DEFAULT_OUTPUT_DIR
    csv_path = _csv_log_path(base_out)
    _backfill_csv_log(csv_path, base_out)
    skipped = 0
    for path in paths:
        if not os.path.isfile(path):
            print("skip (not found): %s" % path, file=sys.stderr)
            failures += 1
            continue
        name_check = os.path.splitext(os.path.basename(path))[0].strip()
        existing_json = os.path.join(base_out, name_check, "%s_text.json" % name_check)
        existing_title = os.path.join(base_out, name_check, "%s_title.json" % name_check)
        existing_links = os.path.join(base_out, name_check, "%s_links.json" % name_check)
        if os.path.isfile(existing_json):
            # --- backfill empty summaries ---
            if not args.no_summary:
                try:
                    with open(existing_json, "r", encoding="utf-8") as f:
                        items = json.load(f)
                    empty_summaries = [
                        i for i in items
                        if "chunk_context_summary" in i
                        and not i["chunk_context_summary"]
                    ]
                    if empty_summaries:
                        print("%s -> backfill %d empty summaries"
                              % (name_check, len(empty_summaries)))
                        try:
                            add_chunk_summaries(items, skip=False, log_data={}, only_empty=True, batch=getattr(args, "batch", False))
                            with open(existing_json, "w", encoding="utf-8") as f:
                                json.dump(items, f, ensure_ascii=False, indent=1)
                            remaining = _count_empty_summaries(items)
                            print("   summaries filled -> %s (remaining empty: %d)"
                                  % (os.path.basename(existing_json), remaining))
                            # update CSV with new empty count
                            csv_rows = _read_csv_log(csv_path)
                            if name_check in csv_rows:
                                csv_rows[name_check]["empty_summary_count"] = remaining
                                _write_csv_log(csv_path, csv_rows)
                        except ConnectionError:
                            print("\n*** LLM server is not reachable. Stopping pipeline. ***")
                            print("*** Start the Llama server and re-run to continue. ***\n")
                            failures += 1
                            break
                except Exception as e:
                    print("%s -> summary backfill failed: %s" % (name_check, e))
            # --- backfill missing title ---
            if not os.path.isfile(existing_title):
                try:
                    epub_t = Epub(path)
                    title_items, title_source = extract_title_page(epub_t)
                    if title_items:
                        _write_json(existing_title,
                                    {"metadata": title_source, "items": title_items})
                        print("%s -> title_page=%d elements (%s) (backfill)"
                              % (name_check, len(title_items), title_source))
                except Exception as e:
                    print("%s -> title backfill failed: %s" % (name_check, e))
            # --- backfill missing links ---
            if not os.path.isfile(existing_links):
                try:
                    link_items = extract_links(path, existing_json)
                    if link_items:
                        _write_json(existing_links, link_items)
                        print("%s -> links=%d (backfill)"
                              % (name_check, len(link_items)))
                except Exception as e:
                    print("%s -> links backfill failed: %s" % (name_check, e))
            # --- backfill missing about_author ---
            existing_about = os.path.join(base_out, name_check, "%s_about_author.json" % name_check)
            if not os.path.isfile(existing_about):
                try:
                    epub_a = Epub(path)
                    about_items = extract_about_author(epub_a, find_for_book(name_check))
                    if about_items:
                        _write_json(existing_about, about_items)
                        print("%s -> about_author=%d elements (backfill)"
                              % (name_check, len(about_items)))
                except Exception as e:
                    print("%s -> about_author backfill failed: %s" % (name_check, e))
            skipped += 1
            continue
        profile = explicit_profile or find_for_book(path) or fallback_profile
        infer_allcaps = True if (args.infer_allcaps or args.faithful) else None
        _book_start = _time.time()
        try:
            epub = Epub(path)
            name = _book_name(epub, path, args.name if len(paths) == 1 else None)
            builder = Builder(
                epub, profile, name,
                table_formula_prompt=args.table_formula_prompt,
                max_formula_words=args.max_formula_words,
                detect_formulas=not args.no_formulas,
                skip_front_matter=not args.include_front_matter,
                infer_allcaps=infer_allcaps,
            )
            result = builder.build()
        except Exception as exc:  # noqa: BLE001 - report and continue with the batch
            print("FAILED %s: %s" % (path, exc), file=sys.stderr)
            _append_csv_row(csv_path, {
                "book_name": name_check,
                "total_items": 0, "text_count": 0, "heading_count": 0,
                "table_count": 0, "formula_count": 0, "equations_count": 0,
                "images_count": 0, "has_subscript": "no", "has_superscript": "no",
                "has_title_page": "no",
                "has_error": "yes", "error_message": str(exc),
                "time_min": "0.0",
            })
            failures += 1
            continue

        out_dir = os.path.join(base_out, name)

        log_data = {}
        llm_threads = []
        if result.equations:
            t = _ErrorCapture(target=refine_equations_via_llm,
                              args=(result.equations,),
                              kwargs={"skip": args.no_summary, "log_data": log_data, "batch": getattr(args, "batch", False)})
            t.start()
            llm_threads.append(t)

        if profile.emit_summary:
            t = _ErrorCapture(target=add_chunk_summaries,
                              args=(result.items,),
                              kwargs={"skip": args.no_summary, "log_data": log_data, "batch": getattr(args, "batch", False)})
            t.start()
            llm_threads.append(t)

        llm_server_down = False
        for t in llm_threads:
            t.join()
            if isinstance(t.error, ConnectionError):
                llm_server_down = True

        if llm_server_down and not args.no_summary:
            print("\n*** LLM server is not reachable. Stopping pipeline. ***")
            print("*** Start the Llama server and re-run to continue. ***\n")
            _book_elapsed = _time.time() - _book_start
            _append_csv_row(csv_path, _csv_row_from_stats(
                name, stats, result, False, elapsed_sec=_book_elapsed,
                error="LLM server unreachable - pipeline stopped"))
            failures += 1
            break

        content_path = os.path.join(out_dir, "%s_text.json" % name)
        _write_json(content_path, result.items)

        written = [os.path.basename(content_path)]
        if result.equations:
            equations_path = os.path.join(out_dir, "%s_equations.json" % name)
            _write_json(equations_path, result.equations)
            written.append(os.path.basename(equations_path))
        elif args.always_equations:
            equations_path = os.path.join(out_dir, "%s_equations.json" % name)
            _write_json(equations_path, [])
            written.append(os.path.basename(equations_path))

        if args.images and result.images:
            image_dir = os.path.join(out_dir, "images")
            count = extract_images(epub, result.images, image_dir)
            written.append("%s (%d files)"
                           % (os.path.relpath(image_dir, out_dir).replace("\\", "/"), count))

        stats = result.stats
        print("%s [profile: %s] -> %s"
              % (os.path.basename(path), profile.data.get("name", "default"), ", ".join(written)))
        print("   items=%d  text=%d heading=%d formula=%d text+formula=%d table=%d"
              % (len(result.items), stats.get("text", 0), stats.get("heading", 0),
                 stats.get("formula", 0), stats.get("text with formula", 0),
                 stats.get("table", 0)))
        print("   equations=%d (paragraph=%d, table=%d)  images=%d"
              % (len(result.equations),
                 len(result.equations) - stats.get("table_formula", 0),
                 stats.get("table_formula", 0), stats.get("images", 0)))

        _write_extraction_log(out_dir, name, path, profile, result, stats, log_data)

        has_title = False
        try:
            title_items, title_source = extract_title_page(epub)
            if title_items:
                title_path = os.path.join(out_dir, "%s_title.json" % name)
                _write_json(title_path, {"metadata": title_source, "items": title_items})
                print("   title_page=%d elements (%s) -> %s"
                      % (len(title_items), title_source, os.path.basename(title_path)))
                has_title = True
        except Exception as e:
            print("   title_page extraction failed: %s" % e)


        try:
            about_items = extract_about_author(epub, profile)
            if about_items:
                about_path = os.path.join(out_dir, "%s_about_author.json" % name)
                _write_json(about_path, about_items)
                print("   about_author=%d elements -> %s" % (len(about_items), os.path.basename(about_path)))
        except Exception as e:
            print("   about_author extraction failed: %s" % e)
        try:
            text_json_path = os.path.join(out_dir, "%s_text.json" % name)
            link_items = extract_links(path, text_json_path)
            if link_items:
                links_path = os.path.join(out_dir, "%s_links.json" % name)
                _write_json(links_path, link_items)
                print("   links=%d -> %s" % (len(link_items), os.path.basename(links_path)))
        except Exception as e:
            print("   link extraction failed: %s" % e)

        _book_elapsed = _time.time() - _book_start
        print("   time: %.1f min" % (_book_elapsed / 60))
        _append_csv_row(csv_path, _csv_row_from_stats(
            name, stats, result, has_title, elapsed_sec=_book_elapsed))

    # --- Image folder report ---
    _write_image_folder_report(base_out)

    total_minutes = (_time.time() - _convert_start) / 60
    if skipped:
        print("Skipped %d already-extracted books" % skipped)
    print("Total time: %.1f minutes" % total_minutes)
    print("CSV report: %s" % csv_path)
    return 1 if failures else 0


def _write_image_folder_report(output_dir: str) -> None:
    if not os.path.isdir(output_dir):
        return
    with_images = []
    without_images = []
    for name in sorted(os.listdir(output_dir)):
        folder = os.path.join(output_dir, name)
        if not os.path.isdir(folder):
            continue
        img_dir = os.path.join(folder, "images")
        if os.path.isdir(img_dir):
            count = len([f for f in os.listdir(img_dir) if os.path.isfile(os.path.join(img_dir, f))])
            with_images.append((name, count))
        else:
            without_images.append(name)
    lines = []
    lines.append("Books WITH images folder: %d" % len(with_images))
    lines.append("-" * 50)
    for isbn, count in with_images:
        lines.append("  %s  (%d files)" % (isbn, count))
    lines.append("\nBooks WITHOUT images folder: %d" % len(without_images))
    lines.append("-" * 50)
    for isbn in without_images:
        lines.append("  %s" % isbn)
    lines.append("\nTotal: %d books" % (len(with_images) + len(without_images)))
    report = "\n".join(lines)
    log_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "image_folders_report.log")
    with open(log_path, "w", encoding="utf-8") as f:
        f.write(report + "\n")
    print("\nImage report: %d with images, %d without -> %s"
          % (len(with_images), len(without_images), log_path))


def cmd_learn(args) -> int:
    profile, report = learn_profile(
        args.epub, args.reference, args.equations, name=args.name,
        min_support=args.min_support, min_confidence=args.min_confidence,
        implied_threshold=args.implied_threshold,
    )
    profile.save(args.output)
    print("aligned %d/%d reference items (%.1f%%)"
          % (report["aligned"], report["reference_items"], report["alignment_pct"]))
    print("learned %d paragraph styles, %d heading rules, %d character styles"
          % (report["paragraph_styles"], report["headings"], report["char_styles"]))
    print("infer_allcaps=%s (%s)" % (report["infer_allcaps"], report["allcaps_evidence"]))
    print("latex_style=%s" % report["latex_style"])
    if report["base_style_overrides"]:
        print("paragraph baselines: %s" % report["base_style_overrides"])
    if report["run_style_paragraph_styles"]:
        print("run-style paragraph styles: %s" % report["run_style_paragraph_styles"])
    if report["ambiguous"]:
        print("ambiguous keys (left to the default template):")
        for key, votes in sorted(report["ambiguous"].items()):
            print("   %-22s %s" % (key, votes))
    print("profile written to %s" % args.output)
    return 0


def cmd_profiles(args) -> int:
    for name in list_builtin():
        print(name)
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="epub2json",
        description="Convert EPUBs into structured content JSON plus a LaTeX equations JSON.",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    convert = sub.add_parser("convert", help="convert one or more EPUBs")
    convert.add_argument("inputs", nargs="*",
                         help="EPUB files, globs, or directories (default: %s/, searched "
                              "recursively)" % DEFAULT_INPUT_DIR)
    convert.add_argument("-o", "--output",
                         help="output directory (default: %s/)" % DEFAULT_OUTPUT_DIR)
    convert.add_argument("--per-book-folder", action="store_true",
                         help="write each book into its own subfolder of the output directory")
    convert.add_argument("-p", "--profile", help="style profile name or path")
    convert.add_argument("-n", "--name", help="book name used for output filenames")
    convert.add_argument("--images", action="store_true", help="also extract images")
    convert.add_argument("--no-formulas", action="store_true", help="disable formula detection")
    convert.add_argument("--max-formula-words", type=int, default=15,
                         help="longest line still treated as a standalone formula")
    convert.add_argument("--table-formula-prompt", choices=["latex", "raw"], default="latex",
                         help="what to put in prompt_text for table formulas")
    convert.add_argument("--always-equations", action="store_true",
                         help="write an equations file even when the book has none")
    convert.add_argument("--include-front-matter", action="store_true",
                         help="include cover, title, copyright, contents, and about-author pages")
    convert.add_argument("--infer-allcaps", action="store_true",
                         help="mark uniformly uppercase text with an allcaps run (EPUB often "
                              "stores this as capital letters only)")
    convert.add_argument("--faithful", action="store_true",
                         help="same as --infer-allcaps: opt in to allcaps on uppercase text")
    convert.add_argument("--no-summary", action="store_true",
                         help="skip LLM chunk_context_summary generation")
    convert.add_argument("--batch", action="store_true",
                         help="batch multiple small chunks into single LLM calls for speed")
    convert.add_argument("--isbn-filter",
                         help="Excel file with ENG/Spanish ISBN columns — extract only "
                              "English books that have a Spanish counterpart")
    convert.set_defaults(func=cmd_convert)

    learn = sub.add_parser("learn", help="learn a style profile from a reference JSON")
    learn.add_argument("epub", help="EPUB file")
    learn.add_argument("reference", help="reference content JSON for the same book")
    learn.add_argument("-e", "--equations", help="reference equations JSON (improves alignment)")
    learn.add_argument("-o", "--output", required=True, help="where to write the profile")
    learn.add_argument("-n", "--name", help="profile name")
    learn.add_argument("--min-support", type=int, default=2)
    learn.add_argument("--min-confidence", type=float, default=0.55)
    learn.add_argument("--implied-threshold", type=float, default=0.9,
                       help="how consistently a character style must carry a flag to imply it")
    learn.set_defaults(func=cmd_learn)

    profiles = sub.add_parser("profiles", help="list built-in profiles")
    profiles.set_defaults(func=cmd_profiles)

    return parser


def main(argv=None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    return args.func(args)
