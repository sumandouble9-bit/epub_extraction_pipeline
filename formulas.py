"""Classify blocks as prose / formula / prose-with-formula and extract equations.

Superscript and subscript information comes from the EPUB's own ``<sup>``/``<sub>``
markup, which is what makes ``e(0.05*1)`` render as ``e^{(0.05*1)}``.
"""
from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass

TEXT = "text"
FORMULA = "formula"
TEXT_WITH_FORMULA = "text with formula"

# Characters that are mathematical wherever they appear.
STRONG_MATH = set("=≈≠≤≥×÷±√∑∫∏≡∝")
# Characters that are mathematical only between two operands.
WEAK_OPERATORS = set("+-*/^")

_NUMBER = re.compile(r"^[(\[{]*[\d][\d.,]*[%°]?[)\]}]*[.,;:]?$")
_SINGLE_LETTER = re.compile(r"^[(\[{]*[A-Za-z][)\]}]*[.,;:]?$")
_HAS_DIGIT = re.compile(r"\d")
_CURRENCY = re.compile(r"^(rs\.?|inr|usd|\$|₹|£|€|us)$", re.I)
# Multiplication written between two numbers is unambiguous; a dash or slash
# between numbers is usually a range or a date, so it stays weak evidence.
_TIMES_BETWEEN_NUMBERS = re.compile(r"(?<=\d)\s*[xX*]\s*(?=\d)")
_ORDINAL = re.compile(r"^(st|nd|rd|th)$", re.I)
# A superscript holding a bare or square-bracketed integer is a footnote
# reference. Parenthesised superscripts really are exponents in these books.
_FOOTNOTE_MARK = re.compile(r"^\[?\d+\]?$")
# A token made only of operator glyphs: the "+" in "indicator= +".
_OPERATOR_ONLY = re.compile(r"^[+\-*/^<>=]+$")
# Brackets and digits are deliberately excluded: "( 0.045*3)" is a real
# exponent group, whereas a lone "=" inside <sub> is a typographic quirk.
_OPERATOR_GLYPHS = STRONG_MATH | set("+-*/^<>")
# Table cells are terser than prose, so a leading operator alone marks an
# expression: "= 6.5:1", "+100". A leading minus is excluded because it is far
# more often a negative figure in a returns column than an operation.
_LEADS_WITH_OPERATOR = re.compile(r"^[=+*/×÷]")
# ... but a bare span of years or page numbers is a range, not arithmetic.
_PURE_RANGE = re.compile(r"^\(?\d[\d,.]*\s*-\s*\d[\d,.]*\)?$")
# Fill-in-the-blank rules in quiz tables are punctuation, not operators.
_FILL_BLANK = re.compile(r"[-_]{3,}")
# Digits, fraction slashes and hyphens with no relation sign are a measurement
# such as "9-1/2" miles, not a calculation.
_MEASUREMENT = re.compile(r"[\d.,\s/-]+")
# "3-1/2 miles" is one quantity, so its dash and slash are not two operations.
_MIXED_FRACTION = re.compile(r"\d+\s*-\s*\d+\s*/\s*\d+")

SYMBOL_LATEX = {
    "×": r"\times", "÷": r"\div", "≈": r"\approx", "≠": r"\neq",
    "≤": r"\leq", "≥": r"\geq", "±": r"\pm", "∞": r"\infty",
    "∑": r"\sum", "∏": r"\prod", "∫": r"\int", "√": r"\sqrt",
    "∂": r"\partial", "∆": r"\Delta", "Δ": r"\Delta", "Σ": r"\Sigma",
    "π": r"\pi", "α": r"\alpha", "β": r"\beta", "γ": r"\gamma",
    "θ": r"\theta", "λ": r"\lambda", "μ": r"\mu", "σ": r"\sigma",
    "ρ": r"\rho", "ω": r"\omega", "Ω": r"\Omega", "φ": r"\phi",
    "·": r"\cdot", "→": r"\to", "⇒": r"\Rightarrow", "∈": r"\in",
    "°": r"^\circ", "∴": r"\therefore", "∵": r"\because",
    "≡": r"\equiv", "∝": r"\propto", "∅": r"\emptyset",
}

DASHES = "–—‒―‐−"
DASH_TABLE = {ord(ch): "-" for ch in DASHES}

