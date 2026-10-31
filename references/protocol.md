# AGY 子代理：协议、参数与成本模型

只在调参、调试或使用批量工具时读本文件；日常委派看 `SKILL.md` 即可。

## 组成

| 文件 | 作用 |
|---|---|
| `scripts/agy_mcp_server.py` | stdio MCP server（Codex 全局注册名 `agy-subagents`），列出 `agy_delegate`、`agy_subagents` |
| `scripts/agy_delegate.py` | 委派核心：路由估算、读文件、分块、map/reduce、精简返回；也是无 MCP 时的 CLI |
| `scripts/run_cluster.py` | 底层：AGY 进程执行、缓存、重试、并行、账本；也是批量 CLI |
| `scripts/agy_ledger.py` | 汇总本机账本 |

注册（已在本机完成）：`codex mcp add agy-subagents -- <python.exe> "$env:USERPROFILE\.codex\skills\agy-subagents\scripts\agy_mcp_server.py"`；检查：`codex mcp get agy-subagents`。修改脚本后需新开 Codex 会话才会重新加载。

每个 AGY 子调用：`agy --model gemini-3.8-flash-high --effort high --mode plan --sandbox --disable-slash-commands`，短 prompt 用 `--output-format json --print=<prompt>`（必须等号形式），超过 12,000 字符改用 `--output-format stream-json --input-format stream-json --print=` 加一行 NDJSON `{"event":"user","message":{"role":"user","content":"…"}}`。

## 成本模型与路由

每个 AGY 进程自带约 9k token 固定上下文；省下的是主模型上下文，AGY 侧总 token 通常高于主模型直接读原文。`agy_delegate` 读文件后先本地估算，不调用 AGY：

- 无文件、源文少于 20,000 字符、`difficulty=high`、`requires_gpt=true` → `direct`；
- 预计节省的主模型输入 token 不超过 AGY 固定上下文 + 重复指令 + 重叠片段 → `direct`；
- `cost_mode=balanced` 时，AGY 估算总 token 超过直处理的 125% → `direct`（中等长度任务基本都会 direct）；
- 其余委派。单分块单视角只调用 1 次 AGY；多分块或多视角为 map 调用 + 1 次 reduce。

token 估算是字符启发式（非 ASCII 约 0.9 token/字，ASCII 约 0.25 token/字符），不是真实 tokenizer。

主模型侧开销：工具 schema 约 400 token/轮（MCP 加载即付）；SKILL.md 约 1.2k token（技能触发时）；每次委派约 50–150 token 调用参数 + 结果本身 + 约 15–40 token envelope。

## 分块与定位

