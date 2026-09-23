"""Run the GB55031 workbook questions against the current local corpus.

This evaluator bypasses answer caches and conversation persistence while using
the same model, system instructions, tools, and max-turn limit as ask_question.
"""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import re
import sys
from time import perf_counter, sleep
import xml.etree.ElementTree as ET
import zipfile

from ask_question import _INSTRUCTION_ZH, _references_from_items
from pageindex import PageIndexLocalClient


ROOT = Path(__file__).resolve().parent
SOURCE = ROOT.parent / "benchmark_55031_评测结果.xlsx"
OUTPUT = ROOT / "outputs" / "benchmark_55031_description_eval"
RESULTS = OUTPUT / "results.jsonl"
NS = {"x": "http://schemas.openxmlformats.org/spreadsheetml/2006/main"}
TARGET = re.compile(r"(\d+\.\d+\.\d+)")


def workbook_rows() -> list[dict]:
    with zipfile.ZipFile(SOURCE) as archive:
        shared_root = ET.fromstring(archive.read("xl/sharedStrings.xml"))
        strings = ["".join(node.itertext()) for node in shared_root.findall("x:si", NS)]
        sheet = ET.fromstring(archive.read("xl/worksheets/sheet1.xml"))
    cases = []
    for row in sheet.findall(".//x:sheetData/x:row", NS):
        row_no = int(row.attrib["r"])
        if row_no < 2:
            continue
        values = {}
        for cell in row.findall("x:c", NS):
            col = re.match(r"[A-Z]+", cell.attrib["r"]).group()
            value = cell.find("x:v", NS)
            if value is None:
                continue
            raw = value.text or ""
            values[col] = strings[int(raw)] if cell.attrib.get("t") == "s" else raw
        question = values.get("A", "").strip()
        target = TARGET.search(values.get("B", ""))
        if question and target:
            cases.append({
                "row": row_no,
                "question": question,
                "target_clause": target.group(1),
                "answer_requirement": values.get("C", ""),
                "old_answer": values.get("D", ""),
                "old_ref_ok": values.get("E", ""),
                "old_answer_ok": values.get("F", ""),
            })
    return cases


def clause_covers(actual: str, target: str) -> bool:
    if actual == target:
        return True
    if "~" not in actual:
        return False
    left, right = actual.split("~", 1)
    try:
        a, b, t = tuple(map(int, left.split("."))), tuple(map(int, right.split("."))), tuple(map(int, target.split(".")))
        return len(a) == len(b) == len(t) and a <= t <= b
    except ValueError:
        return False


def run_case(case: dict, previous: dict | None) -> dict:
    started = perf_counter()
    result = {key: value for key, value in case.items() if key != "old_answer"}
    result["tested_at_utc"] = datetime.now(timezone.utc).isoformat()
    messages = []
    if previous is not None:
        prior_answer = previous["old_answer"]
        if len(prior_answer) > 120:
            prior_answer = prior_answer[:120] + "……（历史回答已归档；回答新问题请重新检索条文）"
        messages.extend([
            {"role": "user", "content": previous["question"]},
            {"role": "assistant", "content": prior_answer},
        ])
    messages.append({"role": "user", "content": case["question"]})
    max_turns = 12 if case["row"] == 57 else 8
    result["max_turns"] = max_turns
    for attempt in range(2):
        try:
            client = PageIndexLocalClient(
                index_model="deepseek/deepseek-chat",
                chat_model="deepseek-chat",
                storage_path=str(ROOT / ".pageindex"),
            )
            terminal = None
            for event in client.responses(messages, doc_id=None, max_turns=max_turns,
                                          instructions=_INSTRUCTION_ZH, stream=True):
                if event.get("type") in ("response.completed", "response.incomplete", "response.failed"):
                    terminal = event
            if terminal is None:
                raise RuntimeError("No terminal response event")
            response = terminal["response"]
            output = response.get("output") or []
            content = output[-1].get("content") or [] if output else []
            answer = "".join(part.get("text", "") for part in content if isinstance(part, dict))
            items = response.get("items") or []
            references = _references_from_items(items)
            tool_calls = []
            for item in items:
                if item.get("type") != "function_call":
                    continue
                try:
                    args = json.loads(item.get("arguments") or "{}")
                except (TypeError, ValueError):
                    args = {}
                tool_calls.append({"name": item.get("name"), "doc_name": args.get("doc_name"), "pages": args.get("pages")})
            result.update({
                "event": terminal["type"],
                "status": response.get("status"),
                "incomplete_details": response.get("incomplete_details"),
                "error": response.get("error"),
                "answer": answer,
                "references": references,
                "tool_calls": tool_calls,
                "usage": response.get("usage"),
                "elapsed_seconds": round(perf_counter() - started, 2),
                "attempts": attempt + 1,
            })
            result["target_in_references"] = any(
                "55031" in str(ref.get("spec_no", "")) and
                clause_covers(str(ref.get("clause_no", "")), case["target_clause"])
                for ref in references
            )
            result["target_in_answer"] = case["target_clause"] in answer
            return result
        except Exception as exc:
            if attempt == 0 and any(term in str(exc).lower() for term in ("connection", "timeout", "temporar", "rate limit")):
                sleep(3)
                continue
            result.update({
                "status": "exception",
                "error": f"{type(exc).__name__}: {exc}",
                "elapsed_seconds": round(perf_counter() - started, 2),
                "attempts": attempt + 1,
                "target_in_references": False,
                "target_in_answer": False,
            })
            return result
    return result


def main() -> None:
    cases = workbook_rows()
    if len(cases) != 81:
        raise RuntimeError(f"Expected 81 questions, found {len(cases)}")
    OUTPUT.mkdir(parents=True, exist_ok=True)
    done = {}
    if RESULTS.exists():
        for line in RESULTS.read_text(encoding="utf-8").splitlines():
            if line.strip():
                item = json.loads(line)
                done[item["row"]] = item
    pending = [case for case in cases if done.get(case["row"], {}).get("status") != "completed"]
    completed = sum(item.get("status") == "completed" for item in done.values())
    print(f"START total={len(cases)} completed={completed} pending={len(pending)} source_sha256={hashlib.sha256(SOURCE.read_bytes()).hexdigest()[:16]}", flush=True)
    if not pending:
        return
    by_row = {case["row"]: case for case in cases}
    with ThreadPoolExecutor(max_workers=3) as pool, RESULTS.open("a", encoding="utf-8") as handle:
        futures = {pool.submit(run_case, case, by_row.get(case["row"] - 1) if case["row"] == 26 else None): case for case in pending}
        for future in as_completed(futures):
            result = future.result()
            handle.write(json.dumps(result, ensure_ascii=False) + "\n")
            handle.flush()
            done[result["row"]] = result
            completed = sum(item.get("status") == "completed" for item in done.values())
            print(f"DONE {completed}/{len(cases)} row={result['row']} status={result['status']} target={result['target_in_references']} sec={result['elapsed_seconds']}", flush=True)


if __name__ == "__main__":
    main()