# Applied to prompt_text so it matches the reference pipeline's plain-text form.
PROMPT_MAP = {"×": "x"}


@dataclass
class Classification:
    kind: str
    start: int = 0          # character span of the formula inside the block text
    end: int = 0
    answer_stripped: bool = False


def normalize_prompt(text: str) -> str:
    for src, dst in PROMPT_MAP.items():
        text = text.replace(src, dst)
    return text


def _tokens(text: str) -> list[tuple[int, int, str]]:
    return [(m.start(), m.end(), m.group(0)) for m in re.finditer(r"\S+", text)]


def _is_operand_char(text: str, i: int, direction: int) -> bool:
    """True when text[i] can act as an operand next to an operator."""
    ch = text[i]
    if ch.isdigit():
        return True
    if ch in (")]}" if direction < 0 else "([{"):
        return True
    if ch.isalpha():
        neighbour = i - 1 if direction < 0 else i + 1
        if 0 <= neighbour < len(text) and text[neighbour].isalnum():
            return False
        return True
    return False


def _core_positions(text: str) -> set[int]:
    """Character positions that make the surrounding token mathematical."""
    positions: set[int] = set()
    length = len(text)
    for i, ch in enumerate(text):
        if ch in STRONG_MATH:
            positions.add(i)
            continue
        is_weak = ch in WEAK_OPERATORS
        is_times = ch in "xX" and (
            (i == 0 or not text[i - 1].isalnum())
            and (i + 1 >= length or not text[i + 1].isalnum())
        )
        if not (is_weak or is_times):
            continue
        left = i - 1
        while left >= 0 and text[left] == " ":
            left -= 1
        right = i + 1
        while right < length and text[right] == " ":
            right += 1
        if left < 0 or right >= length:
            continue
        if _is_operand_char(text, left, -1) and _is_operand_char(text, right, 1):
            positions.add(i)
    return positions


_RATIO_SLASH = re.compile(r"[A-Za-z]{2,}\s+[A-Za-z]{2,}\s*/\s*[A-Za-z]{2,}"
                          r"|[A-Za-z]{2,}\s*/\s*[A-Za-z]{2,}\s+[A-Za-z]{2,}")

def _is_table_cell_math(probe: str) -> bool:
    """Relaxed test for table cells, which rarely spell out a whole equation."""
    body = _MIXED_FRACTION.sub("0", _FILL_BLANK.sub(" ", probe)).strip()
    if not body or not any(ch.isalnum() for ch in body) or _PURE_RANGE.match(body):
        return False
    if _LEADS_WITH_OPERATOR.match(body):
        return True
    if _RATIO_SLASH.search(body) and "=" in body:
        return True
    if _MEASUREMENT.fullmatch(body):
        return False
    return len(_core_positions(body)) >= 2


def _absorb_trailing_operand(tokens, right: int) -> int:
    if right + 1 >= len(tokens):
        return right
    trailing = tokens[right][2].rstrip(".,;:")
    if trailing and trailing[-1] in STRONG_MATH and _OPERATOR_ONLY.match(tokens[right + 1][2]):
        return right + 1
    return right


def _is_math_adjacent(token: str) -> bool:
    if _NUMBER.match(token) or _SINGLE_LETTER.match(token):
        return True
    if _CURRENCY.match(token.strip("()[]{},;:")):
        return True
    return bool(_HAS_DIGIT.search(token)) and len(token) <= 12


def _is_exponent(run) -> bool:
    if "superscript" not in run.styles and "subscript" not in run.styles:
        return False
    body = run.text.strip()
    if len(body) <= 1 or _ORDINAL.match(body) or _FOOTNOTE_MARK.match(body):
        return False
    return True


def _superscript_core(tokens, runs) -> list[bool]:
    flags = [False] * len(tokens)
    for run in runs or []:
        if not _is_exponent(run):
            continue
        for i, (start, end, _) in enumerate(tokens):
            if start < run.end and end > run.start:
                flags[i] = True
    return flags