- `chunk_chars=0`（默认）自动分块：每块 token 量对应 `auto_chunk_chars`（默认 40,000）个中文字符，按源文实际密度换算成字符数（下限 40,000，上限 160,000），英文长文因此也能委派。显式 `chunk_chars` 按字符执行，上限 100,000。
- `overlap_chars` 默认 800。按段落/换行边界切，每块带文件边界标记，文件之间不混块。
- 平均行长 ≥ 40 字符的文件，送给 AGY 前按行编号 `L<n>| `（全文统一编号，跨块不重置），worker 必须以 `<file>:L<n>` 或 `:L<a>-L<b>` 引用证据。桥接检查结果中的定位是否在文件行数范围内，越界数作为 `invalid_locators` 返回。短行文件（CSV、日志）不编号，改用短引文作证据。
- 每个 worker 的 prompt 写明字符上限：单任务等于 `max_output_chars`；扇出时每份 `min(1000, max(600, max_output_chars / 任务数))`。超限被截断的报告计入 `truncated_reports`。模型返回被 ```json 包裹时先去掉再解析。

## `agy_delegate` 参数

可见：`task`（必填）、`files`、`output_contract`、`perspectives`（≤8）、`difficulty`、`max_output_chars`（默认 3000）。

隐藏但可用（不在 schema 中，避免每轮重复付 token；拼错会报 `unknown argument`）：

| 参数 | 默认 | 说明 |
|---|---:|---|
| `chunk_chars` / `overlap_chars` / `auto_chunk_chars` | 0 / 800 / 40000 | 见上 |
| `workers` | 2 | 单次委派内并发，1–8 |
| `timeout` / `total_timeout` | 180 / 240 秒 | 单次尝试 / 整个 map+reduce；总时限到则终止子进程 |
| `retries` | 0 | 只重试超时、网络、429/5xx；权限拒绝、协议错误、空响应不重试 |
| `cost_mode` | `parent_context` | `balanced` 见上 |
| `requires_gpt` | false | 强制 direct |
| `cache_enabled` / `cache_ttl_days` | true / 7 | 见缓存 |
| `max_file_bytes` | 8 MiB | 单文件上限；总计 32 MiB、16 个文件、32 个任务 |
| `telemetry` | `summary` | `full` 返回内部完整 envelope，仅调试 |
| `allow_sensitive` | false | 放行凭据类文件，须用户明确同意 |

CLI 同名参数：`agy_delegate.py --task --file(可重复) --perspective(可重复) --output-contract --difficulty --chunk-chars --cost-mode --no-cache --telemetry --allow-sensitive --output-file …`；`--output-file` 把完整 envelope 落盘，stdout 只打印状态。

## 返回协议

成功：

```json
{"status":"success","result":{...},"tokens":{"agy_calls":3,"agy_total":77446,"gpt_saved_est":52475},"run_id":"20261031-141717-b6b25515"}
```

可选字段只在有信号时出现：`coverage_complete:false`、`truncated_reports`、`invalid_locators`、`failed_workers`+`errors:[{id,category,retryable,attempts}]`、`error`、`result_kind:"unmerged_map_reports"`（归并失败或总时限不够归并时，`result` 为有上限的 `{"unmerged_reports":[...]}`）。

direct：`{"status":"direct","action":"handle_in_parent","reason":"…","source_chars":N,"hint":"…(仅高难度长文)","run_id":"…"}`。

错误类别：`permission_denied`、`timeout`、`overall_timeout`、`transient`、`model_or_protocol_error`、`worker_error`。MCP 只返回 text content（不带 structuredContent，避免同一内容进上下文两次）；`isError` 仅在 `status=error`，或 `partial` 且没有任何可用结果时为 true。CLI 退出码：0 成功或 direct，1 部分/失败，2 输入或环境错误。

## 缓存、账本与隔离

- 缓存：成功的 compact 阶段结果按 SHA-256（prompt、模型、模式、权限、输出预算）存到 `%LOCALAPPDATA%\agy\cache\agy-subagents`，TTL 7 天，过期文件定期清理。命中时 `usage` 为空，`tokens.cache_hits` 计数。源文或任务一变即不命中。写缓存失败不影响结果。
- 账本：每次调用追加一行到 `%LOCALAPPDATA%\agy\telemetry\agy-subagents\runs.jsonl`，只含指标（route 估算、usage、cache、错误、定位检查），不含原文和结果。`AGY_SUBAGENTS_LEDGER_DIR` 改目录，`AGY_SUBAGENTS_LEDGER=0` 关闭；`AGY_SUBAGENTS_CACHE=0` 全局关闭缓存（评测用）。`python scripts/agy_ledger.py [--last N | --run-id ID]`。
- 隔离：每个任务独立 AGY 会话；`--mode plan --sandbox --disable-slash-commands`；map 与 reduce prompt 都禁止工具和外部操作，并声明源文为不可信数据。凭据类文件名/目录默认拒绝；含 NUL 字节的二进制文件拒绝；按 BOM → UTF-8 → GB18030 解码。
- 并发：MCP 工具调用在线程池执行（最多 16 个并发调用），direct 路由不会排在长委派后面；全进程同时运行的 AGY 子进程上限 `AGY_SUBAGENTS_MAX_PROCESSES`（默认 8）。stdio 强制 UTF-8（中文 Windows 默认 GBK）。

## 批量工具 `agy_subagents`（高级）

只在需要多份互相独立的报告时使用。`prompts` 元素可以是字符串，或 `{id, prompt}`、`{id, instruction, file|files, chunk_chars, overlap_chars, difficulty, requires_gpt}`。路由只把桥接从文件读取的内容计为可节省部分；内联长 `prompt` 返回 direct。返回 `{status, failed, results:[{id, status, response | action+reason | category+retryable}], tokens, run_id}`；`output_file` 时完整 envelope 落盘、只返回状态。其他参数：`max_response_chars`、`result_mode`（`full` 返回原始长文，避免使用）、`allow_tools`，以及与上表同名的 `workers`、`timeout`、`total_timeout`、`retries`、`transport`、`cache_*`、`cost_mode`、`telemetry`、`allow_sensitive`。CLI 等价物：`run_cluster.py --prompts-file tasks.json --output-file out.json`。

旧工具名 `agy_summarize_file`（`path`、`task`）仍可调用，等价于单文件 `agy_delegate` 并把 `result` 改名为 `summary`，但已不在工具列表中。

## 实测要点（合成数据，真实 AGY）

- 抽取、多文件对比、跨块计数、注入抵抗、GB18030 中文文件名：均满分；主模型读回 65–284 token，对应原文 2.4 万–8.3 万 token。
- 19 万字符英文日志按错误码精确计数：9 个错 2 个（各差 1）→ 精确计数交给 grep/脚本。
- 缓存命中：0.2 秒、0 次 AGY 调用、结果一致。
- `file:L<n>` 定位：单块 8/8、跨块（9.5 万字、4 次调用）10/10 精确命中原文行号。默认紧凑 JSON 契约使同一结果从 626 降到 262 token。

## 测试与基准

```powershell
python -m unittest discover -s tests -v
python benchmarks/benchmark_delegation.py                    # 离线 mock，无费用
python benchmarks/benchmark_delegation.py --compare <旧副本>  # 前后对比
```

基准经 MCP 层测量主模型读写的 token、每轮 schema、mock AGY 调用和事实覆盖率；token 为字符启发式估算。
