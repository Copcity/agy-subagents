#!/usr/bin/env python3
"""Minimal stdio MCP bridge for the local AGY Gemini subagent cluster.

This uses only the Python standard library so it can be launched by Codex's
MCP manager without a virtualenv. The MCP process stays alive, while each
request keeps AGY task sessions isolated by default. It exposes analysis and
map-reduce tools; the parent GPT remains responsible for mutations and final
decisions.
"""

from __future__ import annotations

import concurrent.futures
import json
import sys
import threading
from argparse import Namespace
from pathlib import Path
from typing import Any

# Import the tested runner next to this file without requiring installation.
SCRIPT_DIR = Path(__file__).resolve().parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))
import run_cluster  # type: ignore  # noqa: E402
import agy_delegate  # type: ignore  # noqa: E402


SERVER_NAME = "agy-subagents"
PROTOCOL_VERSION = "2024-11-05"
DEFAULT_OVERLAP_CHARS = 800
MAX_CONCURRENT_CALLS = 16  # routing is cheap; AGY processes are capped in run_cluster
_write_lock = threading.Lock()
_cancel_lock = threading.Lock()
_active_cancellations: dict[Any, threading.Event] = {}
_cancel_context = threading.local()


def log(message: str) -> None:
    print(f"[{SERVER_NAME}] {message}", file=sys.stderr, flush=True)


def reply(request_id: Any, result: dict[str, Any] | None = None, error: dict[str, Any] | None = None) -> None:
    payload: dict[str, Any] = {"jsonrpc": "2.0", "id": request_id}
    if error is not None:
        payload["error"] = error
    else:
        payload["result"] = result or {}
    line = json.dumps(payload, ensure_ascii=False)
    # Tool calls run on worker threads; one lock keeps JSON-RPC lines whole.
    with _write_lock:
        sys.stdout.write(line + "\n")
        sys.stdout.flush()


# Visible schemas are re-sent to the parent model every turn, so they list
# only what a caller normally sets. Advanced knobs are still accepted (see
# ACCEPTED_ARGS and references/protocol.md) and unknown names are rejected
# server-side, so a typo cannot silently fall back to a default.
DELEGATE_ADVANCED = {
    "chunk_chars", "overlap_chars", "auto_chunk_chars", "workers", "timeout", "total_timeout",
    "retries", "transport", "cost_mode", "cache_enabled", "cache_ttl_days", "max_file_bytes",
    "telemetry", "allow_sensitive", "requires_gpt",
}
ACCEPTED_ARGS = {
    "agy_delegate": {"task", "files", "output_contract", "perspectives", "difficulty", "max_output_chars"} | DELEGATE_ADVANCED,
    "agy_subagents": {
        "prompts", "max_response_chars", "output_file", "workers", "timeout", "total_timeout", "retries",
        "transport", "result_mode", "allow_tools", "auto_chunk_chars", "cost_mode", "cache_enabled",
        "cache_ttl_days", "max_file_bytes", "telemetry", "allow_sensitive",
    },
    # Legacy alias; no longer listed, still dispatched for old prompts.
    "agy_summarize_file": {
        "path", "task", "chunk_chars", "overlap_chars", "max_output_chars", "workers", "timeout",
        "total_timeout", "retries", "max_file_bytes", "difficulty", "requires_gpt", "cost_mode",
        "telemetry", "allow_sensitive", "cache_enabled", "cache_ttl_days",
    },
}