# ---------------------------------------------------------------- stop words
# Words that signal prose boundaries when extracting equations from paragraphs.
_STOP_WORDS = frozenset({
    "is", "are", "was", "were", "has", "have", "had", "be", "been", "being",
    "does", "did", "do", "will", "would", "can", "could", "should", "shall",
    "may", "might", "must", "the", "a", "an", "that", "this", "these", "those",
    "it", "its", "which", "where", "when", "if", "then", "so", "but", "or",
    "yet", "while", "since", "because", "although", "however", "therefore",
    "hence", "thus", "moreover", "furthermore", "of", "by", "from", "into",
    "through", "about", "between", "after", "before", "during", "within",
    "without", "upon", "along", "across", "not", "no", "also", "only", "just",
    "even", "both", "either", "neither", "made", "given", "called", "known",
    "say", "said", "shown", "obtained", "calculated", "determined", "expressed",
    "represented", "written", "defined", "denoted", "assumed", "referred",
    "considered", "used", "applied", "derived", "stated", "described",
    "explained", "discussed", "mentioned", "occurs", "takes", "means",
    "refers", "implies", "suggests", "indicates", "shows", "gives",
    "to", "in", "for", "on", "at", "with", "as", "per", "via", "like",
    "each", "every", "any", "all", "some", "most", "many", "such",
    "than", "what", "how", "why", "who", "whom", "whose",
    "here", "there", "now", "very", "more", "less", "much",
    "answer", "following", "questions", "words", "log", "having",
    "suppose", "generally", "quoted", "delivery", "party",
    "settlement", "forward", "example", "respectively", "applies",
    "expansion", "occurs", "level", "rises", "appears", "displayed",
    "traded", "higher", "lower", "previous", "change",
    "we", "know", "point", "let", "recall", "note", "observe",
    "and", "but", "nor", "so", "i.e", "e.g", "ie", "eg",
    "equal", "equals",
})

_MATH_OPERATORS = frozenset("+-*/=×÷–—±≈≠≤≥−ΔΣ∆→←⇒")

# Greek math symbols recognized as math tokens in equation extraction
_GREEK_MATH_CHARS = set("ΔΣΘΞΨΩαβγδεθλμπσφψω∆")

# Stop words that ALWAYS break equation span boundaries — never bridge across these
_EQUATION_BREAKERS = frozenset({
    "then", "if", "since", "because", "when", "where",
    "however", "therefore", "hence", "thus",
    "means", "that", "equal",
})

_UNIT_WORDS = frozenset({
    "lakh", "lakhs", "crore", "crores", "mm", "mn", "bn", "billion", "million",
    "thousand", "hundred", "marks", "units", "kg", "km", "mt", "tons", "tonnes",
    "percent", "rupees", "dollars", "euros", "pounds", "years", "months",
    "days", "hours", "minutes", "seconds", "pieces", "items", "nos",
})


def _is_eq_math_token(tok: str) -> bool:
    """Is this token clearly mathematical (number, operator, currency, abbreviation)?"""
    t = tok.rstrip(".,;:")
    if not t:
        return False
    if "=" in t:
        return True
    if _HAS_DIGIT.search(t):
        return True
    if len(t) == 1 and t in _MATH_OPERATORS:
        return True
    if _CURRENCY.match(t.strip("()[]{}")):
        return True
    if re.match(r"^\[.*\]$", t) and _HAS_DIGIT.search(t):
        return True
    # Token starts with a math operator glued to a word: "+Rent", "-Employed"
    if t[0] in _MATH_OPERATORS and len(t) > 1:
        return True
    # Greek math symbols (Δ, Σ, α, etc.)
    if any(c in _GREEK_MATH_CHARS for c in t):
        return True
    # Function-call patterns: f(x), sin(30), log(n) — letter followed by parens
    if t[0].isalpha() and '(' in t:
        return True
    # Embedded operators: "Size*base", "price*", "A+B"
    if any(c in t for c in "*/+"):
        return True
    return False


def _is_eq_stop(tok: str) -> bool:
    """Is this token a prose boundary word?"""
    return tok.strip(".,;:……()[]{}").lower() in _STOP_WORDS



# -------------------------------------------------------- equation extraction

