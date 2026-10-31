from __future__ import annotations

import json
import re
import time
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed

PORTS = [8006, 8206, 8406, 8606, 8806, 9006, 9206, 8007, 8207, 
         8407, 8607, 8807, 9007, 9207, 8008, 8208, 8408, 8608, 8808, 9008, 9208, 8009]
SERVER_IP = "103.181.20.53"
LLAMA_URLS = [f"http://{SERVER_IP}:{port}/v1/chat/completions" for port in PORTS]
LLAMA_WORKERS = len(LLAMA_URLS) * 8

_healthy_urls: list[str] = []
_port_shift_log: list[str] = []

_POS_CHOICES = ("noun, proper noun, verb, adjective, adverb, article, "
                "preposition, conjunction, pronoun, numeral, symbol")

_CONTEXT_WINDOW = 2
_BATCH_WORD_LIMIT = 500


def _is_batchable(block_type: str, word_count: int) -> bool:
    """Chunks that can be grouped into a single LLM call."""
    if block_type == "table":
        return False
    if word_count <= 15:
        return False
    return True


def _create_batches(tasks: list[tuple], word_limit: int) -> list[list[tuple]]:
    """Group batchable tasks into batches up to word_limit total words."""
    batches = []
    current = []
    current_words = 0
    for task in tasks:
        text = task[1].strip()
        words = len(text.split())
        if current and current_words + words > word_limit:
            batches.append(current)
            current = [task]
            current_words = words
        else:
            current.append(task)
            current_words += words
    if current:
        batches.append(current)
    return batches


