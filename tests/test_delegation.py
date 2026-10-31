from __future__ import annotations

import json
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

SCRIPT_DIR = Path(__file__).resolve().parents[1] / "scripts"
sys.path.insert(0, str(SCRIPT_DIR))

import agy_delegate
import agy_mcp_server
import run_cluster

_CACHE_TEMP = tempfile.TemporaryDirectory()


def setUpModule() -> None:
    # Never write test cache entries into the real user profile.
    run_cluster.DEFAULT_CACHE_DIR = Path(_CACHE_TEMP.name)


class DelegationTests(unittest.TestCase):
    def test_short_task_routes_direct_without_resolving_agy(self) -> None:
        result = agy_delegate.delegate(
            "Extract the date.", [], "Return one date.", [], agy="", chunk_chars=32000,
            overlap_chars=400, auto_chunk_chars=32000, max_file_bytes=100000,
            max_output_chars=3000, workers=2, timeout=5, retries=0, transport="auto",
        )
        self.assertEqual(result["status"], "direct")
        self.assertEqual(result["workers"], 0)
        self.assertEqual(result["route"]["decision"], "direct")

    def test_mcp_batch_short_prompt_routes_direct_before_agy_lookup(self) -> None:
        with patch.object(run_cluster, "resolve_agy", side_effect=AssertionError("must not resolve AGY")):
            result = agy_mcp_server.run_tasks(
                [{"id": "short", "prompt": "Summarize this sentence."}],
                {"workers": 2, "result_mode": "compact"},
            )
        self.assertEqual(result["status"], "direct")
        self.assertEqual(result["workers"], 0)
        self.assertEqual(result["results"][0]["status"], "direct")

    def test_high_difficulty_forces_parent_gpt_route(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            source = Path(temp) / "source.txt"
            source.write_text("长" * 80000, encoding="utf-8")
            result = agy_delegate.delegate(
                "Make the final architectural decision.", [str(source)], "Return a concise decision.", [],
                agy="must-not-resolve", chunk_chars=40000, overlap_chars=400,
                auto_chunk_chars=40000, max_file_bytes=300000, max_output_chars=1200,
                workers=2, timeout=5, retries=0, transport="auto", difficulty="high",
                requires_gpt=False,
            )
        self.assertEqual(result["status"], "direct")
        self.assertIn("high reasoning difficulty", result["route"]["reason"])

    def test_mcp_batch_cost_gate_routes_medium_prompt_direct(self) -> None:
        with patch.object(run_cluster, "resolve_agy", side_effect=AssertionError("must not resolve AGY")):
            result = agy_mcp_server.run_tasks(
                [{"id": "medium", "prompt": "x" * 21000}],
                {"workers": 2, "result_mode": "compact"},
            )
        self.assertEqual(result["status"], "direct")
        self.assertEqual(result["results"][0]["status"], "direct")
        self.assertIn("fixed AGY context", result["results"][0]["route"]["reason"])

    def test_delegate_chunks_and_returns_only_reduced_result(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            source = root / "source.txt"
            chunks = [f"FACT-{i:02d}: VALUE-{i:02d}\n" + ("长" * 6000) for i in range(1, 7)]
            source.write_text("\n".join(chunks), encoding="utf-8")
            expected = {f"FACT-{i:02d}" for i in range(1, 7)}

            def fake_run_one(task: dict[str, str], **kwargs: object) -> dict[str, object]:
                prompt = task["prompt"]
                found = sorted(set(__import__("re").findall(r"FACT-\d{2}", prompt)))
                response = {"facts": found, "evidence": found}
                return {
                    "id": task["id"], "status": "success", "response": response,
                    "cache_hit": False,
                }

            with patch.object(run_cluster, "run_one", side_effect=fake_run_one):
                result = agy_delegate.delegate(
                    "Extract every fact id.", [str(source)], "Return JSON with facts and evidence.", [],
                    agy="mock", chunk_chars=20000, overlap_chars=100, auto_chunk_chars=20000,
                    max_file_bytes=200000, max_output_chars=3000, workers=3, timeout=5,
                    retries=0, transport="auto", cache_enabled=False,
                )

            self.assertEqual(result["status"], "success")
            self.assertGreater(result["workers"], 1)
            self.assertEqual(result["failed_workers"], 0)
            self.assertEqual(set(result["result"]["evidence"]), expected)
            self.assertNotIn("reports", result)
            self.assertEqual(result["route"]["decision"], "delegate")
            self.assertGreater(result["route"]["agy_calls"], 2)  # maps + reduce

    def test_cache_hit_skips_process(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            prompt = "stable prompt"
            task = {"id": "a", "prompt": prompt}
            payload = {"status": "SUCCESS", "response": '{"summary":"stable"}'}

            def fake_subprocess(*args: object, **kwargs: object) -> object:
                from subprocess import CompletedProcess
                return CompletedProcess(args[0], 0, json.dumps(payload), "")

            kwargs = dict(
                agy="fake", model="m", effort="high", mode="plan", cwd=None, timeout=1,
                retries=0, transport="argv", argv_limit=10000, result_mode="compact",
                max_response_chars=3000, cache_dir=temp,
            )
            with patch.object(run_cluster.subprocess, "run", side_effect=fake_subprocess) as execute:
                first = run_cluster.run_one(task, **kwargs)
                second = run_cluster.run_one(task, **kwargs)
            self.assertEqual(execute.call_count, 1)
            self.assertFalse(first["cache_hit"])
            self.assertTrue(second["cache_hit"])
            self.assertEqual(second["response"], first["response"])

    def test_reduce_failure_returns_partial_without_raw_reports(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            source = Path(temp) / "source.txt"
            source.write_text("\n".join(f"FACT-{i}: " + ("长" * 10000) for i in range(8)), encoding="utf-8")

            def fake_run_one(task: dict[str, str], **kwargs: object) -> dict[str, object]:
                if task["id"] == "reduce":
                    return {
                        "id": "reduce", "status": "error", "response": "",
                        "error_category": "model_or_protocol_error", "retryable": False,
                        "attempts": 1,
                    }
                return {
                    "id": task["id"], "status": "success", "response": {"ok": True},
                    "cache_hit": False,
                }

            with patch.object(run_cluster, "run_one", side_effect=fake_run_one):
                result = agy_delegate.delegate(
                    "Extract facts.", [str(source)], "Return JSON.", [], agy="mock",
                    chunk_chars=30000, overlap_chars=100, auto_chunk_chars=30000,
                    max_file_bytes=300000, max_output_chars=1200, workers=2, timeout=5,
                    retries=0, transport="auto", cache_enabled=False,
                )
            self.assertEqual(result["status"], "partial")
            # Paid map work is kept as one bounded, clearly labeled result.
            self.assertEqual(result["result_kind"], "unmerged_map_reports")
            self.assertLessEqual(len(json.dumps(result["result"], ensure_ascii=False)), 1200)
            self.assertFalse(result["coverage_complete"])
            self.assertIn("reduce", result["missing_worker_ids"])
            self.assertNotIn("reports", result)
            public = agy_delegate.public_envelope(result)
            self.assertEqual(public["result_kind"], "unmerged_map_reports")
            self.assertFalse(agy_mcp_server.is_error_result(public))

    def test_cache_ttl_zero_disables_cache_reads_and_writes(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            payload = {
                "status": "SUCCESS", "response": "stable",
            }

            def fake_subprocess(*args: object, **kwargs: object) -> object:
                from subprocess import CompletedProcess
                return CompletedProcess(args[0], 0, json.dumps(payload), "")

            kwargs = dict(
                agy="fake", model="m", effort="high", mode="plan", cwd=None, timeout=1,
                retries=0, transport="argv", argv_limit=10000, result_mode="compact",
                max_response_chars=3000, cache_dir=temp, cache_ttl_seconds=0,
            )
            with patch.object(run_cluster.subprocess, "run", side_effect=fake_subprocess) as execute:
                first = run_cluster.run_one({"id": "a", "prompt": "stable"}, **kwargs)
                second = run_cluster.run_one({"id": "a", "prompt": "stable"}, **kwargs)
            self.assertEqual(execute.call_count, 2)
            self.assertFalse(first["cache_hit"])
            self.assertFalse(second["cache_hit"])

    def test_transient_failure_retries_once(self) -> None:
        from subprocess import CompletedProcess

        payload = {"status": "SUCCESS", "response": "recovered"}
        calls = [
            CompletedProcess(["fake"], 1, "", "503 service unavailable"),
            CompletedProcess(["fake"], 0, json.dumps(payload), ""),
        ]

        def fake_subprocess(*args: object, **kwargs: object) -> object:
            return calls.pop(0)

        with patch.object(run_cluster.subprocess, "run", side_effect=fake_subprocess) as execute:
            result = run_cluster.run_one(
                {"id": "retry", "prompt": "retry me"}, agy="fake", model="m", effort="high",
                mode="plan", cwd=None, timeout=1, retries=1, transport="argv", argv_limit=10000,
                result_mode="compact", max_response_chars=3000, cache_dir=None,
            )
        self.assertEqual(execute.call_count, 2)
        self.assertEqual(result["status"], "success")
        self.assertEqual(result["attempts"], 2)

    def test_overall_cancellation_terminates_child_process(self) -> None:
        event = __import__("threading").Event()
        event.set()
        with self.assertRaises(run_cluster.ProcessCancelled):
            run_cluster.execute_process(
                [sys.executable, "-c", "import time; time.sleep(2)"],
                cwd=None, stdin_payload=None, timeout=5, cancel_event=event,
            )

    def test_mcp_batch_deadline_returns_stable_error(self) -> None:
        def slow_run_one(task: dict[str, str], **kwargs: object) -> dict[str, object]:
            time.sleep(0.05)
            return {"id": task["id"], "status": "success", "response": {"ok": True}}

        prompts = [{"id": "slow", "prompt": "x" * 60000}]
        with patch.object(run_cluster, "resolve_agy", return_value="mock"), patch.object(run_cluster, "run_one", side_effect=slow_run_one):
            result = agy_mcp_server.run_tasks(
                prompts,
                {"workers": 1, "result_mode": "compact", "timeout": 1, "total_timeout": 0.01},
            )
        # Every task failed, so nothing is usable: error, not partial.
        self.assertEqual(result["status"], "error")
        self.assertEqual(result["errors"], [{"id": "slow", "category": "overall_timeout", "retryable": False}])


def fact_run_one(task: dict[str, str], **kwargs: object) -> dict[str, object]:
    """Mock worker: echoes FACT ids found in its prompt, honoring the cap."""
    import re
    found = sorted(set(re.findall(r"FACT-\d{2}", task["prompt"])))
    response = {"facts": [{"id": fact, "detail": "d" * 150} for fact in found]}
    budget = int(kwargs.get("max_response_chars", 3000))
    return {
        "id": task["id"], "status": "success",
        "response": run_cluster.compact_value(response, budget),
        "truncated": run_cluster.response_exceeds(response, budget),
        "cache_hit": False,
    }


class HardeningTests(unittest.TestCase):
    def test_gbk_file_is_not_misdecoded_as_utf16(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "gbk.txt"
            path.write_bytes("你好世界，测试".encode("gbk"))
            self.assertEqual(run_cluster.read_text_file(str(path), 1000)[1], "你好世界，测试")
            bom16 = Path(temp) / "u16.txt"
            bom16.write_bytes("中文 text".encode("utf-16"))
            self.assertEqual(run_cluster.read_text_file(str(bom16), 1000)[1], "中文 text")

    def test_binary_file_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "blob.bin"
            path.write_bytes(b"MZ\x00\x01\x02" * 50)
            with self.assertRaisesRegex(ValueError, "binary"):
                run_cluster.read_text_file(str(path), 10000)

    def test_credential_files_are_refused_unless_authorized(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            env = Path(temp) / ".env"
            env.write_text("API_KEY=secret\n" + "x" * 30000, encoding="utf-8")
            kwargs = dict(
                agy="", chunk_chars=0, overlap_chars=800, auto_chunk_chars=40000,
                max_file_bytes=100000, max_output_chars=3000, workers=1, timeout=5,
                retries=0, transport="auto",
            )
            with self.assertRaisesRegex(ValueError, "credential"):
                agy_delegate.delegate("Summarize.", [str(env)], "Short.", [], **kwargs)
            # Explicit authorization passes the guard (and then routes normally).
            result = agy_delegate.delegate("Summarize.", [str(env)], "Short.", [], allow_sensitive=True, **kwargs)
            self.assertIn(result["status"], {"direct", "error"})
            for name in ("server.pem", "id_ed25519", ".env.local"):
                with self.assertRaises(ValueError):
                    run_cluster.check_source_allowed(Path(temp) / name)
            run_cluster.check_source_allowed(Path(temp) / "notes.txt")

    def test_single_job_gets_full_output_budget(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            source = Path(temp) / "source.txt"
            source.write_text("\n".join(f"FACT-{i:02d}: " + ("长" * 3500) for i in range(1, 9)), encoding="utf-8")
            with patch.object(run_cluster, "run_one", side_effect=fact_run_one) as run_one:
                result = agy_delegate.delegate(
                    "Extract facts.", [str(source)], "Return JSON.", [], agy="mock",
                    chunk_chars=0, overlap_chars=800, auto_chunk_chars=40000, max_file_bytes=300000,
                    max_output_chars=3000, workers=1, timeout=5, retries=0, transport="auto",
                    cache_enabled=False,
                )
            self.assertEqual(run_one.call_count, 1)
            self.assertEqual(run_one.call_args.kwargs["max_response_chars"], 3000)
            self.assertIn("at most 3000 characters", run_one.call_args.args[0]["prompt"])
            self.assertEqual(len(result["result"]["facts"]), 8)
            self.assertEqual(result["truncated_reports"], 0)

    def test_truncated_map_reports_are_flagged(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            source = Path(temp) / "source.txt"
            source.write_text("\n".join(f"FACT-{i:02d}: " + ("长" * 2500) for i in range(1, 25)), encoding="utf-8")
            with patch.object(run_cluster, "run_one", side_effect=fact_run_one):
                result = agy_delegate.delegate(
                    "Extract facts.", [str(source)], "Return JSON.", [], agy="mock",
                    chunk_chars=30000, overlap_chars=100, auto_chunk_chars=30000, max_file_bytes=400000,
                    max_output_chars=1200, workers=2, timeout=5, retries=0, transport="auto",
                    cache_enabled=False,
                )
            self.assertGreater(result["truncated_reports"], 0)
            public = agy_delegate.public_envelope(result)
            self.assertEqual(public["truncated_reports"], result["truncated_reports"])

    def test_public_envelope_is_lean(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            source = Path(temp) / "source.txt"
            source.write_text("\n".join(f"FACT-{i:02d}: " + ("长" * 6000) for i in range(1, 7)), encoding="utf-8")
            with patch.object(run_cluster, "run_one", side_effect=fact_run_one):
                full = agy_delegate.delegate(
                    "Extract facts.", [str(source)], "Return JSON.", [], agy="mock",
                    chunk_chars=20000, overlap_chars=100, auto_chunk_chars=20000, max_file_bytes=300000,
                    max_output_chars=3000, workers=2, timeout=5, retries=0, transport="auto",
                    cache_enabled=False,
                )
            public = agy_delegate.public_envelope(full)
            self.assertEqual(set(public), {"status", "result"})
            self.assertEqual(public["result"], full["result"])

    def test_direct_public_envelope_tells_parent_to_handle(self) -> None:
        envelope = agy_delegate.delegate(
            "Extract the date.", [], "Return one date.", [], agy="", chunk_chars=0,
            overlap_chars=800, auto_chunk_chars=40000, max_file_bytes=1000,
            max_output_chars=3000, workers=1, timeout=5, retries=0, transport="auto",
        )
        public = agy_delegate.public_envelope(envelope)
        self.assertEqual(public["action"], "handle_in_parent")
        self.assertLess(len(json.dumps(public, ensure_ascii=False)), 200)

    def test_cache_write_failure_keeps_successful_result(self) -> None:
        from subprocess import CompletedProcess
        payload = {"status": "SUCCESS", "response": "ok"}
        with tempfile.TemporaryDirectory() as temp, \
                patch.object(run_cluster.subprocess, "run", return_value=CompletedProcess(["x"], 0, json.dumps(payload), "")), \
                patch.object(Path, "replace", side_effect=PermissionError("sharing violation")):
            result = run_cluster.run_one(
                {"id": "a", "prompt": "p"}, agy="fake", model="m", effort="high", mode="plan",
                cwd=None, timeout=1, retries=0, transport="argv", argv_limit=10000,
                result_mode="compact", max_response_chars=3000, cache_dir=temp,
            )
            self.assertEqual(result["status"], "success")
            self.assertEqual(list(Path(temp).glob("*.tmp")), [])

    def test_partial_with_deliverable_is_not_mcp_error(self) -> None:
        self.assertFalse(agy_mcp_server.is_error_result({"status": "partial", "result": {"a": 1}}))
        self.assertTrue(agy_mcp_server.is_error_result({"status": "partial", "result": None}))
        self.assertTrue(agy_mcp_server.is_error_result({"status": "error"}))
        self.assertFalse(agy_mcp_server.is_error_result({"status": "direct"}))


class ScenarioRegressionTests(unittest.TestCase):
    """Defects found by the live scenario run."""

    def test_fenced_json_is_parsed_not_kept_as_string(self) -> None:
        fenced = '```json\n{"metrics": {"K1": "7116"}, "ok": true}\n```\n'
        self.assertEqual(run_cluster.compact_response(fenced, 3000), {"metrics": {"K1": "7116"}, "ok": True})
        self.assertEqual(run_cluster.compact_response("```\n[1, 2]\n```", 3000), [1, 2])
        self.assertEqual(run_cluster.compact_response("plain text", 3000), "plain text")

    def test_english_auto_chunks_by_tokens_and_delegates(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            log = Path(temp) / "server.log"
            log.write_text("\n".join(f"2026 INFO request {i} handled normally with no anomaly" for i in range(3600)), encoding="utf-8")
            calls: list[str] = []

            def fake(task: dict[str, str], **kwargs: object) -> dict[str, object]:
                calls.append(task["id"])
                return {"id": task["id"], "status": "success", "response": {"ok": True}, "cache_hit": False}

            with patch.object(run_cluster, "run_one", side_effect=fake):
                result = agy_delegate.delegate(
                    "Count errors.", [str(log)], "JSON.", [], agy="mock", chunk_chars=0,
                    overlap_chars=800, auto_chunk_chars=40000, max_file_bytes=1_000_000,
                    max_output_chars=3000, workers=2, timeout=5, retries=0, transport="auto",
                    cache_enabled=False,
                )
            self.assertEqual(result["route"]["decision"], "delegate")
            self.assertLessEqual(len(calls), 3)
            # An explicit chunk size is honoured as characters.
            explicit = agy_delegate.route_estimate("t", "c", [("f", "a" * 200000)], 3000, 40000, 800, 1)
            self.assertEqual(explicit["agy_calls"], 7)  # 6 maps + reduce

    def test_inline_batch_prompt_routes_direct(self) -> None:
        with patch.object(run_cluster, "resolve_agy", side_effect=AssertionError("must not resolve AGY")):
            result = agy_mcp_server.run_request({"prompts": [{"id": "inline", "prompt": "长" * 30000}]})
        self.assertEqual(result["status"], "direct")
        self.assertIn("already written by GPT", result["results"][0]["reason"])

    def test_file_batch_prompt_still_delegates(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            source = Path(temp) / "doc.txt"
            source.write_text("长" * 30000, encoding="utf-8")
            done = {"id": "doc", "status": "success", "response": {"ok": True}, "cache_hit": False}
            with patch.object(run_cluster, "resolve_agy", return_value="mock"), \
                    patch.object(run_cluster, "run_one", return_value=done):
                result = agy_mcp_server.run_request({"prompts": [{"id": "doc", "instruction": "Summarize.", "file": str(source)}]})
        self.assertEqual(result["status"], "success")
        self.assertEqual(result["results"][0]["response"], {"ok": True})

    def test_process_slot_wait_is_cancellable(self) -> None:
        slots = run_cluster.AGY_PROCESS_SLOTS
        held = 0
        while slots.acquire(blocking=False):
            held += 1
        try:
            event = __import__("threading").Event()
            event.set()
            with self.assertRaises(run_cluster.ProcessCancelled):
                run_cluster.execute_process([sys.executable, "-c", "pass"], cwd=None, stdin_payload=None, timeout=5, cancel_event=event)
        finally:
            for _ in range(held):
                slots.release()

    def test_routing_call_is_not_queued_behind_running_delegations(self) -> None:
        import threading
        release = threading.Event()
        replies: list[dict[str, object]] = []

        def slow_delegate(arguments: dict[str, object]) -> dict[str, object]:
            release.wait(10)
            return {"status": "success", "result": "late"}

        with patch.object(agy_mcp_server, "run_delegate", side_effect=slow_delegate), \
                patch.object(agy_mcp_server, "reply", side_effect=lambda rid, result=None, error=None: replies.append({"id": rid})):
            for rid in range(1, 6):
                agy_mcp_server.handle({"id": rid, "method": "tools/call", "params": {"name": "agy_delegate", "arguments": {}}})
            agy_mcp_server.handle({"id": 99, "method": "ping"})
            started = time.time()
            agy_mcp_server.handle({"id": 100, "method": "tools/call", "params": {"name": "agy_summarize_file", "arguments": {}}})
            while not any(r["id"] == 100 for r in replies) and time.time() - started < 5:
                time.sleep(0.01)
            self.assertTrue(any(r["id"] == 100 for r in replies), "routing reply was queued behind slow calls")
            release.set()
            while len(replies) < 7 and time.time() - started < 10:
                time.sleep(0.01)


class CacheEnvTests(unittest.TestCase):
    def test_env_switch_disables_cache(self) -> None:
        import os
        from subprocess import CompletedProcess
        payload = {"status": "SUCCESS", "response": "ok"}
        kwargs = dict(agy="f", model="m", effort="high", mode="plan", cwd=None, timeout=1, retries=0,
                      transport="argv", argv_limit=10000, result_mode="compact", max_response_chars=3000)
        with tempfile.TemporaryDirectory() as temp,                 patch.dict(os.environ, {"AGY_SUBAGENTS_CACHE": "0"}),                 patch.object(run_cluster.subprocess, "run", return_value=CompletedProcess(["x"], 0, json.dumps(payload), "")) as run:
            run_cluster.run_one({"id": "a", "prompt": "p"}, cache_dir=temp, **kwargs)
            second = run_cluster.run_one({"id": "a", "prompt": "p"}, cache_dir=temp, **kwargs)
            self.assertEqual(run.call_count, 2)
            self.assertFalse(second["cache_hit"])
            self.assertEqual(list(Path(temp).iterdir()), [])


class ParallelTests(unittest.TestCase):
    def test_run_parallel_returns_one_result_per_task_in_order(self) -> None:
        import threading

        def worker(task: dict[str, str]) -> dict[str, object]:
            if task["id"] == "boom":
                raise RuntimeError("x")
            if task["id"] == "slow":
                time.sleep(1.0)
            return {"id": task["id"], "status": "success", "response": task["id"]}

        tasks = [{"id": "a"}, {"id": "boom"}, {"id": "slow"}, {"id": "b"}]
        event = threading.Event()
        results = run_cluster.run_parallel(tasks, worker, workers=4, total_timeout=0.3, cancel_event=event)
        self.assertEqual([r["id"] for r in results], ["a", "boom", "slow", "b"])
        self.assertEqual([r["status"] for r in results], ["success", "error", "error", "success"])
        self.assertEqual(results[1]["error_category"], "worker_error")
        self.assertEqual(results[2]["error_category"], "overall_timeout")
        self.assertTrue(event.is_set())


class NativeLikeTests(unittest.TestCase):
    def test_numbered_lines_and_locator_check(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            source = Path(temp) / "doc.txt"
            lines = [f"第{i}行：" + "长" * 60 for i in range(1, 501)]
            source.write_text(chr(10).join(lines), encoding="utf-8")
            seen: list[str] = []

            def fake(task: dict[str, str], **kwargs: object) -> dict[str, object]:
                seen.append(task["prompt"])
                return {"id": task["id"], "status": "success", "cache_hit": False,
                        "response": {"evidence": ["doc.txt:L12", "doc.txt:L40-L42", "doc.txt:L9999"]}}

            with patch.object(run_cluster, "run_one", side_effect=fake):
                result = agy_delegate.delegate(
                    "Find facts.", [str(source)], "", [], agy="mock", chunk_chars=0, overlap_chars=800,
                    auto_chunk_chars=40000, max_file_bytes=500000, max_output_chars=3000, workers=1,
                    timeout=5, retries=0, transport="auto", cache_enabled=False,
                )
            self.assertIn("L12| 第12行", seen[0])
            self.assertIn("file:L<line>", seen[0])  # default contract asks for locators
            self.assertIn("compact JSON", seen[0])
            self.assertEqual(result["locator_check"], {"locators": 3, "invalid_locators": 1})
            public = agy_delegate.public_envelope(result)
            self.assertEqual(public["invalid_locators"], 1)
            self.assertNotIn("coverage_complete", public)

    def test_short_lines_are_not_numbered(self) -> None:
        self.assertFalse(agy_delegate.use_line_numbers(("a,b" + chr(10)) * 1000))
        self.assertTrue(agy_delegate.use_line_numbers(("x" * 80 + chr(10)) * 10))

    def test_chunked_line_numbers_are_global(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            source = Path(temp) / "doc.txt"
            source.write_text(chr(10).join("长" * 79 for _ in range(1200)), encoding="utf-8")
            prompts: list[str] = []

            def fake(task: dict[str, str], **kwargs: object) -> dict[str, object]:
                prompts.append(task["prompt"])
                return {"id": task["id"], "status": "success", "response": {"ok": 1}, "cache_hit": False}

            with patch.object(run_cluster, "run_one", side_effect=fake):
                result = agy_delegate.delegate(
                    "x", [str(source)], "y", [], agy="mock", chunk_chars=0, overlap_chars=800,
                    auto_chunk_chars=40000, max_file_bytes=500000, max_output_chars=3000, workers=2,
                    timeout=5, retries=0, transport="auto", cache_enabled=False,
                )
            maps = [p for p in prompts if "BEGIN FILE" in p]
            self.assertEqual(len(maps), result["route"]["agy_calls"] - 1)
            self.assertIn("L1200| ", maps[-1])
            self.assertNotIn("L1| ", maps[-1])

    def test_high_difficulty_direct_suggests_extract_then_reason(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            source = Path(temp) / "doc.txt"
            source.write_text("长" * 30000, encoding="utf-8")
            envelope = agy_delegate.delegate(
                "Decide.", [str(source)], "y", [], agy="", chunk_chars=0, overlap_chars=800,
                auto_chunk_chars=40000, max_file_bytes=500000, max_output_chars=3000, workers=1,
                timeout=5, retries=0, transport="auto", difficulty="high",
            )
            public = agy_delegate.public_envelope(envelope)
            self.assertEqual(public["status"], "direct")
            self.assertIn("difficulty=low", public["hint"])

    def test_schema_is_small_and_unknown_args_are_rejected(self) -> None:
        tools = agy_mcp_server.tool_definitions()
        self.assertEqual([t["name"] for t in tools], ["agy_delegate", "agy_subagents"])
        self.assertLess(run_cluster.estimate_tokens(json.dumps(tools, ensure_ascii=False)), 700)
        replies: list[dict[str, object]] = []
        with patch.object(agy_mcp_server, "reply", side_effect=lambda rid, result=None, error=None: replies.append(result)):
            agy_mcp_server.call_tool(1, "agy_delegate", {"task": "x", "chunk_size": 5})
            # Advanced knobs stay accepted even though they are not listed.
            agy_mcp_server.call_tool(2, "agy_delegate", {"task": "x", "cost_mode": "balanced"})
            agy_mcp_server.call_tool(3, "agy_summarize_file", {"path": "missing.txt", "task": "x"})
        self.assertTrue(replies[0]["isError"])
        self.assertIn("chunk_size", replies[0]["content"][0]["text"])
        self.assertEqual(json.loads(replies[1]["content"][0]["text"])["status"], "direct")
        self.assertIn("does not exist", replies[2]["content"][0]["text"])


class McpStdioTests(unittest.TestCase):
    def test_non_ascii_paths_and_output_survive_stdio_under_gbk_locale(self) -> None:
        import os
        import subprocess
        with tempfile.TemporaryDirectory() as temp:
            source = Path(temp) / "中文文件😀.txt"
            source.write_text("短文本", encoding="utf-8")
            requests = [
                {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}},
                {"jsonrpc": "2.0", "id": 2, "method": "tools/call", "params": {"name": "agy_delegate", "arguments": {
                    "task": "提取要点😀", "files": [str(source)], "output_contract": "返回要点"}}},
                {"jsonrpc": "2.0", "id": 3, "method": "tools/call", "params": {"name": "agy_delegate", "arguments": {
                    "task": "x", "files": [str(Path(temp) / "不存在😀.txt")], "output_contract": "y"}}},
            ]
            stdin = "".join(json.dumps(item, ensure_ascii=False) + "\n" for item in requests).encode("utf-8")
            env = {**os.environ, "PYTHONIOENCODING": "", "PYTHONUTF8": "0"}
            completed = subprocess.run(
                [sys.executable, str(SCRIPT_DIR / "agy_mcp_server.py")],
                input=stdin, capture_output=True, timeout=30, env=env,
            )
            replies = {item["id"]: item for item in map(json.loads, completed.stdout.decode("utf-8").splitlines())}
            self.assertEqual(set(replies), {1, 2, 3})
            self.assertNotIn("structuredContent", replies[2]["result"])
            direct = json.loads(replies[2]["result"]["content"][0]["text"])
            self.assertEqual(direct["status"], "direct")
            self.assertEqual(direct["source_chars"], 3)
            missing = json.loads(replies[3]["result"]["content"][0]["text"])
            self.assertIn("不存在😀.txt", missing["error"])


if __name__ == "__main__":
    unittest.main()