def tool_definitions() -> list[dict[str, Any]]:
    return [
        {
            "name": "agy_delegate",
            "description": (
                "Delegate one low/medium-difficulty task over long local files to an external worker model; you get back "
                "one compact deliverable instead of reading the files. status=direct means no worker ran (short source, "
                "high difficulty, or no saving): do it yourself. Results cite file:L<n>; spot-check key claims. "
                "You keep all decisions and edits."
            ),
            "inputSchema": {
                "type": "object",
                "properties": {
                    "task": {"type": "string"},
                    "files": {"type": "array", "items": {"type": "string"}, "maxItems": 16, "description": "Local text files (read by the bridge)."},
                    "output_contract": {"type": "string", "description": "Output shape/length. Default: conclusion, evidence with locators, uncertainties."},
                    "perspectives": {"type": "array", "items": {"type": "string"}, "maxItems": 8, "description": "Optional reviewer roles, merged internally."},
                    "difficulty": {"type": "string", "enum": ["low", "medium", "high"], "default": "medium"},
                    "max_output_chars": {"type": "integer", "minimum": 500, "default": 3000},
                },
                "required": ["task"],
            },
        },
        {
            "name": "agy_subagents",
            "description": (
                "Advanced: several independent prompts, one compact report each. Prefer agy_delegate. "
                "Pass long sources as {id,instruction,file}; inline long text routes direct."
            ),
            "inputSchema": {
                "type": "object",
                "properties": {
                    "prompts": {"type": "array", "maxItems": 32, "items": {"type": ["string", "object"]}},
                    "max_response_chars": {"type": "integer", "minimum": 500, "default": 3000},
                },
                "required": ["prompts"],
            },
        },
    ]