def extract_equation_text(text: str) -> str | None:
    """Extract the equation substring from a text that contains an embedded formula.

    Returns the equation text as-is (not converted to LaTeX notation).
    Returns None if no equation is found.
    """
    if "=" not in text:
        return None

    # Normalize dashes so Unicode minus (−) is recognized as operator
    text_norm = text.translate(DASH_TABLE)
    tokens = _tokens(text_norm)
    if not tokens:
        return None

    eq_indices = [i for i, (_, _, t) in enumerate(tokens) if "=" in t]
    if not eq_indices:
        return None

    first_eq = eq_indices[0]
    # Find last = that is not separated from first by a breaker word
    last_eq = first_eq
    for ei in range(1, len(eq_indices)):
        prev_eq = eq_indices[ei - 1]
        cur_eq = eq_indices[ei]
        has_breaker = False
        for bi in range(prev_eq + 1, cur_eq):
            bare = tokens[bi][2].strip(".,;:()[]{}").lower()
            if bare in _EQUATION_BREAKERS:
                has_breaker = True
                break
        if has_breaker:
            break
        last_eq = cur_eq

    # --- LEFT boundary: walk left from first =, stop at stop-words ---
    left = first_eq
    for i in range(first_eq - 1, -1, -1):
        tok = tokens[i][2]
        if tok.endswith(":"):
            break
        # Comma-ending non-math token is a clause boundary ("economy,", "curve CC,")
        if tok.endswith(",") and not _is_eq_math_token(tok):
            break
        if _is_eq_stop(tok):
            # Hard breakers always end the equation span
            if tok.strip(".,;:……()[]{}").lower() in _EQUATION_BREAKERS:
                break
            # Look further left past stop words for operator-bearing tokens
            found_op_left = False
            for j in range(i - 1, max(i - 5, -1), -1):
                tj = tokens[j][2]
                tjr = tj.rstrip(".,;:")
                if "=" in tjr:
                    found_op_left = True
                    break
                if tjr in _MATH_OPERATORS:
                    found_op_left = True
                    break
                if tjr and tjr[0] in _MATH_OPERATORS and len(tjr) > 1:
                    found_op_left = True
                    break
                if tj.endswith(":"):
                    break
            if found_op_left:
                left = i
                continue
            break
        left = i

    # --- RIGHT boundary: walk right from last =, using operand absorption ---
    right = last_eq
    last_was_operator = True   # the = itself is an operator, so absorb next operand

    for i in range(last_eq + 1, len(tokens)):
        tok = tokens[i][2]

        if _is_eq_math_token(tok):
            right = i
            t = tok.rstrip(".,;:")
            last_was_operator = (len(t) == 1 and t in _MATH_OPERATORS)
            continue

        if _is_eq_stop(tok):
            # Hard breakers always end the equation span
            if tok.strip(".,;:……()[]{}").lower() in _EQUATION_BREAKERS:
                break
            # After an operator, single-letter stop words (a, b, x, y) are
            # likely variables not articles — absorb them
            cleaned = tok.rstrip(".,;:")
            if last_was_operator and len(cleaned) <= 2:
                right = i
                last_was_operator = False
                continue
            # Look ahead past stop words for more math
            found_math_ahead = False
            for j in range(i + 1, min(i + 5, len(tokens))):
                if _is_eq_math_token(tokens[j][2]):
                    found_math_ahead = True
                    break
                tj = tokens[j][2].rstrip(".,;:")
                if tj and tj[0] in _MATH_OPERATORS and len(tj) > 1:
                    found_math_ahead = True
                    break
            if found_math_ahead:
                right = i
                continue
            break

        # Non-math, non-stop token — check for breaker word stripped of parens
        bare = tok.strip(".,;:()[]{}").lower()
        if bare in _EQUATION_BREAKERS:
            break

        if last_was_operator:
            right = i
            last_was_operator = False
            continue

        # Not after an operator — check lookahead for more math
        found_math = False
        for j in range(i + 1, min(i + 4, len(tokens))):
            if _is_eq_math_token(tokens[j][2]):
                found_math = True
                break
            tj = tokens[j][2].rstrip(".,;:")
            if tj and tj[0] in _MATH_OPERATORS and len(tj) > 1:
                found_math = True
                break
            if _is_eq_stop(tokens[j][2]):
                break
        if found_math:
            right = i
        else:
            break

    # Absorb a trailing unit/label word immediately after the last math token.
    if right + 1 < len(tokens):
        next_word = tokens[right + 1][2].rstrip(".,;:)]}")
        if next_word.lower() in _UNIT_WORDS:
            right += 1

    start_char = tokens[left][0]
    end_char = tokens[right][1]
    result = text[start_char:end_char].strip()
    # Strip label prefixes like "Justification:NIT" → "NIT", "Answer:Y=5" → "Y=5"
    colon_idx = result.find(":")
    if colon_idx >= 0 and colon_idx < len(result) - 1:
        after_colon = result[colon_idx + 1:].lstrip()
        if after_colon and (after_colon[0].isupper() or after_colon[0].isdigit()
                            or after_colon[0] in "=+-(" or after_colon[0] == "\\"):
            result = after_colon
    return result




