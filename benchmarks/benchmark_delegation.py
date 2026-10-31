"""Offline benchmark for the AGY delegation bridge (no model service calls).

Measures what the parent GPT actually pays through the MCP surface:
  * gpt_out  - tokens GPT writes for the tool call arguments
  * gpt_in   - tokens GPT reads back (tool result text), plus the source it
               must read itself when the bridge routes ``direct``
  * schema   - tools/list size, re-sent to GPT every turn; SKILL.md size
  * agy      - mock AGY calls / tokens / cache hits
  * quality  - exact FACT coverage of the returned deliverable

Usage:
  python benchmarks/benchmark_delegation.py                 # current tree
  python benchmarks/benchmark_delegation.py --root DIR      # another copy
  python benchmarks/benchmark_delegation.py --compare DIR   # DIR vs current

The mock AGY honours an "at most N characters" instruction when the prompt
states one (an instruction-following model); otherwise it answers in full and
the bridge's own truncation applies. Token counts use the bridge's character
heuristic, not a real tokenizer.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import tempfile
from pathlib import Path
from subprocess import CompletedProcess
from unittest.mock import patch

DETAIL_CHARS = 150
FACT_RE = re.compile(r"FACT-\d{2}")


def load(root: Path):
    sys.path.insert(0, str(root / "scripts"))
    import agy_mcp_server  # noqa: E402
    import run_cluster  # noqa: E402
    return agy_mcp_server, run_cluster


def make_mock(run_cluster, counter: dict[str, int]):
    def mock_agy(command: list[str], **kwargs: object) -> CompletedProcess[str]:
        counter["calls"] += 1
        prompt = str(kwargs.get("stdin_payload") or kwargs.get("input") or "")
        if not prompt:
            prompt = next((part[8:] for part in command if part.startswith("--print=")), "")
        facts = sorted(set(FACT_RE.findall(prompt)))
        limit = re.search(r"at most (\d+) characters", prompt)
        detail = DETAIL_CHARS
        if limit and facts:
            detail = max(8, min(DETAIL_CHARS, int(limit.group(1)) // len(facts) - 40))
        response = json.dumps({"facts": [{"id": fact, "detail": "d" * detail} for fact in facts]})
        tokens_in = 9000 + run_cluster.estimate_tokens(prompt)
        tokens_out = run_cluster.estimate_tokens(response)
        payload = {"status": "SUCCESS", "response": response, "duration_seconds": 0.01,
                   "usage": {"input_tokens": tokens_in, "output_tokens": tokens_out, "total_tokens": tokens_in + tokens_out}}
        if "stream-json" in command:
            stdout = json.dumps({"event": "result", "result": payload}) + "\n"
        else:
            stdout = json.dumps(payload)
        return CompletedProcess(command, 0, stdout, "")
    return mock_agy


def returned_facts(result_text: str) -> set[str]:
    return set(FACT_RE.findall(result_text))


def run(root: Path) -> dict[str, object]:
    server, run_cluster = load(root)
    est = run_cluster.estimate_tokens
    counter = {"calls": 0}
    report: dict[str, object] = {"root": str(root)}
    with tempfile.TemporaryDirectory() as temp:
        tmp = Path(temp)
        run_cluster.DEFAULT_CACHE_DIR = tmp / "cache"
        if hasattr(run_cluster, "DEFAULT_LEDGER_DIR"):
            run_cluster.DEFAULT_LEDGER_DIR = tmp / "ledger"

        def call(arguments: dict[str, object], source_text: str = "") -> dict[str, object]:
            before = counter["calls"]
            with patch.object(run_cluster, "execute_process", side_effect=make_mock(run_cluster, counter)), \
                    patch.object(run_cluster, "resolve_agy", return_value="mock-agy"):
                result = server.run_delegate(arguments)
            text = json.dumps(result, ensure_ascii=False, separators=(",", ":"))
            direct = result.get("status") == "direct"
            payload = result.get("result")
            payload_text = "" if payload is None else (payload if isinstance(payload, str) else json.dumps(payload, ensure_ascii=False))
            return {
                "status": result.get("status"),
                "gpt_out_tool_call": est(json.dumps(arguments, ensure_ascii=False)),
                "gpt_in_tool_result": est(text),
                "gpt_in_envelope_overhead": est(text) - est(payload_text),
                # On a direct route GPT still has to read the source itself.
                "gpt_in_total": est(text) + (est(source_text) if direct else 0),
                "agy_calls": counter["calls"] - before,
                "facts_returned": len(returned_facts(payload_text)),
            }

        contract = "Return JSON {facts:[{id,detail}]} with every fact id."
        report["schema_tools_list_tokens"] = est(json.dumps(server.tool_definitions(), ensure_ascii=False))
        # Loaded into the parent's context whenever the skill triggers.
        report["skill_md_tokens"] = est((root / "SKILL.md").read_text(encoding="utf-8"))

        # A. short task, no files -> must route direct.
        report["A_short_no_files"] = call({"task": "Extract the date from: shipped 2024-05-01.", "output_contract": "One ISO date."})

        # B. medium ASCII file (~25k chars) -> fixed AGY context beats savings.
        medium = tmp / "medium.txt"
        medium_text = "\n".join(f"line {i}: " + "lorem ipsum " * 18 for i in range(110))
        medium.write_text(medium_text, encoding="utf-8")
        report["B_medium_ascii_25k"] = call({"task": "Summarize.", "files": [str(medium)], "output_contract": "Five bullets."}, medium_text)

        # C. single-job Chinese (~28k chars, one chunk) with 8 facts.
        single = tmp / "single.txt"
        single_text = "\n".join(f"FACT-{i:02d}: " + ("长" * 3500) for i in range(1, 9))
        single.write_text(single_text, encoding="utf-8")
        report["C_single_job_cn_28k"] = {**call({"task": "Extract all fact ids.", "files": [str(single)], "output_contract": contract}, single_text), "facts_expected": 8}

        # D. long Chinese (~56k chars, 2 chunks + reduce), cold then warm cache.
        long_file = tmp / "long.txt"
        long_text = "\n\n".join(f"FACT-{i:02d}: VALUE-{i:02d}\n" + ("长" * 7000) for i in range(1, 9))
        long_file.write_text(long_text, encoding="utf-8")
        args_d = {"task": "Extract all fact ids.", "files": [str(long_file)], "output_contract": contract, "chunk_chars": 30000, "overlap_chars": 200}
        report["D_long_cn_56k_cold"] = {**call(args_d, long_text), "facts_expected": 8}
        report["D_long_cn_56k_warm"] = {**call(args_d, long_text), "facts_expected": 8}
        report["D_baseline_no_agy_gpt_in"] = est(long_text) + est(args_d["task"] + contract)

        # E. long ASCII batch of 24 facts with 3 chunks: map-report truncation.
        many = tmp / "many.txt"
        many_text = "\n".join(f"FACT-{i:02d}: " + ("长" * 2500) for i in range(1, 25))
        many.write_text(many_text, encoding="utf-8")
        report["E_dense_cn_60k_24facts"] = {**call({"task": "Extract all fact ids.", "files": [str(many)], "output_contract": contract, "chunk_chars": 30000, "overlap_chars": 100, "max_output_chars": 1200}, many_text), "facts_expected": 24}
    return report


def compare(baseline: Path, current: Path) -> int:
    def sub(root: Path) -> dict[str, object]:
        env = {**os.environ, "AGY_SUBAGENTS_LEDGER": "0", "PYTHONIOENCODING": "utf-8"}
        out = subprocess.run([sys.executable, __file__, "--root", str(root)], capture_output=True, text=True, encoding="utf-8", env=env, check=True)
        return json.loads(out.stdout)

    before, after = sub(baseline), sub(current)
    rows = []
    for key in after:
        if key == "root":
            continue
        a, b = before.get(key), after[key]
        if isinstance(b, dict):
            for metric in ("status", "gpt_out_tool_call", "gpt_in_tool_result", "gpt_in_envelope_overhead", "gpt_in_total", "agy_calls", "facts_returned", "facts_expected"):
                if metric in b:
                    rows.append((key, metric, (a or {}).get(metric), b[metric]))
        else:
            rows.append((key, "", a, b))
    width = max(len(f"{r[0]}.{r[1]}") for r in rows)
    print(f"{'metric'.ljust(width)}  {'before':>10}  {'after':>10}")
    for scenario, metric, a, b in rows:
        name = f"{scenario}.{metric}" if metric else scenario
        print(f"{name.ljust(width)}  {str(a):>10}  {str(b):>10}")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", type=Path, default=Path(__file__).resolve().parents[1])
    parser.add_argument("--compare", type=Path, help="baseline skill copy to compare against the current tree")
    args = parser.parse_args()
    if args.compare:
        return compare(args.compare.resolve(), Path(__file__).resolve().parents[1])
    report = run(args.root.resolve())
    print(json.dumps(report, ensure_ascii=False, indent=2))
    ok = (
        report["A_short_no_files"]["status"] == "direct"
        and report["D_long_cn_56k_cold"]["facts_returned"] == 8
        and report["D_long_cn_56k_warm"]["agy_calls"] == 0
    )
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