def run_tasks(
    prompts: list[dict[str, Any]],
    arguments: dict[str, Any],
    cancel_event: threading.Event | None = None,
) -> dict[str, Any]:
    requested_workers = int(arguments.get("workers", 0))
    if requested_workers < 0 or requested_workers > 8:
        raise ValueError("workers must be between 0 and 8; 0 selects an automatic count")
    timeout = float(arguments.get("timeout", 180))
    total_timeout = float(arguments.get("total_timeout", agy_delegate.DEFAULT_TOTAL_TIMEOUT))
    retries = int(arguments.get("retries", 0))
    transport = str(arguments.get("transport", "auto"))
    cwd = arguments.get("cwd")
    if cwd is not None and not isinstance(cwd, str):
        raise ValueError("cwd must be a string when provided")
    result_mode = str(arguments.get("result_mode", "compact"))
    max_response_chars = int(arguments.get("max_response_chars", run_cluster.DEFAULT_MAX_RESPONSE_CHARS))
    allow_tools = bool(arguments.get("allow_tools", False))
    cost_mode = agy_delegate.normalize_cost_mode(arguments.get("cost_mode", "parent_context"))
    cache_enabled = bool(arguments.get("cache_enabled", True))
    cache_ttl_days = int(arguments.get("cache_ttl_days", 7))
    if cache_ttl_days < 0 or cache_ttl_days > 365:
        raise ValueError("cache_ttl_days must be between 0 and 365")
    cache_dir = run_cluster.DEFAULT_CACHE_DIR if cache_enabled and not allow_tools else None
    cache_ttl_seconds = cache_ttl_days * 24 * 60 * 60
    if timeout <= 0 or total_timeout < 0 or retries < 0 or transport not in {"auto", "argv", "stream"}:
        raise ValueError("timeout and total_timeout must be positive, retries non-negative, and transport valid")
    if result_mode not in {"compact", "full"} or max_response_chars < 500:
        raise ValueError("result_mode must be compact or full and max_response_chars must be at least 500")
    direct_results: dict[int, dict[str, Any]] = {}
    eligible: list[tuple[int, dict[str, Any]]] = []
    for index, task in enumerate(prompts):
        prompt_text = task.get("prompt", "")
        route = agy_delegate.route_prompt_estimate(
            prompt_text,
            max_response_chars,
            difficulty=str(task.get("difficulty", "medium")),
            requires_gpt=bool(task.get("requires_gpt", False)),
            cost_mode=cost_mode,
            offloaded_chars=task.get("offloaded_chars"),
            offloaded_tokens=task.get("offloaded_tokens"),
        )
        if route["decision"] == "direct":
            direct_results[index] = {
                "id": task["id"], "status": "direct", "response": None,
                "route": route,
                "estimated_input_tokens": run_cluster.estimate_tokens(prompt_text),
            }
        else:
            eligible.append((index, task))
    if not eligible:
        return {
            "status": "direct", "model": "gemini-3.8-flash-high", "workers": 0,
            "count": len(prompts), "failed": 0,
            "results": [direct_results[index] for index in range(len(prompts))],
            "usage": {}, "cache": {"hits": 0, "misses": 0, "avoided_input_tokens": 0},
            "agy_prompt_tokens_estimated": {"sent_estimated": 0, "avoided_estimated": 0},
            "errors": [], "total_timeout": total_timeout,
        }
    agy = run_cluster.resolve_agy(arguments.get("agy"))
    if not agy:
        raise RuntimeError("agy executable not found on PATH or %LOCALAPPDATA%\\agy\\bin\\agy.exe")
    workers = min(requested_workers or max(1, min(len(eligible), 8)), len(eligible))
    if total_timeout <= 0:
        total_timeout = agy_delegate.auto_total_timeout(len(eligible), workers, timeout)
    common = dict(
        agy=agy,
        model="gemini-3.8-flash-high",
        effort="high",
        mode="plan",
        cwd=cwd,
        timeout=timeout,
        retries=retries,
        transport=transport,
        argv_limit=run_cluster.DEFAULT_ARGV_LIMIT,
        result_mode=result_mode,
        max_response_chars=max_response_chars,
        allow_tools=allow_tools,
        cache_dir=cache_dir,
        cache_ttl_seconds=cache_ttl_seconds,
        cancel_event=cancel_event or threading.Event(),
    )
    ran = run_cluster.run_parallel(
        [task for _, task in eligible],
        lambda task: run_cluster.run_one(task, **common),
        workers=workers,
        total_timeout=total_timeout,
        cancel_event=common["cancel_event"],
    )
    executed = {index: result for (index, _), result in zip(eligible, ran)}
    results = [direct_results[index] if index in direct_results else executed[index] for index in range(len(prompts))]
    failed = sum(result.get("status") not in {"success", "direct"} for result in results)
    actual = [result for result in results if result.get("status") == "success"]
    totals = run_cluster.summarize_runs(results)
    errors = [
        {
            "id": str(result.get("id")),
            "category": str(result.get("error_category") or "worker_error"),
            "retryable": bool(result.get("retryable", False)),
        }
        for result in results
        if result.get("status") == "error"
    ]
    return {
        "status": "success" if failed == 0 else ("error" if not actual else "partial"),
        "model": "gemini-3.8-flash-high",
        "effort": "high",
        "mode": "plan",
        "transport": transport,
        "workers": workers,
        "count": len(results),
        "failed": failed,
        "results": results,
        **totals,
        "errors": errors,
        "total_timeout": total_timeout,
    }


