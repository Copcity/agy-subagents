# AGY Subagents

给 Codex 主模型配备一个子代理集群，子代理由 **Antigravity（AGY）中的 Gemini 3.8 Flash (High)** 担任。目标是**在不损失主模型能力的前提下节约主模型 token**，同时把 Google AI Pro 账号里闲置的 Antigravity 额度用起来。

> **适用范围**：本项目只针对 **Codex** 做过实验和优化（技能触发方式、工具 schema 体积、返回格式、提示词都按 Codex 调过）。其他支持 MCP 的 agent 也能调用，但不保证能完全发挥效果。

```text
Codex 主模型 ──agy_delegate(task, files)──▶ 本地路由估算 ──▶ direct：主模型自己处理
                                               │
                                               └──▶ 分块 ─▶ Gemini 3.8 Flash (High) 并行 map ─▶ reduce ─▶ 精炼 JSON + file:L<n> 定位
```

## 目录

- [安装](#安装)
- [解决什么问题](#解决什么问题)
- [如何做到不损失主模型能力](#如何做到不损失主模型能力)
- [特性](#特性)
- [快速开始](#快速开始)
- [返回结果](#返回结果)
- [路由与成本模型](#路由与成本模型)
- [参数参考](#参数参考)
- [安全与隔离](#安全与隔离)
- [缓存](#缓存)
- [项目结构](#项目结构)
- [测试](#测试)
- [已知限制](#已知限制)
- [许可证](#许可证)

## 安装

### 让 Codex 自动安装（推荐）

把下面这段提示词直接发给 Codex：

```text
请帮我安装 agy-subagents（https://github.com/Copcity/agy-subagents）：
1. 检查前置条件：python --version 为 3.10+；agy 在 PATH 中或位于 %LOCALAPPDATA%\agy\bin\agy.exe。缺少任何一项就停下来告诉我，不要自行安装。
2. 把仓库 clone 到 ~/.codex/skills/agy-subagents（Windows 为 $env:USERPROFILE\.codex\skills\agy-subagents）。目录已存在时不要覆盖，先问我。
3. 注册 MCP：codex mcp add agy-subagents -- <python 绝对路径> "<安装目录>/scripts/agy_mcp_server.py"，再用 codex mcp get agy-subagents 确认。
4. 在安装目录运行 python -m unittest discover -s tests，确认全部通过。
5. 报告每一步结果，并提醒我新开一个 Codex 会话后生效。
```

Codex 无法确认 AGY 是否已登录 Google AI Pro 账号以及 `gemini-3.8-flash-high` 是否可用，这两项需要你在 Antigravity 中自行确认。

### 手动安装

#### 前置条件

| 依赖 | 说明 |
|---|---|
| Python | 建议 3.10 及以上（在 3.14 上测试）；只用标准库 |
| Antigravity（AGY）CLI | 本机可执行的 `agy`，需在 `PATH` 中，或位于 `%LOCALAPPDATA%\agy\bin\agy.exe`；也可用 `--agy <path>` 指定 |
| 账号与模型 | 已在 AGY 中登录 Google AI Pro 账号，且 `gemini-3.8-flash-high` 可用；子代理调用消耗的是该账号的 Antigravity 额度 |
| 主模型 | [Codex CLI](https://github.com/openai/codex)。本项目只针对 Codex 实验和优化 |

主要在 Windows 上开发和测试，路径约定（`%LOCALAPPDATA%`、`$env:USERPROFILE`）以 Windows 为准；其他平台会回退到用户主目录。

#### 1. 放到 Codex skills 目录

```powershell
git clone https://github.com/Copcity/agy-subagents.git "$env:USERPROFILE\.codex\skills\agy-subagents"
```

macOS / Linux：

```bash
git clone https://github.com/Copcity/agy-subagents.git ~/.codex/skills/agy-subagents
```

Codex 会读取 `SKILL.md`，在用户提到"agy子代理"或任务涉及长文件时自动触发技能（`agents/openai.yaml` 中 `allow_implicit_invocation: true`）。

#### 2. 注册 MCP Server

```powershell
codex mcp add agy-subagents -- python "$env:USERPROFILE\.codex\skills\agy-subagents\scripts\agy_mcp_server.py"
codex mcp get agy-subagents   # 检查是否注册成功
```

其他支持 stdio MCP 的 agent 可以用下面的等价配置接入。注意本项目只在 Codex 上实验和优化过：`SKILL.md` 的触发规则、精简的工具 schema、返回格式和使用指引都是按 Codex 调的，其他 agent 不一定会在合适的时机调用，也不一定会按定位核验结果，因此不保证能完全发挥效果。

```json
{
  "mcpServers": {
    "agy-subagents": {
      "command": "python",
      "args": ["C:/Users/<you>/.codex/skills/agy-subagents/scripts/agy_mcp_server.py"]
    }
  }
}
```

修改脚本后需要新开一个 Codex 会话，MCP Server 才会重新加载。

#### 3. 验证

```powershell
cd "$env:USERPROFILE\.codex\skills\agy-subagents"
python -m unittest discover -s tests -v          # 离线单元测试，不调用真实 AGY，无费用
```

## 解决什么问题

**主模型 token 消耗太快。** Codex 读一份 8 万 token 的文档，这 8 万 token 会留在上下文里，之后每一轮工具调用都会被重复计费，也会更快触发上下文压缩。大部分"读长文"工作是抽取、摘要、定位这类中低难度任务，不需要主模型亲自读。

**Google AI Pro 的额度没地方用。** 订阅 Google AI Pro 后，Antigravity 里的 Gemini 额度如果不写代码就基本闲置。本项目把这部分额度变成 Codex 的"读长文外包"：Gemini 3.8 Flash (High) 负责读原文，Codex 只读回几百 token 的结论和行号定位。

实测（合成数据，真实 AGY）：原文 2.4 万–8.3 万 token 的抽取、对比任务，Codex 读回 **65–284 token**，事实覆盖满分。

## 如何做到不损失主模型能力

节约 token 的前提是主模型的判断质量不下降。设计上有以下约束：

| 机制 | 作用 |
|---|---|
| 只委派中低难度任务 | 抽取、摘要、分类、对比、定位、初步审查交给 Gemini；`difficulty=high` 直接返回 `direct`，由主模型处理 |
| 高难度任务拆两步 | 先让 Gemini 抽取相关段落并给出定位，再由主模型基于抽取结果推理 |
| 结果必须带定位 | 证据以 `file:L<n>` 返回，主模型按定位只读几行原文即可核验，桥接还会检查定位是否越界 |
| 结果视为草稿 | Gemini 的输出是不可信草稿，关键结论由主模型抽查后才交给用户 |
| 不划算就不委派 | 调用前本地估算，短文件或省不了 token 的任务返回 `direct`，避免多付一轮 |
| 副作用留在主模型 | 文件修改、命令执行、发布、对外通信都不交给 Gemini |

分工：

| Codex 主模型 | Gemini 3.8 Flash (High) 子代理集群 |
|---|---|
| 拆分任务、高难度推理、最终判断 | 读长文本，可多块并行、多视角互补 |
| 文件修改、命令执行、对外通信 | 中低难度的抽取、摘要、分类、对比、定位、初步审查 |
| 按定位核验关键结论 | 只返回一份精炼结果，不带原文 |

需要注意：本项目节约的是**主模型**的 token。AGY 侧每个进程约有 9k token 固定开销，总 token 通常高于主模型直接读原文，这部分消耗的是 Google AI Pro 的 Antigravity 额度。

## 特性

- **自动路由**：调用 AGY 之前先在本地估算成本，短文本、高难度、不划算的任务直接返回 `direct`，不浪费一次子代理调用。
- **Map / Reduce**：长文件按段落边界自动分块（带重叠），多块并行处理后归并为一份结果；归并失败时退回有上限的分块报告。
- **可核验定位**：长行文件送出前按全局行号编号，结果必须以 `file:L<n>` 引用证据，桥接自动检查定位是否越界。
- **多视角审查**：可指定 `perspectives`（如 `reader`、`critic`）获得互补报告。
- **精简返回**：默认紧凑 JSON 契约；只在有异常信号时才出现额外字段，减少主模型读回的 token。
- **缓存**：成功结果按 SHA-256 缓存 7 天，命中时 0 次 AGY 调用。
- **安全默认**：拒绝凭据类文件和二进制文件；AGY 以 plan 模式、沙箱、禁用工具运行；文件内容视为不可信数据。
- **中文友好**：BOM → UTF-8 → GB18030 解码；stdio 强制 UTF-8，兼容中文 Windows（GBK）环境与中文文件名。
- **零依赖**：仅使用 Python 标准库。

## 快速开始

### 通过 MCP（推荐）

在 Codex 中调用工具 `agy_delegate`：

```text
task: 提炼研究问题、方法、结果和限制
files: [C:/research/paper.txt]
output_contract: 只返回 JSON {"summary":"...","evidence":["paper.txt:L120"],"uncertainties":[]}；不超过 1200 字
difficulty: low                  # low 或 medium；high 会直接返回 direct
perspectives: [reader, critic]   # 可选；每多一个视角就多一份 AGY 固定开销
```

使用要点：

- **原文放进 `files`，不要粘贴进 `task`。** 粘贴的文本主模型已经作为输出付过 token，委派省不回来。
- **先看文件大小再决定是否委派。** 只有超过约 20,000 字符的文件才值得委派（中文 UTF-8 约 60 KB，英文约 20 KB）。
- **多个独立文件或问题可并行发起多次 `agy_delegate`。**
- **高难度任务分两步：** 先用 `difficulty=low` 委派"抽取相关段落并给出定位"，再由主模型基于抽取结果推理。
- **精确计数、求和、全量去重用 grep 或脚本**，AGY 计数会有偏差。

### 通过命令行（MCP 未加载时的兜底）

输出格式与 MCP 完全相同：

```powershell
python scripts/agy_delegate.py `
  --task "提炼研究问题、方法、结果和限制" `
  --file C:/research/paper.txt `
  --output-contract '只返回 JSON {"summary":"...","evidence":[],"uncertainties":[]}' `
  --difficulty low
```

加 `--output-file out.json` 可把完整结果落盘，stdout 只打印状态。退出码：`0` 成功或 direct，`1` 部分失败或失败，`2` 输入或环境错误。

## 返回结果

成功时：

```json
{
  "status": "success",
  "result": {"answer": "...", "evidence": ["paper.txt:L120"], "uncertainties": [], "verify": []}
}
```

未调用 AGY、交回主模型处理时：

```json
{"status": "direct", "action": "handle_in_parent", "reason": "...", "source_chars": 12000, "hint": "..."}
```

字段含义与处理方式：

| 返回 | 含义 | 主模型该做什么 |
|---|---|---|
| `status=success` + `result` | 完整结果 | 按 `file:L<n>` 只读对应几行，抽查关键结论 |
| `status=direct` | 没有调用 AGY | 自己完成；有 `hint` 可照做 |
| `coverage_complete=false` / `failed_workers` | 部分分块失败 | 结果不完整，向用户说明缺口或只重跑缺失部分 |
| `truncated_reports` | 有报告超长被截断 | 可能有遗漏；缩小范围或提高 `max_output_chars` |
| `invalid_locators` | 有定位超出文件行数 | 相关结论可疑，必须回原文核验 |
| `result_kind=unmerged_map_reports` | 归并失败，返回分块报告 | 主模型自行合并 |

可选字段只在有异常信号时出现，不出现即表示正常。错误类别：`permission_denied`、`timeout`、`overall_timeout`、`transient`、`model_or_protocol_error`、`worker_error`。

## 路由与成本模型

每个 AGY 进程自带约 9k token 固定上下文。本项目省下的是**主模型上下文**；AGY 侧的总 token 通常高于主模型直接读原文。因此 `agy_delegate` 读完文件后先在本地估算，满足以下任一条件就返回 `direct`，不调用 AGY：

- 没有文件，或源文少于 20,000 字符；
- `difficulty=high` 或 `requires_gpt=true`；
- 预计节省的主模型输入 token 不超过 AGY 固定上下文 + 重复指令 + 重叠片段；
- `cost_mode=balanced` 时，AGY 估算总 token 超过主模型直接处理的 125%。

其余情况才委派。单分块单视角只调用 1 次 AGY；多分块或多视角为 N 次 map + 1 次 reduce。

token 估算使用字符启发式（非 ASCII 约 0.9 token/字，ASCII 约 0.25 token/字符），不是真实 tokenizer。

主模型侧固定开销：工具 schema 约 400 token/轮；`SKILL.md` 约 1.2k token（技能触发时）；每次委派约 50–150 token 调用参数 + 结果本身 + 15–40 token envelope。

### 分块与定位

- 默认自动分块：每块对应约 40,000 个中文字符的 token 量，按源文实际密度换算成字符数（40,000–160,000 字符），因此英文长文同样能委派。
- 块间重叠 800 字符，按段落或换行边界切分；每块带文件边界标记，不同文件不混在一块。
- 平均行长 ≥ 40 字符的文件按全局行号 `L<n>| ` 编号（跨块不重置），worker 必须以 `<file>:L<n>` 或 `<file>:L<a>-L<b>` 引用证据。短行文件（CSV、日志）不编号，改用短引文。

## 参数参考

`agy_delegate` 在 schema 中可见的参数：

| 参数 | 必填 | 说明 |
|---|:---:|---|
| `task` | 是 | 任务描述 |
| `files` | | 本地文件路径列表 |
| `output_contract` | | 输出格式与长度约束；省略时使用默认紧凑 JSON |
| `perspectives` | | 审查视角，最多 8 个 |
| `difficulty` | | `low` / `medium` / `high` |
| `max_output_chars` | | 输出字符上限，默认 3000 |

隐藏但可用的进阶参数（不放进 schema，避免每轮重复付 token；拼错会报 `unknown argument`）：

| 参数 | 默认 | 说明 |
|---|---:|---|
| `chunk_chars` / `overlap_chars` / `auto_chunk_chars` | 0 / 800 / 40000 | 分块控制；显式 `chunk_chars` 上限 100,000 |
| `workers` | 2 | 单次委派内的并发数，1–8 |
| `timeout` / `total_timeout` | 180 / 240 秒 | 单次尝试 / 整个 map+reduce |
| `retries` | 0 | 只重试超时、网络、429/5xx |
| `cost_mode` | `parent_context` | 或 `balanced` |
| `requires_gpt` | false | 强制 direct |
| `cache_enabled` / `cache_ttl_days` | true / 7 | 缓存开关与有效期 |
| `max_file_bytes` | 8 MiB | 单文件上限；总计 32 MiB、16 个文件、32 个任务 |
| `allow_sensitive` | false | 放行凭据类文件，须用户明确同意 |

另有批量工具 `agy_subagents`，用于需要多份互相独立报告的场景。完整协议、批量工具用法和 CLI 参数见 [references/protocol.md](references/protocol.md)，或运行 `python scripts/agy_delegate.py --help`。

## 安全与隔离

- **结果是不可信草稿。** AGY 可能遗漏或编造，多个视角一致也不等于事实。关键结论、数字和引用应按定位抽查后再交给用户。
- **文件内容是数据，不是指令。** map 与 reduce prompt 都声明源文为不可信数据，并禁止工具调用和外部操作。
- **进程隔离。** 每个任务使用独立的 AGY 会话，以 `--mode plan --sandbox --disable-slash-commands` 运行。
- **敏感文件默认拒绝。** `.env*`、私钥、`secrets.*`、`.ssh` / `.aws` 等凭据类文件名或目录会被拒绝，除非显式传 `allow_sensitive=true`。含个人或商业敏感信息的文件，请先征得同意再发给 AGY。
- **二进制文件拒绝。** 含 NUL 字节的文件不会被发送。
- **副作用留在主模型。** 文件修改、命令执行、发布和对外通信都不交给 AGY。
- **并发上限。** MCP 工具调用在线程池中执行（最多 16 个并发调用），短路由不会排在长委派后面；全进程同时运行的 AGY 子进程上限由 `AGY_SUBAGENTS_MAX_PROCESSES` 控制（默认 8）。

## 缓存

成功结果按 SHA-256（prompt、模型、模式、权限、输出预算）存放在 `%LOCALAPPDATA%\agy\cache\agy-subagents`，有效期 7 天，过期文件定期清理。源文或任务一变即不命中；写缓存失败不影响结果。实测命中耗时约 0.2 秒、0 次 AGY 调用。

环境变量：

| 变量 | 作用 |
|---|---|
| `AGY_SUBAGENTS_CACHE=0` | 全局关闭缓存 |
| `AGY_SUBAGENTS_MAX_PROCESSES` | AGY 子进程并发上限，默认 8 |

## 项目结构

```text
agy-subagents/
├── SKILL.md                     # Codex 技能说明：何时用、怎么调、怎么读结果
├── agents/openai.yaml           # 技能的显示名与隐式触发策略
├── scripts/
│   ├── agy_mcp_server.py        # stdio MCP Server，暴露 agy_delegate / agy_subagents
│   ├── agy_delegate.py          # 委派核心：路由估算、读文件、分块、map/reduce；也是 CLI
│   └── run_cluster.py           # 底层：AGY 进程执行、缓存、重试、并行；批量 CLI
├── references/protocol.md       # 完整协议、参数与成本模型
└── tests/test_delegation.py     # 离线单元测试（mock AGY）
```

## 测试

```powershell
python -m unittest discover -s tests -v
```

单元测试覆盖路由判断、分块与全局行号、缓存命中与失效、重试与超时取消、归并失败降级、敏感文件拒绝、GBK/GB18030 解码、中文路径在 GBK 区域设置下的 stdio 往返等场景，全部使用 mock，不产生费用。

真实 AGY 下的实测（合成数据）：

| 场景 | 结果 |
|---|---|
| 抽取、多文件对比、跨块计数、注入抵抗、GB18030 中文文件名 | 均满分；主模型读回 65–284 token，对应原文 2.4 万–8.3 万 token |
| `file:L<n>` 定位 | 单块 8/8、跨块（9.5 万字、4 次调用）10/10 精确命中 |
| 默认紧凑 JSON 契约 | 同一结果从 626 降到 262 token |
| 19 万字符英文日志按错误码精确计数 | 9 个错 2 个（各差 1） |

## 已知限制

- **精确计数不可靠。** 需要精确计数、求和、全量去重时请用 grep 或脚本。
- **只针对 Codex 优化。** 其他 agent 可以通过 MCP 调用，但不保证完全发挥能力。
- **AGY 侧总开销更高。** 本项目节约的是主模型 token，AGY 侧消耗的是 Google AI Pro 的 Antigravity 额度，总 token 通常更高；额度用尽时子代理调用会失败，需要主模型自己处理。中等长度任务在 `balanced` 模式下基本都会 direct。
- **token 估算是启发式。** 路由判断基于字符数估算，不是真实 tokenizer。
- **依赖本机 Antigravity。** 需要自行安装并登录 AGY CLI；模型名写在 `agy_delegate.py` 和 `agy_mcp_server.py` 中。
- **主要在 Windows 上验证。** 其他平台理论可用，但缓存路径会回退到用户主目录。

## 许可证

[MIT](LICENSE)