_BREAKER_SPLIT = re.compile(
    r'(?:,\s*|\s+)(?:[()\[\]]*)?(?:then|if|since|because|where|when|means|that)\b\s*', re.I)


def extract_all_equations(text: str) -> list[tuple[int, int]]:
    """Find all equation spans in text. Returns list of (start, end) char offsets.

    Splits on sentence boundaries and breaker words, then finds equations
    in each segment so a paragraph with multiple equations yields multiple spans.
    """
    # First split on sentence boundaries
    sentences = re.split(r'(?<=[.!?])\s+(?=[A-Z])', text)
    # Then split each sentence on breaker words (then, if, where, etc.)
    segments: list[str] = []
    for sentence in sentences:
        parts = _BREAKER_SPLIT.split(sentence)
        segments.extend(p for p in parts if p.strip())

    spans = []
    offset = 0
    for segment in segments:
        idx = text.find(segment, offset)
        if idx < 0:
            continue
        eq = extract_equation_text(segment)
        if eq:
            eq_start = segment.find(eq)
            if eq_start >= 0:
                abs_start = idx + eq_start
                abs_end = abs_start + len(eq)
                spans.append((abs_start, abs_end))
        offset = idx + len(segment)
    # Deduplicate overlapping spans
    if len(spans) <= 1:
        return spans
    merged = [spans[0]]
    for s, e in spans[1:]:
        ps, pe = merged[-1]
        if s <= pe:
            merged[-1] = (ps, max(pe, e))
        else:
            merged.append((s, e))
    return merged


_LABEL_PREFIX = re.compile(r"^(Answer|Solution|Ans|Sol)\s*:\s*", re.I)


def strip_equation_label(text: str) -> str:
    """Strip leading labels like 'Answer:' from equation text."""
    return _LABEL_PREFIX.sub("", text)


# -------------------------------------------------- latex text normalization

# Greek letters that are ONLY visual lookalikes for Latin in economics EPUBs.
# Math-symbol Greeks (Δ, Σ, Θ, Ψ, Ω, α, β, γ, etc.) go to _LATEX_CHAR_MAP.
_GREEK_TO_LATIN = {
    'Α': 'A', 'Β': 'B', 'Γ': 'G', 'Ε': 'E',
    'Ζ': 'Z', 'Η': 'H', 'Ι': 'I', 'Κ': 'K',
    'Λ': 'L', 'Μ': 'M', 'Ν': 'N', 'Ο': 'O',
    'Π': 'P', 'Ρ': 'P', 'Τ': 'T', 'Υ': 'Y',
    'Φ': 'F', 'Χ': 'X',
}

_LATEX_CHAR_MAP = {
    '–': '-',    # en-dash
    '—': '-',    # em-dash
    '−': '-',    # Unicode minus sign (U+2212)
    '×': ' \\times ',
    '÷': ' \\div ',
    '·': ' \\cdot ',
    '±': ' \\pm ',
    '≈': ' \\approx ',
    '≠': ' \\neq ',
    '≤': ' \\leq ',
    '≥': ' \\geq ',
    '∞': ' \\infty ',
    '√': '\\sqrt',
    '²': '^2',
    '³': '^3',
    '¹': '^1',
    '⁴': '^4',
    '%': '\\%',
    '→': '->',
    '←': '<-',
    # Greek math symbols → LaTeX commands (matching IDML _SPECIAL_CHAR_MAP)
    'Δ': '\\Delta ',
    '∆': '\\Delta ',
    'Σ': '\\Sigma ',
    'Θ': '\\Theta ',
    'Ξ': '\\Xi ',
    'Ψ': '\\Psi ',
    'Ω': '\\Omega ',
    'α': '\\alpha ',
    'β': '\\beta ',
    'γ': '\\gamma ',
    'δ': '\\delta ',
    'ε': '\\epsilon ',
    'θ': '\\theta ',
    'λ': '\\lambda ',
    'μ': '\\mu ',
    'π': '\\pi ',
    'σ': '\\sigma ',
    'τ': '\\tau ',
    'φ': '\\phi ',
    'ψ': '\\psi ',
    'ω': '\\omega ',
}