def run_delegate(arguments: dict[str, Any], cancel_event: threading.Event | None = None) -> dict[str, Any]:
    cancel_event = cancel_event or getattr(_cancel_context, "event", None)
    task = arguments.get("task")
    contract = arguments.get("output_contract", "")
    files = arguments.get("files", [])
    perspectives = arguments.get("perspectives", [])
    if not isinstance(task, str) or not isinstance(contract, str):
        raise ValueError("task and output_contract must be strings")
    if not isinstance(files, list) or not all(isinstance(item, str) for item in files):
        raise ValueError("files must be an array of strings")
    if not isinstance(perspectives, list) or not all(isinstance(item, str) for item in perspectives):
        raise ValueError("perspectives must be an array of strings")
    workers = int(arguments.get("workers", 0))
    timeout = float(arguments.get("timeout", 180))
    retries = int(arguments.get("retries", 0))
    max_output_chars = int(arguments.get("max_output_chars", 3000))
    transport = str(arguments.get("transport", "auto"))
    if workers < 0 or workers > 8 or timeout <= 0 or retries < 0 or max_output_chars < 500 or transport not in {"auto", "argv", "stream"}:
        raise ValueError("workers, timeout, retries, max_output_chars, or transport is invalid")
    cache_ttl_days = int(arguments.get("cache_ttl_days", 7))
    if cache_ttl_days < 0 or cache_ttl_days > 365:
        raise ValueError("cache_ttl_days must be between 0 and 365")
    envelope = agy_delegate.delegate(
        task,
        files,
        contract,
        perspectives,
        agy=run_cluster.resolve_agy(None) or "",
        chunk_chars=int(arguments.get("chunk_chars", 0)),
        overlap_chars=int(arguments.get("overlap_chars", DEFAULT_OVERLAP_CHARS)),
        auto_chunk_chars=int(arguments.get("auto_chunk_chars", run_cluster.DEFAULT_AUTO_CHUNK_CHARS)),
        max_file_bytes=int(arguments.get("max_file_bytes", run_cluster.DEFAULT_MAX_FILE_BYTES)),
        max_output_chars=max_output_chars,
        workers=workers,
        timeout=timeout,
        retries=retries,
        transport=transport,
        allow_tools=False,
        cache_enabled=bool(arguments.get("cache_enabled", True)),
        cache_ttl_seconds=cache_ttl_days * 24 * 60 * 60,
        difficulty=str(arguments.get("difficulty", "medium")),
        requires_gpt=bool(arguments.get("requires_gpt", False)),
        total_timeout=float(arguments.get("total_timeout", agy_delegate.DEFAULT_TOTAL_TIMEOUT)),
        cost_mode=str(arguments.get("cost_mode", "parent_context")),
        allow_sensitive=bool(arguments.get("allow_sensitive", False)),
        cancel_event=cancel_event,
    )
    return agy_delegate.public_envelope(envelope, tool="agy_delegate", telemetry=str(arguments.get("telemetry", "summary")))


def run_request(arguments: dict[str, Any], cancel_event: threading.Event | None = None) -> dict[str, Any]:
    cancel_event = cancel_event or getattr(_cancel_context, "event", None)
    prompts_raw = arguments.get("prompts")
    if not isinstance(prompts_raw, list) or not prompts_raw:
        raise ValueError("prompts must be a non-empty array")

    # Reuse the same materialization and validation path as the CLI script.
    args = Namespace(
        prompts_file=None,
        stdin=False,
        prompt=prompts_raw,
        max_file_bytes=int(arguments.get("max_file_bytes", run_cluster.DEFAULT_MAX_FILE_BYTES)),
        auto_chunk_chars=int(arguments.get("auto_chunk_chars", run_cluster.DEFAULT_AUTO_CHUNK_CHARS)),
        allow_sensitive=bool(arguments.get("allow_sensitive", False)),
    )
    prompts = run_cluster.load_prompts(args)
    envelope = run_tasks(prompts, arguments, cancel_event=cancel_event)
    run_id = run_cluster.new_run_id()
    run_cluster.ledger_write({
        "run_id": run_id, "tool": "agy_subagents", "status": envelope["status"],
        "count": envelope["count"], "failed": envelope["failed"], "workers": envelope.get("workers", 0),
        "errors": envelope.get("errors", []), "usage": run_cluster.nonzero(envelope.get("usage")),
        "cache": envelope.get("cache", {}), "cache_avoided_usage": run_cluster.nonzero(envelope.get("cache_avoided_usage")),
        "agy_prompt_tokens_estimated": envelope.get("agy_prompt_tokens_estimated", {}),
        "routes": [item.get("route") for item in envelope["results"] if item.get("route")],
    })
    output_file = arguments.get("output_file")
    if output_file:
        output_path = run_cluster.expand_path(str(output_file))
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_text(json.dumps({**envelope, "run_id": run_id}, ensure_ascii=False) + "\n", encoding="utf-8")
        return {"status": envelope["status"], "output_file": str(output_path), "count": envelope["count"], "failed": envelope["failed"], "run_id": run_id}
    if str(arguments.get("telemetry", "summary")) == "full":
        return {**envelope, "run_id": run_id}
    return lean_batch(envelope, run_id)


