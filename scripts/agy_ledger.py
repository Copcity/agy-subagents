#!/usr/bin/env python3
"""Summarize the local AGY delegation ledger (metrics only, no source text).

  python agy_ledger.py              # totals over all runs
  python agy_ledger.py --last 20    # only the most recent 20 runs
  python agy_ledger.py --run-id ID  # one run's full telemetry record
"""

from __future__ import annotations

import argparse
import json
import sys
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import run_cluster  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--ledger-dir", default=None)
    parser.add_argument("--last", type=int, default=0)
    parser.add_argument("--run-id")
    run_cluster.configure_stdio()
    args = parser.parse_args()
    path = Path(args.ledger_dir or run_cluster.DEFAULT_LEDGER_DIR) / "runs.jsonl"
    if not path.is_file():
        print(json.dumps({"runs": 0, "ledger": str(path)}))
        return 0
    records = []
    for line in path.read_text(encoding="utf-8").splitlines():
        try:
            records.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    if args.run_id:
        match = [r for r in records if r.get("run_id") == args.run_id]
        print(json.dumps(match[-1] if match else {"error": "run_id not found"}, ensure_ascii=False, indent=2))
        return 0 if match else 1
    if args.last > 0:
        records = records[-args.last:]
    status = Counter(str(r.get("status")) for r in records)
    def total(section: str, field: str) -> int:
        return sum(int((r.get(section) or {}).get(field, 0) or 0) for r in records)
    summary = {
        "ledger": str(path),
        "runs": len(records),
        "status": dict(status),
        "agy_calls": total("cache", "misses"),
        "cache_hits": total("cache", "hits"),
        "agy_input_tokens": total("usage", "input_tokens"),
        "agy_output_tokens": total("usage", "output_tokens"),
        "agy_total_tokens": total("usage", "total_tokens"),
        "cache_avoided_agy_tokens": total("cache_avoided_usage", "total_tokens"),
        "gpt_input_saved_est": sum(int((r.get("route") or {}).get("estimated_gpt_input_tokens_saved", 0) or 0)
                                   for r in records if r.get("status") in {"success", "partial"}),
        "gpt_returned_tokens_est": total("gpt_context", "returned_tokens_estimated"),
        "truncated_reports": sum(int(r.get("truncated_reports", 0) or 0) for r in records),
    }
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
