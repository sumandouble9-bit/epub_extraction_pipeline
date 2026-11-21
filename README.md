# 📚 EPUB Extraction Pipeline

Convert EPUB e-books into clean, structured **JSON** that downstream tools (IDML / PDF typesetting) can consume. The pipeline reads an EPUB's spine in reading order, resolves its CSS, and emits ordered blocks of text, tables, images, links, formulas, and front/back-matter — each with styling, paragraph roles, and stable hash anchors.

---

## ✨ What it produces

For every book (named by its ISBN), the pipeline writes a folder containing:

| 📄 File | What's inside |
|---|---|
| `<isbn>_text.json` | 🧱 Main body: ordered paragraphs, headings, tables, inline images, formula anchors |
| `<isbn>_title.json` | 🏷️ Title page (title / author / etc.) + `metadata` naming the source document |
| `<isbn>_about_author.json` | ✍️ "About the Author" page |
| `<isbn>_links.json` | 🔗 Hyperlinks (internal + external) with resolved targets |
| `<isbn>_log.txt` | 🪵 Per-book extraction log (counts, port health, timings) |
| `images/<isbn>/…` | 🖼️ Extracted image files (with `--images`) |
| `extraction_report.csv` | 📊 One row per book: tables, formulas, images, errors, time |

---

## 🧩 Module guide — what each file does & how it works

### 🚀 Entry point & orchestration
- **`epub_extraction_pipeline.py`** — The one-command launcher. It simply calls `converter.main(["convert", "--images", …])`, so running it converts EPUBs **and** extracts images in one go.
- **`converter.py`** — The CLI and orchestrator. Defines the `convert`, `learn`, and `profiles` sub-commands; expands inputs (files / globs / folders), loads profiles, and loops over every book. Per book it: runs the extractor → builds JSON → launches the LLM threads (equation + summary) → writes `text/title/about_author/links` JSON, the log, and images → appends a CSV row. Also handles **backfilling** missing outputs for already-processed books and optional **ISBN filtering** from an Excel sheet.

### 📖 EPUB reading & styling
- **`epubdoc.py`** — The `Epub` class. Opens the `.epub` zip, finds and parses the **OPF** (metadata, manifest, spine), parses each XHTML document with `lxml`, resolves relative paths, and gathers the stylesheets that apply to a document.
- **`css_resolver.py`** — A lightweight CSS engine. Parses stylesheets into selectors, matches them against an element's ancestor path, applies the cascade, and tracks **inherited** properties — so the parser knows the computed `font-weight`, `font-style`, `text-align`, etc. for any element.

### 🏗️ Core extraction
- **`epub_parser.py`** — The heart of the pipeline. Walks each XHTML `<body>` into an ordered stream of **`Block`s** (`para` / `table` / `image`), each with styled **`Run`s** (bold, italic, super/subscript, small-caps…). Key logic:
  - 🧠 **Front-matter classification** (`_classify_doc`) — labels each spine document as `front` (cover / title / copyright / TOC / dedication), `about_author`, or `body`, using `epub:type`, filenames, CSS classes, and text signals. `trim_to_body_start` then drops front/back-matter from the body so only chapters land in `text.json`.
  - 👤 **About-author detection** (`is_about_author_doc`) — filename-first (`bio.*`), then an exact heading match — shared with the about-author extractor so they never disagree.
  - ↩️ **Line breaks** — a unique `<br/>` sentinel so real breaks become single `\n`, while pretty-printed whitespace does not.
  - 🖼️ **Inline image offsets** — records where an image sits *inside* a paragraph so the builder can anchor it.
- **`builder.py`** — Turns `Block`s into the final JSON items + equations. Applies the **profile** styles, detects formulas, measures & lays out images, and assigns **stable hash IDs**. Inserts `[formula_anchor_…]` and `[image_anchor_…]` tokens into `prompt_text` so equations and in-paragraph images can be placed back precisely. `extract_images` writes the actual image files.
- **`formulas.py`** — Formula detection and classification (`text`, `formula`, `text_with_formula`), run-to-**LaTeX** conversion, prompt normalization, and the `anchor()` / `image_anchor()` token helpers.

### 🎨 Styling profiles
- **`profile.py`** — The `Profile` class maps CSS classes / styles to **named paragraph & character styles**, detects heading levels, and defines the image-path template. Profiles live in `profiles/*.json` (`default`, `double9`, `prairie`); `find_for_book` picks one per book.
- **`learn.py`** — Learns a new style profile by aligning an EPUB against a reference JSON for the same book (used by `convert learn`).