def lean_batch(envelope: dict[str, Any], run_id: str) -> dict[str, Any]:
    """Per-result id/status/response plus one token line for the batch."""
    results = []
    for item in envelope["results"]:
        entry: dict[str, Any] = {"id": item.get("id"), "status": item.get("status")}
        if item.get("status") == "success":
            entry["response"] = item.get("response")
            if item.get("truncated"):
                entry["truncated"] = True
        elif item.get("status") == "direct":
            entry["action"] = "handle_in_parent"
            entry["reason"] = (item.get("route") or {}).get("reason")
        else:
            entry["category"] = item.get("error_category") or "worker_error"
            entry["retryable"] = bool(item.get("retryable", False))
        results.append(entry)
    usage = envelope.get("usage") or {}
    cache = envelope.get("cache") or {}
    tokens = {
        key: value
        for key, value in {
            "agy_calls": int(cache.get("misses", 0) or 0),
            "agy_total": int(usage.get("total_tokens", 0) or 0),
            "cache_hits": int(cache.get("hits", 0) or 0),
        }.items()
        if value
    }
    return {"status": envelope["status"], "failed": envelope["failed"], "results": results, "tokens": tokens, "run_id": run_id}


def summarize_file(arguments: dict[str, Any], cancel_event: threading.Event | None = None) -> dict[str, Any]:
    cancel_event = cancel_event or getattr(_cancel_context, "event", None)
    path = arguments.get("path")
    task = arguments.get("task")
    if not isinstance(path, str) or not path.strip() or not isinstance(task, str) or not task.strip():
        raise ValueError("path and task are required")
    chunk_chars = int(arguments.get("chunk_chars", run_cluster.DEFAULT_AUTO_CHUNK_CHARS))
    overlap_chars = int(arguments.get("overlap_chars", DEFAULT_OVERLAP_CHARS))
    if chunk_chars < 1000 or overlap_chars < 0 or overlap_chars >= chunk_chars:
        raise ValueError("chunk_chars must be >= 1000 and overlap_chars must be smaller")
    max_file_bytes = int(arguments.get("max_file_bytes", run_cluster.DEFAULT_MAX_FILE_BYTES))
    result = agy_delegate.delegate(
        task,
        [path],
        "Return a concise evidence-grounded summary with source locations, uncertainties, and items for GPT to verify.",
        [],
        agy=run_cluster.resolve_agy(None) or "",
        chunk_chars=chunk_chars,
        overlap_chars=overlap_chars,
        auto_chunk_chars=chunk_chars,
        max_file_bytes=max_file_bytes,
        max_output_chars=int(arguments.get("max_output_chars", 3000)),
        workers=int(arguments.get("workers", 2)),
        timeout=float(arguments.get("timeout", 180)),
        retries=int(arguments.get("retries", 0)),
        transport="auto",
        cache_enabled=bool(arguments.get("cache_enabled", True)),
        cache_ttl_seconds=max(0, int(arguments.get("cache_ttl_days", 7))) * 24 * 60 * 60,
        difficulty=str(arguments.get("difficulty", "medium")),
        requires_gpt=bool(arguments.get("requires_gpt", False)),
        total_timeout=float(arguments.get("total_timeout", agy_delegate.DEFAULT_TOTAL_TIMEOUT)),
        cost_mode=str(arguments.get("cost_mode", "parent_context")),
        allow_sensitive=bool(arguments.get("allow_sensitive", False)),
        cancel_event=cancel_event,
    )
    public = agy_delegate.public_envelope(result, tool="agy_summarize_file", telemetry=str(arguments.get("telemetry", "summary")))
    if "result" in public:
        public["summary"] = public.pop("result")
    return public