def normalize_latex(text: str) -> str:
    """Normalize Unicode math chars to ASCII and replace special chars for LaTeX."""
    result = unicodedata.normalize('NFKC', text)
    result = ''.join(_GREEK_TO_LATIN.get(ch, ch) for ch in result)
    for char, repl in _LATEX_CHAR_MAP.items():
        result = result.replace(char, repl)
    result = re.sub(r'  +', ' ', result)
    return result.strip()


def _merge_script_runs(text, runs):
    """Merge adjacent superscript or subscript runs into single spans."""
    scripts = {"superscript": [], "subscript": []}
    others = []
    for r in runs:
        if "superscript" in r.styles:
            scripts["superscript"].append((r.start, r.end))
        elif "subscript" in r.styles:
            scripts["subscript"].append((r.start, r.end))
        else:
            others.append(r)
    merged = list(others)
    for kind, spans in scripts.items():
        if not spans:
            continue
        spans.sort()
        cur_s, cur_e = spans[0]
        for s, e in spans[1:]:
            gap = text[cur_e:s]
            if s <= cur_e or not any(ch.isalpha() for ch in gap):
                cur_e = max(cur_e, e)
            else:
                merged.append(type(runs[0])(
                    text=text[cur_s:cur_e], styles=(kind,),
                    index=[], start=cur_s, end=cur_e))
                cur_s, cur_e = s, e
        merged.append(type(runs[0])(
            text=text[cur_s:cur_e], styles=(kind,),
            index=[], start=cur_s, end=cur_e))
    merged.sort(key=lambda r: r.start)
    return merged


def runs_to_latex(text: str, runs, start: int = 0, end: int | None = None) -> str:
    """Build LaTeX from text + runs, handling subscript/superscript and Unicode."""
    if end is None:
        end = len(text)
    if not runs:
        return normalize_latex(text[start:end])

    relevant = sorted(
        (r for r in runs if r.end > start and r.start < end),
        key=lambda r: r.start,
    )
    if not relevant:
        return normalize_latex(text[start:end])

    has_script = any("superscript" in r.styles or "subscript" in r.styles
                     for r in relevant)
    if has_script:
        relevant = _merge_script_runs(text, relevant)
        relevant = [r for r in relevant if r.end > start and r.start < end]

    parts = []
    pos = start
    for run in relevant:
        rs = max(run.start, start)
        re_ = min(run.end, end)
        if rs < pos:
            continue
        if rs > pos:
            parts.append(normalize_latex(text[pos:rs]))
        chunk = normalize_latex(text[rs:re_])
        if not chunk.strip():
            parts.append(chunk)
        elif "subscript" in run.styles:
            inner = chunk.strip()
            parts.append(f"_{{{inner}}}" if len(inner) > 1 else f"_{inner}")
        elif "superscript" in run.styles:
            inner = chunk.strip()
            parts.append(f"^{{{inner}}}" if len(inner) > 1 else f"^{inner}")
        else:
            parts.append(chunk)
        pos = re_

    if pos < end:
        parts.append(normalize_latex(text[pos:end]))

    return ''.join(parts).strip()


# ------------------------------------------------------------ classification

_ANSWER_PREFIX = re.compile(
    r"^(Answer|Solution|Ans|Sol)\s*[:]\s*[-=]?\s*", re.I)