def _llama_batch_summary(args) -> list[tuple[int, str, str]]:
    """Send multiple chunks in one LLM call using XML boundary markers."""
    batch_items, batch_idx = args

    chunk_parts = []
    for i, (idx, text, prev_text, next_text, block_type, applied_style) in enumerate(batch_items, 1):
        text_clean = text.strip()[:400]
        type_attr = ""
        if block_type == "heading":
            type_attr = ' type="heading"'
        elif block_type == "front_matter":
            type_attr = ' type="front_matter"'
        chunk_parts.append(f"<CHUNK {i}{type_attr}>\n{text_clean}\n</CHUNK {i}>")

    n = len(batch_items)
    chunks_block = "\n\n".join(chunk_parts)

    system_msg = "You rephrase and summarize text in your own words. Never copy phrases from the source."
    user_msg = (
        f"<task>\nBelow are {n} text chunks wrapped in XML tags.\n"
        "For EACH chunk, explain its main point in 50-70 words.\n"
        "IMPORTANT: Use your own words — do NOT copy any phrase from the chunks.\n"
        "Rephrase everything completely. No preamble, no sign-off.\n"
        'For chunks marked type="heading", begin with "This section" or "This chapter".\n'
        'For chunks marked type="front_matter", state what kind of element it is.\n'
        f"Wrap each summary: <SUMMARY 1>text</SUMMARY 1> through <SUMMARY {n}>text</SUMMARY {n}>.\n"
        "Write ONLY the tagged summaries.\n"
        "</task>\n\n"
        f"{chunks_block}"
    )

    payload = json.dumps({
        "messages": [
            {"role": "system", "content": system_msg},
            {"role": "user", "content": user_msg},
        ],
        "max_tokens": 200 * n,
        "temperature": 0.0,
        "stream": False,
        "think": False,
    }).encode("utf-8")

    urls = _healthy_urls or LLAMA_URLS
    max_retries = min(len(urls), 5)
    last_reason = ""
    for attempt in range(max_retries):
        target_url = urls[(batch_idx + attempt) % len(urls)]
        port_num = target_url.split(":")[-1].split("/")[0]
        req = urllib.request.Request(target_url, data=payload,
                                    headers={"Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=300) as resp:
                data = json.loads(resp.read().decode("utf-8"))
                choices = data.get("choices", [])
                if not choices:
                    last_reason = "no_choices_in_response"
                    _log_port_event("[port %s] batch %d: no choices, shifting to next port" % (port_num, batch_idx))
                    continue
                msg = choices[0].get("message", {})
                response = msg.get("content", "").strip()
                if not response:
                    finish = choices[0].get("finish_reason", "")
                    last_reason = "empty_content (finish=%s)" % finish
                    _log_port_event("[port %s] batch %d: empty response (finish=%s), shifting to next port" % (port_num, batch_idx, finish))
                    continue

                results = []
                for i, (idx, text, prev, nxt, btype, style) in enumerate(batch_items, 1):
                    pattern = rf"<SUMMARY {i}>(.*?)</SUMMARY {i}>"
                    m = re.search(pattern, response, re.DOTALL)
                    if m:
                        s = _clean_summary(m.group(1))
                        if btype == "heading" and not s.lower().startswith(("this section", "this chapter")):
                            s = "This section " + s
                        results.append((idx, s, ""))
                    else:
                        results.append((idx, "", "batch_parse_failed"))
                return results
        except urllib.error.URLError as e:
            last_reason = "timeout" if "timed out" in str(e) else "url_error: %s" % e
            _log_port_event("[port %s] batch %d FAILED: %s -> shifting to next port" % (port_num, batch_idx, last_reason))
        except Exception as e:
            last_reason = "exception: %s" % type(e).__name__
            _log_port_event("[port %s] batch %d FAILED: %s -> shifting to next port" % (port_num, batch_idx, last_reason))
        if attempt < max_retries - 1:
            time.sleep(0.2)

    return [(idx, "", "%s (after %d ports tried)" % (last_reason, max_retries))
            for idx, *_ in batch_items]


def _tokenize_for_pos(text: str) -> list[str]:
    toks = re.findall(r"[^\W\d_]+(?:[''\-][^\W\d_]+)*|\d+(?:\.\d+)?|[^\s\w]", text)
    return [t for t in toks if re.search(r"[^\W\d_]|\d", t)]


def _log_port_event(msg: str) -> None:
    print("    " + msg)
    _port_shift_log.append(msg)


def _build_context_block(prev_clean: str, next_clean: str) -> str:
    parts = []
    if prev_clean:
        parts.append(f"  <before>{prev_clean}</before>")
    if next_clean:
        parts.append(f"  <after>{next_clean}</after>")
    if not parts:
        return ""
    return "<surrounding_context>\n" + "\n".join(parts) + "\n</surrounding_context>\n\n"

_FORBIDDEN_STARTS = re.compile(
    r"^(The excerpt|The text|This passage|The author|The given text|"
    r"The provided text|The above text|This text|The chunk|The content|"
    r"The paragraph|The piece|The selected text|The section text|"
    r"The following|In this)\b",
    re.IGNORECASE,
)


def _clean_summary(summary: str) -> str:
    """Strip XML tags, forbidden phrase starts, robotic openers, and repetition loops."""
    s = re.sub(r"</?\w[^>]*>", "", summary).strip()
    s = s.split("\n")[0].strip()
    s = re.sub(r"^(Summary|Answer|Main point):\s*", "", s, flags=re.IGNORECASE).strip()
    s = re.sub(
        r"^(The main point being made (here |in the passage )?is that |"
        r"The main point of (this passage|the passage) is that |"
        r"The passage (discusses|describes|explains|states|suggests|argues|mentions) (that )?|"
        r"Discusses |Describes )",
        "", s, flags=re.IGNORECASE
    ).strip()
    if s and s[0].islower():
        s = s[0].upper() + s[1:]
    m = _FORBIDDEN_STARTS.match(s)
    if m:
        rest = s[m.end():].lstrip(" ,;:")
        if rest:
            s = rest[0].upper() + rest[1:]
    words = s.split()
    if len(words) > 20:
        for n in range(5, min(15, len(words)//3)):
            phrase = " ".join(words[:n])
            count = s.lower().count(phrase.lower())
            if count >= 3:
                s = " ".join(words[:n*2])
                break
    if not s:
        return summary.strip().split("\n")[0].strip()
    return s

def _is_verbatim(summary: str, source: str) -> bool:
    """Check if summary copies >50% of sequential words from source."""
    s_words = summary.lower().split()
    src_words = source.lower().split()
    if len(s_words) < 5 or len(src_words) < 5:
        return False
    src_str = " ".join(src_words)
    matches = 0
    for i in range(len(s_words) - 5):
        phrase = " ".join(s_words[i:i+6])
        if phrase in src_str:
            matches += 6
    return matches > len(s_words) * 0.5


def _make_retry_prompt(text_clean: str, word_count: int) -> tuple[str, str]:
    """Stricter prompt for retry after verbatim detection."""
    system_msg = "You rephrase text. Use completely different words and sentence structure. NEVER copy any phrase from the original."
    user_msg = (
        "Read this text, then explain its main point in your own words. "
        "Use COMPLETELY DIFFERENT words and sentence structure. "
        "Do NOT reuse any phrase of 3+ words from the original. "
        "Keep it between 40 and 70 words.\n\n"
        f"Original text: {text_clean[:400]}\n\nYour rephrasing:"
    )
    return system_msg, user_msg






def _cell_to_str(cell: dict) -> str:
    if not isinstance(cell, dict):
        return str(cell).strip()
    return str(cell.get("text", "")).strip()


def _table_to_text(item: dict) -> str:
    n_rows = item.get("rows", 0)
    n_cols = item.get("columns", 0)
    n_hdr = item.get("header_rows", 0)
    cells = item.get("cells") or []
    if not cells:
        return f"[TABLE {item.get('table_index', '')}] ({n_rows} rows x {n_cols} cols)"
    by_row: dict[int, list[dict]] = {}
    for c in cells:
        if isinstance(c, dict):
            by_row.setdefault(c.get("row_index", 0), []).append(c)
    rows = [sorted(by_row[r], key=lambda c: c.get("col_index", 0))
            for r in sorted(by_row)]
    lines = [f"Table ({n_rows} rows x {n_cols} cols):"]
    for r_idx, row in enumerate(rows[:6]):
        cell_texts = [_cell_to_str(c) for c in row]
        label = "header" if r_idx < n_hdr else f"row {r_idx - n_hdr + 1}"
        lines.append(f"  {label}: {' | '.join(cell_texts[:8])}")
    if n_rows > 6:
        lines.append(f"  ... ({n_rows - 6} more rows)")
    return "\n".join(lines)


def _llama_summary(args) -> tuple[int, str]:
    idx, text, prev_text, next_text, block_type, applied_style = args
    text_clean = text.strip()
    word_count = len(text_clean.split())

    if word_count <= 30 or block_type == "heading":
        prev_clean = prev_text.strip()[-120:]
        next_clean = next_text.strip()[:120]
        context_block = _build_context_block(prev_clean, next_clean)
    else:
        context_block = ""

    if block_type == "table":
        system_msg = "You describe data tables in your own words. Never copy cell values."
        user_msg = (
            "Look at this table data and answer: What information does this table show? "
            "What kind of data is being presented?\n"
            "Answer in 50-70 words using your own words. Do not list cell values.\n\n"
            f"{text_clean[:500]}\n\nAnswer:"
        )
    elif block_type == "heading":
        system_msg = "You describe book sections. Always begin with 'This section' or 'This chapter'."
        user_msg = (
            f"The heading is: \"{text_clean}\"\n\n"
            "Based on the heading and the text that follows it below, "
            "what topics does this section cover? "
            "Begin your answer with 'This section' or 'This chapter'. "
            "Answer in 50-70 words.\n\n"
            f"Text after the heading:\n{next_clean if word_count <= 30 else next_text.strip()[:200]}\n\nAnswer:"
        )
    elif block_type == "front_matter":
        system_msg = "You identify and describe book elements."
        user_msg = (
            "What kind of book element is this (e.g. dedication, preface, foreword, "
            "copyright notice, author bio, epigraph)? What does it say?\n"
            "Answer in 40-70 words using your own words.\n\n"
            f"{text_clean}\n\nAnswer:"
        )
    elif word_count <= 8:
        system_msg = "You explain what short text fragments mean in their surrounding context."
        user_msg = (
            f"Short fragment: \"{text_clean}\"\n\n"
            "Surrounding context:\n"
            f"Before: {prev_text.strip()[-150:]}\n"
            f"After: {next_text.strip()[:150]}\n\n"
            "What does this fragment refer to? What is its role here? "
            "Answer in 20-40 words.\n\nAnswer:"
        )
    elif word_count <= 30:
        system_msg = "You explain text in your own words."
        user_msg = (
            "Read this passage and the surrounding context. "
            "Then answer: What is the main point of this passage?\n"
            "Use your own words — do NOT copy phrases from the passage. "
            "Answer in 40-60 words.\n\n"
            f"{context_block}"
            f"Passage: {text_clean}\n\nMain point:"
        )
    else:
        system_msg = "You explain text in your own words."
        user_msg = (
            "Read this passage carefully. "
            "Then answer: What is the main point being made here?\n"
            "IMPORTANT: Use completely different words — do NOT copy any phrase from the passage. "
            "Rephrase everything. Answer in 50-70 words.\n\n"
            f"Passage: {text_clean[:400]}\n\nMain point:"
        )

    payload = json.dumps({
        "messages": [
            {"role": "system", "content": system_msg},
            {"role": "user", "content": user_msg},
        ],
        "max_tokens": 200,
        "temperature": 0.3,
        "stream": False,
        "think": False,
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
            with urllib.request.urlopen(req, timeout=180) as resp:
                data = json.loads(resp.read().decode("utf-8"))
                choices = data.get("choices", [])
                if not choices:
                    last_reason = "no_choices_in_response"
                    _log_port_event("[port %s] no choices in response for chunk %d, shifting to next port" % (port_num, idx))
                    continue
                msg = choices[0].get("message", {})
                response = msg.get("content", "").strip()
                finish = choices[0].get("finish_reason", "")
                if not response and finish == "length":
                    last_reason = "max_tokens_exceeded"
                    _log_port_event("[port %s] max_tokens exceeded for chunk %d, shifting to next port" % (port_num, idx))
                    continue
                if not response:
                    last_reason = "empty_content (finish=%s)" % finish
                    _log_port_event("[port %s] empty response (finish=%s) for chunk %d, shifting to next port" % (port_num, finish, idx))
                    continue

                if block_type == "heading":
                    raw = response.lstrip().split("\n")[0].strip()
                    raw = re.sub(r"^(Answer|Summary|Main point):\s*", "", raw, flags=re.IGNORECASE).strip()
                    if raw.lower().startswith(("this section", "this chapter")):
                        summary = _clean_summary(raw)
                    else:
                        summary = _clean_summary("This section " + raw)
                else:
                    raw = response.lstrip().split("\n")[0].strip()
                    raw = re.sub(r"^(Answer|Summary|Main point):\s*", "", raw, flags=re.IGNORECASE).strip()
                    summary = _clean_summary(raw)

                if _is_verbatim(summary, text_clean) and attempt < max_retries - 1:
                    retry_sys, retry_usr = _make_retry_prompt(text_clean, word_count)
                    retry_payload = json.dumps({
                        "messages": [
                            {"role": "system", "content": retry_sys},
                            {"role": "user", "content": retry_usr},
                        ],
                        "max_tokens": 200,
                        "temperature": 0.5,
                        "stream": False,
                        "think": False,
                    }).encode("utf-8")
                    retry_url = urls[(idx + attempt + 1) % len(urls)]
                    retry_req = urllib.request.Request(retry_url, data=retry_payload,
                                                      headers={"Content-Type": "application/json"})
                    try:
                        with urllib.request.urlopen(retry_req, timeout=180) as rr:
                            rd = json.loads(rr.read().decode("utf-8"))
                            rc = rd.get("choices", [])
                            if rc:
                                rm = rc[0].get("message", {}).get("content", "").strip()
                                if rm:
                                    retry_clean = _clean_summary(rm.split("\n")[0].strip())
                                    if retry_clean and not _is_verbatim(retry_clean, text_clean):
                                        summary = retry_clean
                    except Exception as ve:
                        vport = retry_url.split(":")[-1].split("/")[0]
                        _log_port_event("[port %s] verbatim retry failed: %s" % (vport, ve))

                return idx, summary, ""
        except urllib.error.URLError as e:
            last_reason = "timeout" if "timed out" in str(e) else "url_error: %s" % e
            _log_port_event("[port %s] FAILED for chunk %d: %s -> shifting to next port" % (port_num, idx, last_reason))
        except Exception as e:
            last_reason = "exception: %s" % type(e).__name__
            _log_port_event("[port %s] FAILED for chunk %d: %s -> shifting to next port" % (port_num, idx, last_reason))
        if attempt < max_retries - 1:
            time.sleep(0.2)
    return idx, "", "%s (after %d ports tried)" % (last_reason, max_retries)


def add_chunk_summaries(items: list[dict], *, skip: bool = False,
                        log_data: dict | None = None,
                        only_empty: bool = False,
                        batch: bool = False) -> None:
    """Fill chunk_context_summary on every item that has the field."""
    if log_data is None:
        log_data = {}

    if only_empty:
        targets = [i for i, item in enumerate(items)
                   if "chunk_context_summary" in item
                   and not item["chunk_context_summary"].strip()]
    else:
        targets = [i for i, item in enumerate(items) if "chunk_context_summary" in item]
    if not targets:
        log_data["summaries"] = {"total": 0, "status": "no_targets"}
        return

    if skip:
        print(f"    Chunk summaries skipped (--no-summary): {len(targets)} block(s)")
        log_data["summaries"] = {"total": len(targets), "status": "skipped"}
        return

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
        print("    Ports DOWN:")
        for port, reason in down_ports:
            print("      port %d -> %s" % (port, reason))
    if up_ports:
        print("    Ports UP: %s" % ", ".join(str(p) for p in up_ports))

    if not up_ports:
        msg = "All %d Llama ports are down" % len(PORTS)
        print("    " + msg)
        log_data["summaries"] = {"total": len(targets), "status": "server_unreachable",
                                 "error": "; ".join("port %d: %s" % (p, r) for p, r in down_ports)}
        raise ConnectionError(msg)

    global _healthy_urls
    _healthy_urls = [f"http://{SERVER_IP}:{port}/v1/chat/completions" for port in up_ports]
    _port_shift_log.clear()
    log_data["port_health"] = {
        "up": [p for p in up_ports],
        "down": [(p, r) for p, r in down_ports],
    }
    print(f"    Llama array: {len(up_ports)}/{len(PORTS)} GPUs active")

    block_texts = []
    for item in items:
        if item.get("type") == "table":
            block_texts.append(_table_to_text(item))
        else:
            block_texts.append(item.get("prompt_text", "").strip())

    tasks = []
    for i in targets:
        text = block_texts[i]
        if not text:
            continue
        block_type = items[i].get("type", "text")
        applied_style = items[i].get("applied_style", "")
        prev_parts = []
        for j in range(i - 1, -1, -1):
            if block_texts[j]:
                prev_parts.append(block_texts[j])
                if len(prev_parts) >= _CONTEXT_WINDOW:
                    break
        next_parts = []
        for j in range(i + 1, len(items)):
            if block_texts[j]:
                next_parts.append(block_texts[j])
                if len(next_parts) >= _CONTEXT_WINDOW:
                    break
        tasks.append((i, text, "\n".join(reversed(prev_parts)),
                      "\n".join(next_parts), block_type, applied_style))

    total_chunks = len(tasks)
    failed_log = []
    start_time = time.time()
    last_print_time = start_time

    if batch:
        individual_tasks = []
        batchable_tasks = []
        for task in tasks:
            idx, text, prev, nxt, btype, style = task
            wc = len(text.strip().split())
            if _is_batchable(btype, wc):
                batchable_tasks.append(task)
            else:
                individual_tasks.append(task)
        batches = _create_batches(batchable_tasks, _BATCH_WORD_LIMIT)
        total_calls = len(individual_tasks) + len(batches)
        print(f"    Batch mode: {len(batchable_tasks)} chunks in {len(batches)} batches + "
              f"{len(individual_tasks)} individual = {total_calls} LLM calls "
              f"(was {total_chunks})")
    else:
        individual_tasks = tasks
        batches = []
        total_calls = total_chunks
        print(f"    Sending {total_calls} blocks to GPU ({LLAMA_WORKERS} workers) ...")

    done_calls = 0

    def _store_result(idx, summary, reason):
        items[idx]["chunk_context_summary"] = summary
        if not summary:
            failed_log.append({
                "item_index": idx,
                "type": items[idx].get("type", ""),
                "prompt_text": items[idx].get("prompt_text", "")[:100],
                "reason": reason or "empty_after_parse",
            })

    with ThreadPoolExecutor(max_workers=LLAMA_WORKERS) as executor:
        futures = {}
        for task in individual_tasks:
            f = executor.submit(_llama_summary, task)
            futures[f] = ("single", task)
        for bi, b in enumerate(batches):
            f = executor.submit(_llama_batch_summary, (b, bi))
            futures[f] = ("batch", b)

        for future in as_completed(futures):
            kind, data = futures[future]
            try:
                if kind == "single":
                    _, summary, reason = future.result()
                    _store_result(data[0], summary, reason)
                else:
                    batch_results = future.result()
                    parse_failed = []
                    for idx, summary, reason in batch_results:
                        if reason == "batch_parse_failed":
                            parse_failed.append(idx)
                        _store_result(idx, summary, reason)
                    if parse_failed:
                        retry_tasks = [t for t in data if t[0] in parse_failed]
                        retry_futures = {executor.submit(_llama_summary, t): t
                                         for t in retry_tasks}
                        for rf in as_completed(retry_futures):
                            rt = retry_futures[rf]
                            try:
                                _, s, r = rf.result()
                                items[rt[0]]["chunk_context_summary"] = s
                                if s:
                                    for fl in failed_log:
                                        if fl["item_index"] == rt[0]:
                                            failed_log.remove(fl)
                                            break
                            except Exception:
                                pass
            except Exception as e:
                if kind == "single":
                    items[data[0]]["chunk_context_summary"] = ""
                    failed_log.append({
                        "item_index": data[0],
                        "type": items[data[0]].get("type", ""),
                        "prompt_text": items[data[0]].get("prompt_text", "")[:100],
                        "reason": "executor_exception: %s" % type(e).__name__,
                    })
                else:
                    for idx, *_ in data:
                        items[idx]["chunk_context_summary"] = ""
                        failed_log.append({
                            "item_index": idx,
                            "type": items[idx].get("type", ""),
                            "prompt_text": items[idx].get("prompt_text", "")[:100],
                            "reason": "batch_exception: %s" % type(e).__name__,
                        })
            done_calls += 1
            if done_calls % 20 == 0 or done_calls == total_calls:
                now = time.time()
                chunk_time = now - last_print_time
                last_print_time = now
                print(f"    Summaries: {done_calls}/{total_calls}  [{chunk_time:.1f}s]")

    elapsed = time.time() - start_time
    success = total_chunks - len(failed_log)
    print(f"    Done: {total_chunks} summaries ({success} success, {len(failed_log)} empty) — {elapsed:.1f}s total")
    if _port_shift_log:
        log_data["port_shifts"] = list(_port_shift_log)

    if failed_log:
        print(f"\n    --- Empty chunk summaries ({len(failed_log)}) ---")
        for entry in failed_log:
            prompt = entry["prompt_text"]
            if len(prompt) > 60:
                prompt = prompt[:57] + "..."
            print(f"    item[{entry['item_index']}] ({entry['type']}): {entry['reason']}")
            print(f"      text: {prompt}")
        print()

    log_data["summaries"] = {
        "total": total_chunks,
        "success": success,
        "empty": len(failed_log),
        "elapsed_s": round(elapsed, 1),
        "workers": LLAMA_WORKERS,
        "failures": failed_log,
    }