def is_error_result(result: dict[str, Any]) -> bool:
    """Only flag MCP isError when nothing usable came back. A partial result
    that still carries a deliverable should be read, not discarded."""
    status = result.get("status")
    if status == "error":
        return True
    if status != "partial":
        return False
    if result.get("result") is not None or result.get("summary") is not None:
        return False
    if any(item.get("status") == "success" for item in result.get("results", []) or []):
        return False
    return "output_file" not in result


def call_tool(
    request_id: Any,
    name: str,
    arguments: dict[str, Any],
    cancel_event: threading.Event | None = None,
) -> None:
    try:
        if cancel_event is not None:
            _cancel_context.event = cancel_event
        unknown = sorted(set(arguments) - ACCEPTED_ARGS[name])
        if unknown:
            raise ValueError(f"unknown argument(s) for {name}: {', '.join(unknown)}")
        if name == "agy_delegate":
            result = run_delegate(arguments)
        elif name == "agy_summarize_file":
            result = summarize_file(arguments)
        else:
            result = run_request(arguments)
        text = json.dumps(result, ensure_ascii=False, separators=(",", ":"))
        reply(
            request_id,
            {
                "content": [{"type": "text", "text": text}],
                "isError": is_error_result(result),
            },
        )
    except Exception as exc:
        log(f"tool error: {type(exc).__name__}: {exc}")
        reply(
            request_id,
            {
                "content": [{"type": "text", "text": json.dumps({"status": "error", "error": str(exc)}, ensure_ascii=False)}],
                "isError": True,
            },
        )
    finally:
        if hasattr(_cancel_context, "event"):
            del _cancel_context.event
        with _cancel_lock:
            _active_cancellations.pop(request_id, None)


_executor: concurrent.futures.ThreadPoolExecutor | None = None


def handle(request: dict[str, Any]) -> None:
    global _executor
    method = request.get("method")
    request_id = request.get("id")
    if method == "initialize":
        reply(
            request_id,
            {
                "protocolVersion": PROTOCOL_VERSION,
                "capabilities": {"tools": {"listChanged": False}},
                "serverInfo": {"name": SERVER_NAME, "version": "1.1.0"},
                "instructions": "agy_delegate offloads reading of long files; status=direct means do it yourself. You own verification and all edits.",
            },
        )
    elif method == "notifications/cancelled":
        params = request.get("params") or {}
        cancelled_id = params.get("requestId", params.get("request_id"))
        with _cancel_lock:
            event = _active_cancellations.get(cancelled_id)
        if event is not None:
            event.set()
    elif method in {"notifications/initialized", "ping"}:
        if request_id is not None:
            reply(request_id, {})
    elif method == "tools/list":
        reply(request_id, {"tools": tool_definitions()})
    elif method == "tools/call":
        params = request.get("params") or {}
        name = params.get("name")
        if name not in ACCEPTED_ARGS:
            reply(request_id, error={"code": -32602, "message": "unknown tool"})
            return
        arguments = params.get("arguments") or {}
        if not isinstance(arguments, dict):
            reply(request_id, error={"code": -32602, "message": "arguments must be an object"})
            return
        # Long delegations must not block ping/tools/list or a second
        # parallel delegation from the parent agent.
        if _executor is None:
            _executor = concurrent.futures.ThreadPoolExecutor(max_workers=MAX_CONCURRENT_CALLS, thread_name_prefix="agy-call")
        cancel_event = threading.Event()
        with _cancel_lock:
            _active_cancellations[request_id] = cancel_event
        _executor.submit(call_tool, request_id, name, arguments, cancel_event)
    else:
        if request_id is not None:
            reply(request_id, error={"code": -32601, "message": f"method not found: {method}"})


def main() -> int:
    run_cluster.configure_stdio()
    log("started")
    try:
        for line in sys.stdin:
            if not line.strip():
                continue
            try:
                request = json.loads(line)
                if isinstance(request, dict):
                    handle(request)
            except Exception as exc:
                log(f"protocol error: {exc}")
    finally:
        if _executor is not None:
            _executor.shutdown(wait=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