def classify(text: str, runs=None, max_formula_words: int = 15,
             coverage_threshold: float = 0.6, lead_words: int = 3,
             table_cell: bool = False) -> Classification:
    """Decide whether a block is prose, a formula, or prose containing a formula."""
    # Strip Answer:/Solution: prefix — the label is not part of the equation
    label_match = _ANSWER_PREFIX.match(text)
    if label_match and not table_cell:
        stripped = text[label_match.end():]
        inner = classify(stripped, runs, max_formula_words,
                         coverage_threshold, lead_words, table_cell)
        offset = label_match.end()
        if inner.kind == FORMULA:
            return Classification(TEXT_WITH_FORMULA, offset, len(text),
                                  answer_stripped=True)
        if inner.kind == TEXT_WITH_FORMULA:
            return Classification(TEXT_WITH_FORMULA,
                                  offset + inner.start, offset + inner.end,
                                  answer_stripped=True)
        return inner

    probe = text.translate(DASH_TABLE)
    tokens = _tokens(probe)
    if not tokens:
        return Classification(TEXT)

    has_strong = any(ch in STRONG_MATH for ch in probe) or _TIMES_BETWEEN_NUMBERS.search(probe)
    if not has_strong:
        if table_cell and _RATIO_SLASH.search(probe):
            return Classification(FORMULA, 0, len(text))
        if not (table_cell and _is_table_cell_math(probe)):
            return Classification(TEXT)

    positions = _core_positions(probe)
    core = [any(p in positions for p in range(start, end)) for start, end, _ in tokens]
    for i, flag in enumerate(_superscript_core(tokens, runs)):
        core[i] = core[i] or flag

    if not any(core):
        if table_cell and _LEADS_WITH_OPERATOR.match(probe.strip()):
            return Classification(FORMULA, 0, len(text))
        return Classification(TEXT)

    if table_cell and _LEADS_WITH_OPERATOR.match(probe.strip()):
        return Classification(FORMULA, 0, len(text))

    # Definition detection: "X = long prose description" → TEXT
    # If every = sign has only prose words after it (no numbers,
    # operators, or Greek), it's a definition not a formula.
    # Skipped for table cells — table lines like "= Current assets/current
    # liabilities" are calculations, not definitions.
    if not table_cell:
        eq_tok_indices = [i for i, (_, _, t) in enumerate(tokens) if '=' in t.rstrip(".,;:")]
        if eq_tok_indices:
            all_definitions = True
            for ei_pos, ei in enumerate(eq_tok_indices):
                next_eq = eq_tok_indices[ei_pos + 1] if ei_pos + 1 < len(eq_tok_indices) else len(tokens)
                right_toks = tokens[ei + 1:next_eq]
                if len(right_toks) < 1:
                    continue
                has_math_right = any(_is_eq_math_token(t) for _, _, t in right_toks)
                if has_math_right:
                    all_definitions = False
                    break
            if all_definitions and len(eq_tok_indices) >= 1:
                total_right_toks = sum(
                    1 for ei_pos, ei in enumerate(eq_tok_indices)
                    for _ in tokens[ei + 1:(eq_tok_indices[ei_pos + 1] if ei_pos + 1 < len(eq_tok_indices) else len(tokens))]
                )
                if total_right_toks > 1:
                    return Classification(TEXT)

    adjacent = [_is_math_adjacent(tok) for _, _, tok in tokens]
    first_core = core.index(True)

    best = None
    i = 0
    while i < len(tokens):
        if not core[i]:
            i += 1
            continue
        left = i
        while left - 1 >= 0 and (adjacent[left - 1] or core[left - 1]):
            left -= 1
        right = i
        while right + 1 < len(tokens) and (adjacent[right + 1] or core[right + 1]):
            right += 1
        if best is None or (right - left) > (best[1] - best[0]):
            best = (left, right)
        i = right + 1

    left, right = best

    # Coverage for FORMULA decision uses only the core+adjacent span
    core_coverage = (right - left + 1) / len(tokens)

    if core_coverage >= coverage_threshold:
        return Classification(FORMULA, 0, len(text))
    if first_core <= lead_words and len(tokens) <= max_formula_words:
        stop_count = sum(1 for _, _, t in tokens if _is_eq_stop(t))
        if stop_count <= len(tokens) // 3:
            # Verify right side of = has actual math (not just prose)
            fc_eq = next((i for i, (_, _, t) in enumerate(tokens) if '=' in t.rstrip(".,;:")), None)
            if fc_eq is not None:
                has_mr = any(_is_eq_math_token(tokens[j][2])
                             for j in range(fc_eq + 1, len(tokens)))
                if has_mr:
                    return Classification(FORMULA, 0, len(text))
            else:
                return Classification(FORMULA, 0, len(text))

    # Short text with = and few stop words → likely a formula name = value
    # e.g. "National Income at constant price = 60,000" (1 stop total)
    # vs "If indicator= + appears it shows the favourable trend." (5 stops → prose)
    if len(tokens) <= 20 and not re.search(r'\.\s+[A-Z]', text):
        eq_tok = next((i for i, (_, _, t) in enumerate(tokens) if "=" in t), None)
        if eq_tok is not None:
            stops_before = sum(1 for j in range(eq_tok)
                               if _is_eq_stop(tokens[j][2]))
            total_stops = sum(1 for _, _, t in tokens if _is_eq_stop(t))
            # Verify right side has actual math content (numbers, operators, Greek)
            has_math_r = any(_is_eq_math_token(tokens[j][2])
                             for j in range(eq_tok + 1, len(tokens)))
            if stops_before <= 2 and total_stops <= 4 and eq_tok <= 6 and has_math_r:
                return Classification(FORMULA, 0, len(text))

    # Expand span to absorb word-operands (economics-style equations)
    exp_left, exp_right = left, right
    while exp_left - 1 >= 0:
        if _is_eq_stop(tokens[exp_left - 1][2]):
            break
        exp_left -= 1
    while exp_right + 1 < len(tokens):
        if _is_eq_stop(tokens[exp_right + 1][2]):
            break
        exp_right += 1

    # Multi-operator equations spanning most of the text are formulas.
    # Count operator tokens directly — _core_positions misses standalone +/-
    # between word-operands (economics equations like "Wages + Rent + profit").
    op_indices = []
    for idx_t, (_, _, t) in enumerate(tokens):
        ts = t.rstrip(".,;:")
        if "=" in ts or ts in _MATH_OPERATORS or (ts and ts[0] in _MATH_OPERATORS and len(ts) > 1):
            op_indices.append(idx_t)
    if len(op_indices) >= 3:
        op_span = (op_indices[-1] - op_indices[0] + 1) / len(tokens)
        if op_span >= 0.65:
            return Classification(FORMULA, 0, len(text))

    left, right = exp_left, exp_right

    # For TEXT_WITH_FORMULA, use the dedicated equation extractor for better spans
    eq_text = extract_equation_text(text)
    if eq_text:
        start = text.find(eq_text)
        if start >= 0:
            return Classification(TEXT_WITH_FORMULA, start, start + len(eq_text))

    right = _absorb_trailing_operand(tokens, right)
    return Classification(TEXT_WITH_FORMULA, tokens[left][0], tokens[right][1])