### 🧾 Side extractors
- **`extract_title_page.py`** — Finds the title page (filename hint first, then title/author classes), extracts it to `*_title.json` with a `metadata` field naming the source file.
- **`extract_about_author.py`** — Extracts the about-author page to `*_about_author.json`, using the shared `is_about_author_doc` detector.
- **`extract_links.py`** — Collects hyperlinks (including Calibre `data-*` rewritten targets) and resolves them to full document paths → `*_links.json`.

### 🤖 LLM refinement (optional, uses a llama-server)
- **`equation_refiner.py`** — Sends extracted equations to the **equation** LLM server for clean-up (dash normalization, prefix stripping). Falls back to the heuristic result if the LLM returns garbage.
- **`summary_generator.py`** — Generates per-chunk context summaries via the **text** LLM server. ⚠️ Currently **disabled** (`USE_TEXT_SUMMARY_LLM = False` in `converter.py`, and `PORTS = []`), so only the equation server is used.

### 🔧 Utilities
- **`fix_prompt_text_markers.py`** — Normalizes `_()` / `^()` subscript/superscript markers in `prompt_text`.
- **`find_image_folders.py`** — Reports which output books have an `images/` folder → `image_folders_report.log`.
- **`config.py`** — Loads `.env` and exposes `DEFAULT_INPUT_DIR`, `DEFAULT_OUTPUT_DIR`, and the ISBN-filter settings.

---

## 📦 Installation

Requires **Python 3.10+**.

```bash
python -m venv venv
```
```bash
venv\Scripts\activate
```
```bash
pip install -r requirements.txt
```

Dependencies (`requirements.txt`):
- 🧬 **lxml** ≥ 5.0 — XHTML / OPF parsing
- 🖼️ **Pillow** ≥ 10.0 — image measurement (width/height/DPI)

---

## ⚙️ Configuration

Copy the example env file and set your paths:

```bash
copy .env.example .env
```

`.env` keys:
- `EPUB_INPUT_DIR` — folder scanned for `.epub` files when no input is given
- `EPUB_OUTPUT_DIR` — where per-book JSON folders are written
- `ISBN_FILTER_EXCEL` / `ISBN_COLUMN` — optional Excel sheet to filter/prioritize ISBNs

---

## ▶️ Running the pipeline

### Full run (all EPUBs in the input folder, with images)
```bash
python epub_extraction_pipeline.py
```

### One or more specific books
```bash
python epub_extraction_pipeline.py "D:\path\to\9789358715057.epub"
```

### A whole folder (searched recursively)
```bash
python epub_extraction_pipeline.py "D:\path\to\epub_folder"
```

> 💡 `epub_extraction_pipeline.py` is a shortcut for `convert --images`. Pass **EPUB file paths, globs, or directories** (a bare ISBN with no `.epub` is treated as a literal path). With no arguments it scans `EPUB_INPUT_DIR`.

### Using the converter directly (more options)
```bash
python converter.py convert "book.epub" -o "D:\output" --images
```

Useful `convert` flags:
- `--images` 🖼️ also extract image files
- `-o, --output` 📁 output directory (defaults to `EPUB_OUTPUT_DIR`)
- `-p, --profile` 🎨 style profile name or path
- `--no-formulas` ➗ disable formula detection
- `--max-formula-words N` 🔢 cap formula length (default 15)
- `--no-summary` 🚫 skip the LLM summary stage
- `--include-front-matter` 📑 keep cover/title/copyright/TOC in the body
- `--infer-allcaps` / `--faithful` 🔠 all-caps handling
- `--isbn-filter PATH` 🗂️ prioritize/filter by an Excel ISBN sheet

### Other sub-commands
```bash
python converter.py profiles
```
```bash
python converter.py learn "book.epub" "reference.json" -o "profiles/mybook.json" -n mybook
```

---

## 🔄 Pipeline flow at a glance

```
.epub ─▶ epubdoc (OPF + spine)
      ─▶ css_resolver (computed styles)
      ─▶ epub_parser (blocks + runs, front-matter trim)
      ─▶ builder (+ profile, formulas, images, hash anchors)
      ─▶ equation_refiner (LLM, optional)
      ─▶ JSON: text / title / about_author / links  +  images  +  log  +  CSV
```

---

## 🧠 Good to know
- 🧱 **Only chapters** reach `text.json` — title, copyright, TOC, and about-author pages are routed to their own files or dropped.
- 🖼️ **In-paragraph images** carry a `hash_id` and a matching `[image_anchor_<hash>]` token in `prompt_text`, so they can be re-placed at the exact spot. Standalone images are left untouched.
- 🤖 The **text/summary LLM is off** by default; only the **equation** LLM server is contacted. Flip `USE_TEXT_SUMMARY_LLM` in `converter.py` to re-enable it.
