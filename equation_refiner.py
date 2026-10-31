from __future__ import annotations

import json
import re
import time
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed

PORTS = [8009]
SERVER_IP = "103.181.20.53"
LLAMA_URLS = [f"http://{SERVER_IP}:{port}/v1/chat/completions" for port in PORTS]
LLAMA_WORKERS = len(LLAMA_URLS) * 8

_healthy_urls: list[str] = []
_port_shift_log: list[str] = []


def _log_port_event(msg: str) -> None:
    print("    " + msg)
    _port_shift_log.append(msg)

_REPEATING = re.compile(r"(.{2,20}?)\1{4,}")
_SPECIAL_TOKEN = re.compile(r"<\|[^>]*\|?>")
_PROMPT_ECHO = re.compile(r"\b(Input|Output)\s*:", re.I)

_SYSTEM_MSG = "You clean math equations. Remove English words. Keep only math. One line output."

_USER_PROMPT = """\
Remove ALL English prose words from this equation. Keep ONLY:
- Variable names: single letters (Y, C, a, x) and abbreviations (GDP, MPC, NDP, NIT)
- Numbers, operators (= + - / *), Greek letters (\\Delta), arrows (->)
- Subscripts and superscripts

Remove: this, that, and, the, equal, given, means, rises, crores, subsidies, employees, wages, salaries, income, saving, consumption, production, expenditure, compensation, surplus, contribution, accommodation, ALL other English words.

These books will be translated to Spanish and Arabic. English words MUST NOT be in the equation.

Examples:
subsidies. NIT = Indirect Taxes - Subsidies. -> NIT = Indirect Taxes - Subsidies.
employees =97500. -> =97500.
equal C =Y .....Given -> C =Y
\\Delta y=10 crores Saving rises to 9 means \\Delta s=4 crores -> \\Delta y=10, \\Delta s=4
economy, i.e., AD = C + I. -> AD = C + I
And C = a + bY -> C = a + bY
Compensation of Employees= 60,000 + 30,000 + 7500. -> = 60,000 + 30,000 + 7500.
NDP_{{FC}}= wages in salaries +Employees contribution -> NDP_{{FC}}=

Input: {latex}
Output:"""


def _is_garbage(llm_output: str, original: str) -> bool:
    """Detect garbage LLM output."""
    if not llm_output:
        return True
    if llm_output.lower().strip() in ("thought", "thinking", "answer"):
        return True
    if _SPECIAL_TOKEN.search(llm_output):
        return True
    if llm_output.startswith("<"):
        return True
    if _REPEATING.search(llm_output):
        return True
    if _PROMPT_ECHO.search(llm_output):
        return True
    if "\n" in llm_output:
        return True
    if len(llm_output) > len(original) * 1.5 + 20:
        return True
    unique_chars = set(llm_output.replace(" ", ""))
    if len(unique_chars) <= 3 and len(llm_output) > 10:
        return True
    return False


def _word_overlap(llm_output: str, original: str) -> float:
    """Fraction of original's significant words present in LLM output."""
    orig_words = {w.lower().strip(".,;:()") for w in original.split() if len(w) > 2}
    if not orig_words:
        return 1.0
    llm_words = {w.lower().strip(".,;:()") for w in llm_output.split()}
    return len(orig_words & llm_words) / len(orig_words)