# --------------------------------------------------------------------- LaTeX


def to_latex(text: str, runs=None, start: int = 0, end: int | None = None,
             style: str = "latex") -> str:
    """Render the span ``text[start:end]`` as LaTeX using run-level sup/sub marks."""
    if end is None:
        end = len(text)
    pieces = []
    cursor = start
    for span_start, span_end, wrapper in _sup_sub_spans(text, runs, start, end):
        if span_start > cursor:
            pieces.append(_convert(text[cursor:span_start], style))
        inner = _convert(text[span_start:span_end], style).strip()
        if inner:
            pieces.append("%s{%s}" % (wrapper, inner) if len(inner) > 1
                          else "%s%s" % (wrapper, inner))
        cursor = span_end
    if cursor < end:
        pieces.append(_convert(text[cursor:end], style))
    return re.sub(r"\s+", " ", "".join(pieces)).strip()


def _sup_sub_spans(text, runs, start, end):
    spans = []
    for run in runs or []:
        if "superscript" in run.styles:
            wrapper = "^"
        elif "subscript" in run.styles:
            wrapper = "_"
        else:
            continue
        body = run.text.strip()
        if body and all(ch.isspace() or ch in _OPERATOR_GLYPHS for ch in body):
            continue
        left = max(run.start, start)
        right = min(run.end, end)
        if left < right:
            spans.append([left, right, wrapper])
    spans.sort()

    merged = []
    for span in spans:
        if merged and merged[-1][2] == span[2] and not text[merged[-1][1]:span[0]].strip():
            merged[-1][1] = span[1]
            continue
        merged.append(span)
    return merged


def _convert(fragment: str, style: str = "latex") -> str:
    if not fragment:
        return ""
    out = fragment.translate(DASH_TABLE)
    out = out.replace("%", r"\%").replace("&", r"\&")
    for src, dst in SYMBOL_LATEX.items():
        out = out.replace(src, dst + " " if dst[-1].isalpha() else dst)
    if style != "plain":
        out = re.sub(r"(?<=[\d)\]}])\s+[xX]\s+(?=[\d(\[{])", r" \\times ", out)
    return re.sub(r"\s+", " ", out)


def anchor(hash_id: str) -> str:
    return "[formula_anchor_%s]" % hash_id