def _llama_latex(args) -> tuple[int, str]:
    """Worker: send one equation's heuristic latex to the LLM for cleanup."""
    idx, latex, eq_type = args
    user_msg = _USER_PROMPT.format(latex=latex.strip())

    max_tokens = 2000

    payload = json.dumps({
        "messages": [
            {"role": "system", "content": _SYSTEM_MSG},
            {"role": "user", "content": user_msg},
        ],
        "max_tokens": max_tokens,
        "temperature": 0.0,
        "stream": False,
        "think": True,
    }).encode("utf-8")

    urls = _healthy_urls or LLAMA_URLS
    max_retries = min(len(urls), 5)
    last_reason = ""
    for attempt in range(max_retries):
        target_url = urls[(idx + attempt) % len(urls)]
        port_num = target_url.split(":")[-1].split("/")[0]
        req = urllib.request.Request(target_url, data=payload,
                                    headers={"Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                data = json.loads(resp.read().decode("utf-8"))
                choices = data.get("choices", [])
                if not choices:
                    last_reason = "no_choices"
                    _log_port_event("[port %s] eq %d: no choices, shifting to next port" % (port_num, idx))
                    continue
                msg = choices[0].get("message", {})
                response = msg.get("content", "").strip()
                if not response and choices[0].get("finish_reason") == "length":
                    last_reason = "max_tokens"
                    _log_port_event("[port %s] eq %d: max_tokens exceeded, shifting to next port" % (port_num, idx))
                    continue
                if not response:
                    last_reason = "empty_response"
                    _log_port_event("[port %s] eq %d: empty response, shifting to next port" % (port_num, idx))
                    continue
                result = response.split("\n")[0].strip()
                result = re.sub(r"^```(?:latex)?\s*", "", result)
                result = re.sub(r"\s*```$", "", result)
                result = result.strip("` ")
                result = _SPECIAL_TOKEN.sub("", result).strip()

                if _is_garbage(result, latex):
                    return idx, ""

                if _word_overlap(result, latex) < 0.25:
                    return idx, ""

                return idx, result
        except urllib.error.URLError as e:
            last_reason = "timeout" if "timed out" in str(e) else "url_error: %s" % e
            _log_port_event("[port %s] eq %d FAILED: %s -> shifting to next port" % (port_num, idx, last_reason))
        except Exception as e:
            last_reason = "exception: %s" % type(e).__name__
            _log_port_event("[port %s] eq %d FAILED: %s -> shifting to next port" % (port_num, idx, last_reason))
        if attempt < max_retries - 1:
            time.sleep(0.2)
    return idx, ""


def refine_equations_via_llm(equations: list[dict], *, skip: bool = False,
                             log_data: dict | None = None) -> None:
    """Refine equation latex in each entry using the LLM."""
    if log_data is None:
        log_data = {}

    if not equations:
        log_data["equations"] = {"total": 0, "status": "no_equations"}
        return

    targets = [(i, eq) for i, eq in enumerate(equations)
               if eq.get("latex") and eq["latex"].strip()]
    if not targets:
        log_data["equations"] = {"total": 0, "status": "no_targets"}
        return

    if skip:
        print(f"    LaTeX refinement skipped (--no-summary): {len(targets)} equation(s)")
        log_data["equations"] = {"total": len(targets), "status": "skipped"}
        return

    global _healthy_urls
    up_ports = []
    down_ports = []
    for port in PORTS:
        health_url = f"http://{SERVER_IP}:{port}/health"
        try:
            with urllib.request.urlopen(health_url, timeout=5) as r:
                if r.status == 200:
                    up_ports.append(port)
                else:
                    down_ports.append((port, "status %d" % r.status))
        except urllib.error.HTTPError:
            up_ports.append(port)
        except Exception as e:
            down_ports.append((port, str(e)))

    if down_ports:
        print("    Equation ports DOWN:")
        for port, reason in down_ports:
            print("      port %d -> %s" % (port, reason))
    if up_ports:
        print("    Equation ports UP: %s" % ", ".join(str(p) for p in up_ports))

    if not up_ports:
        msg = "All %d equation Llama ports are down" % len(PORTS)
        print("    " + msg)
        log_data["equations"] = {"total": len(targets), "status": "server_unreachable",
                                 "error": "; ".join("port %d: %s" % (p, r) for p, r in down_ports)}
        raise ConnectionError(msg)

    _healthy_urls = [f"http://{SERVER_IP}:{port}/v1/chat/completions" for port in up_ports]
    _port_shift_log.clear()
    log_data["eq_port_health"] = {
        "up": [p for p in up_ports],
        "down": [(p, r) for p, r in down_ports],
    }
    print(f"    Llama array active for LaTeX: {len(up_ports)}/{len(PORTS)} GPUs")

    tasks = []
    for i, eq in targets:
        latex = eq["latex"]
        eq_type = eq.get("type", "formula")
        tasks.append((i, latex, eq_type))

    total = len(tasks)
    done = 0
    rejected = 0
    rejected_log = []
    start_time = time.time()
    print(f"    Sending {total} equations to GPU for refinement ({LLAMA_WORKERS} workers) ...")

    with ThreadPoolExecutor(max_workers=LLAMA_WORKERS) as executor:
        future_to_task = {executor.submit(_llama_latex, task): task for task in tasks}
        for future in as_completed(future_to_task):
            task = future_to_task[future]
            idx = task[0]
            try:
                _, result = future.result()
                if result:
                    equations[idx]["latex"] = result
                else:
                    rejected += 1
                    rejected_log.append({
                        "eq_index": idx,
                        "latex": equations[idx].get("latex", "")[:100],
                        "reason": "llm_rejected_or_garbage",
                    })
            except Exception as e:
                rejected += 1
                rejected_log.append({
                    "eq_index": idx,
                    "latex": equations[idx].get("latex", "")[:100],
                    "reason": "exception: %s" % type(e).__name__,
                })
            done += 1
            if done % 50 == 0 or done == total:
                elapsed = time.time() - start_time
                print(f"    LaTeX: {done}/{total}  [{elapsed:.1f}s]")

    elapsed = time.time() - start_time
    print(f"    Done: {total} refined, {rejected} kept heuristic (LLM rejected) — {elapsed:.1f}s total")
    if _port_shift_log:
        log_data["eq_port_shifts"] = list(_port_shift_log)

    log_data["equations"] = {
        "total": total,
        "refined": total - rejected,
        "rejected": rejected,
        "elapsed_s": round(elapsed, 1),
        "workers": LLAMA_WORKERS,
        "failures": rejected_log,
    }
