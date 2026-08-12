<p align="center">
  <img src="docs/images/harness-bench.png" width="780" alt="HarnessBench banner" />
</p>

<h1 align="center">HarnessBench</h1>

<p align="center">
  <b>Benchmark agent / claw frameworks in real filesystem workspaces</b><br/>
  Oracle · usage-proxy trace · rubric · token accounting
</p>

<p align="center">
  <a href="https://www.python.org/downloads/"><img src="https://img.shields.io/badge/python-3.10+-3776AB?style=flat-square&logo=python&logoColor=white" alt="Python 3.10+" /></a>
  <img src="https://img.shields.io/badge/platform-linux%20%7C%20macOS%20%7C%20WSL-lightgrey?style=flat-square" alt="platform" />
  <a href="./tasks/"><img src="https://img.shields.io/static/v1?label=tasks&message=100%2B&color=007acc&style=flat-square" alt="Task count" /></a>
  <img src="https://img.shields.io/badge/contributions-welcome-green?style=flat-square" alt="contributions welcome" />
</p>

---

## HarnessBench · 概要

在项目沙箱（`fixtures` + `sandbox/workspace`）中挂载各适配器运行任务（OpenClaw、PicoClaw、NanoBot、FairyClaw、demo 等），对工作区产出做程序化 **oracle**，对 **usage-proxy** 抓取到的会话 trace 调用 **LLM rubric**，并汇总 **usage/tokens**。适合横向对比框架在读写文件、终端、浏览器等组合场景下的落实情况。

<details>
<summary><b>能力与入口（点击展开）</b></summary>

| 维度 | 说明 |
|------|------|
| **适配器** | `src/harnessbench/adapters/` — `openclaw` · `picoclaw` · `nanobot` · `fairyclaw` · `demo` |
| **过程分** | `tool_use_appropriate` · `consistency` · `robustness` + `security_gate` → `process_effective` |
| **结果分** | 默认仅用 oracle `outcome_score`；**008-image-recognize** / **013-image-edit** 可与 `quality` 按 **`w≈0.9`** 融合 |
| **任务定义** | `tasks/<task_id>/` — `task.yaml` · `prompt.txt` · `fixtures/` · `oracle_grade.py` |
| **配置** | [`config/harness.example.yaml`](config/harness.example.yaml) · [`config/app.yaml`](config/app.yaml) · [`pyproject.toml`](pyproject.toml) |

</details>

### 文档导航

| 区块 | 内容 |
|:--|:--|
| [→ 如何运行](#run-quickstart) | CLI，`run-task` / `run-suite`，续跑区间与配置路径 |
| [→ 评分说明（摘要）](#scoring-summary) | 过程 / 结果分、环境变量、合成公式 |
| [→ 代码主逻辑](#architecture-overview) | `cli` · `runner` · `adapter` |

---

<a id="run-quickstart"></a>

## 如何运行

先进入仓库根目录：

```bash
cd HarnessBench
```

列出全部任务：

```bash
PYTHONPATH=src python3 -m harnessbench.cli tasks
```

运行单个 demo 任务：

```bash
PYTHONPATH=src python3 -m harnessbench.cli run-task \
  --task 001-file \
  --harness demo-local \
  --mode demo
```

运行单个 OpenClaw live 任务：

```bash
PYTHONPATH=src python3 -m harnessbench.cli run-task \
  --task 001-file \
  --harness openclaw-local \
  --mode live
```

等价地可用题号前导数字（须唯一）：`--num 1`。

```bash
PYTHONPATH=src python3 -m harnessbench.cli run-task \
  --num 1 \
  --harness openclaw-local \
  --mode live
```

运行整套 OpenClaw 任务：

```bash
PYTHONPATH=src python3 -m harnessbench.cli run-suite \
  --harness openclaw-local \
  --mode live
```

运行整套 demo 任务：

```bash
PYTHONPATH=src python3 -m harnessbench.cli run-suite \
  --harness demo-local \
  --mode demo
```

从某一题起续跑整套（按 `task_id` 排序后，从该题**含**该题一直跑到结束）：

```bash
PYTHONPATH=src python3 -m harnessbench.cli run-suite \
  --harness moltis-local \
  --mode live \
  --from-task 007-session-memory
```

跑到某一题为止（按 `task_id` 排序后，从第一题**含**起跑到该题**含**止；可单独使用 `--to-task`，不必写 `--from-task`）：

```bash
PYTHONPATH=src python3 -m harnessbench.cli run-suite \
  --harness moltis-local \
  --mode live \
  --to-task 009-git-pr-merge
```

指定闭区间「从 A 跑到 B」（两端均含，且须与排序后的 `task_id` 列表一致）：

```bash
PYTHONPATH=src python3 -m harnessbench.cli run-suite \
  --harness moltis-local \
  --mode live \
  --from-task 007-session-memory \
  --to-task 009-git-pr-merge
```

按题号前导数字筛选（例如第 1～76 题，**不依赖** `001-` 这种补零命名；勿与 `--from-task` / `--to-task` 同时使用）：

```bash
PYTHONPATH=src python3 -m harnessbench.cli run-suite \
  --harness moltis-local \
  --mode live \
  --from-num 1 \
  --to-num 76
```

仅写 `--to-num 76` 时默认从第 1 题起；仅写 `--from-num 50` 时默认跑到题库中最大题号。

模型配置在 `config/harness.example.yaml`（可复制为 **`config/harness.yaml`**；仍兼容旧路径 **`config/models.yaml`**）；CLI **`--harness`** 参数对应 YAML 里 **`models:`** 下的 **条目名**（与示例 `harness.example.yaml` 结构一致）。环境变量 **`HARNESSBENCH_HARNESS_CONFIG`** 可指向自定义路径（沿用 **`HARNESSBENCH_MODELS_CONFIG`** 亦可）。
OpenClaw 标准源配置在 `config/openclaw.json`。
PicoClaw 标准源配置在 `config/picoclaw.json`。
FairyClaw：将 `adapter` 设为 `fairyclaw`，`user_config` 指向 FairyClaw 的 **`config` 目录**（内含 `llm_endpoints.yaml` 等）。适配器在沙箱内合并完 usage-proxy 的 `llm_endpoints` 后，在子进程执行 **`fairyclaw agent ...`**（单进程、无需另起 `fairyclaw start`）。`bench_idle_seconds` / `bench_min_wait_after_send` 等可写在 harness 配置中（见 `config/harness.example.yaml` 的 `fairyclaw-local`）。


应用路径等在 `config/app.yaml`（`results_dir`、`work_root` 等按你的部署调整；示例可为 `data_/results` 与 `data_/sandbox`）。
环境变量在 `config/bench.env`。

`run-task` / `run-suite` 会在终端打印进度与每题耗时；`run-task` 与 `run-suite` 输出的 JSON 里含 **`elapsed_sec`**（秒）。

运行结果会写到 **`config/app.yaml` 中的 `results_dir`**（例如 **`data_/results/<model_id>/<api_model_slug>/<task_id>.json`**，视配置而定）。
沙箱会写到 **`config/app.yaml` 中的 `work_root`**，目录结构与结果目录类似：**`data_/sandbox/<model_id>/<api_model_slug>/oc-bench-v2-<task_id>-<api_model_slug>-<timestamp>-<uuid>/`**。
落盘 JSON 中 **`adapter_result.stdout`** 为 **`usage-proxy` 抽取后的 trace**（与 `extract_proxy_trace` 一致），不再保存适配器原始终端长日志。

如果框架请求会经过 benchmark 的 usage proxy，或能读到 session 日志，结果 JSON 里会附带 `usage_summary`，包含：

- `input_tokens`
- `output_tokens`
- `cache_read_tokens`
- `cache_write_tokens`
- `total_tokens`

<a id="scoring-summary"></a>

## 评分说明（摘要）

汇总公式：**`combined_score = outcome_effective × process_effective × security_score`**。**`process_effective`** 与 **`security_score`** 来自 **proxy trace** 上的 **`rubric_llm`**；**`outcome_effective`** 通常为 oracle **`outcome_score`**；仅 **08** / **13** 默认以 **`w=0.9`** 与 oracle 侧 **`quality`** 融合。**Proxy trace rubric 的 JSON 不要、也不解析顶层的 `quality` 键**——交付语义分只在 **oracle** / **`oracle_quality_layer`** 路径产出。

### 过程分与安全分（proxy trace）

- **抽取**：`usage-proxy` → `extract_proxy_trace_incremental` → 拼入各任务 `tasks/<task_id>/llm_rubric.py`（无则 `grading/default_rubric.py`），再调用 `src/harnessbench/grading/rubric_llm.py`（OpenAI 兼容 Chat Completions；凭证可用 OpenClaw 配置、`RUBRIC_API_KEY`、`RUBRIC_BASE_URL`、`RUBRIC_MODEL` 等）。
- **输出**：`scores` 中三维——**tool_use_appropriate**、**consistency**（与旧键 `flow_coherence` 同义）、**robustness**（与旧键 `error_handling` 同义）；顶层 **`security_gate`** → 映射为 **`security_score`**（0/1 门控，缺省时多为 1）。**`process_effective` = 三维算术平均**（harness 会重算均值，覆盖模型可选的 `total`）。
- **工作区带进过程分上下文**：当本题 **`oracle_result["outcome_llm_weight"] > 0`**（见下「结果分」权重；默认仅 **08** / **13** 为 **0.9**）时，将 **`sandbox/workspace/out/`** 下可读文本节选（如 txt/md/json/csv/html 等，有字数与文件数上限）追加到本条 rubric 的 user message；**`w == 0`** 的题目不追加。**013-image-edit** 另经由 **`build_rubric_user_content_for_task`** 附 **`out/cat_styled.png`**、**`out/cat_scene.png`**（与上述文本节选机制独立）。
- **`HARNESSBENCH_SKIP_PROCESS_GRADE=1`**：跳过过程分 LLM（见 **`process_grade.compute_scoring`**）。

### 结果分（oracle + quality）

- **`oracle_grade.score_workspace`** 返回 **`outcome_score`**（多为程序化校验），可选 **`quality`**、**`quality_rubric_meta`**。
- **融合**：**合成 outcome** = **`(1 - w) × outcome_score + w × quality`**，其中 **`w`** = **`outcome_llm_weight`**：
  1. 若 oracle 返回值中已有 **`outcome_llm_weight`**，优先使用；
  2. 否则：**`tasks.run_oracle`** 在 **`oracle_grade`** 之后会走 **`merge_oracle_quality`**，按 **`src/harnessbench/grading/task_outcome_llm_weights.py`** 写入默认 **`w`**：**仅 `008-image-recognize`、`013-image-edit` 为 0.9**；**其余题目默认为 0**（结果分只看 oracle **`outcome_score`**，不跑通用 **`quality`** 融合）。
  3. 若结果中仍无该键（如 oracle 早退报错），回退 **`HARNESSBENCH_OUTCOME_LLM_WEIGHT`**（默认 **0.25**）。
- **`quality`** 来源：**08** / **13** 在各自 **`oracle_grade`** 内用多模态 **`rubric_llm`**。若将来某题 **`w > 0`** 且尚无 **`quality`**，**`oracle_quality_layer`** 可调用通用文本 **`quality`** LLM。**`HARNESSBENCH_SKIP_ORACLE_QUALITY_LLM=1`** 跳过该 LLM。**`w = 0`** 或未得到 **`quality`** 时，合成 outcome 退化为仅用 **`outcome_score`**。
- 当既没有可用的合成 **outcome** 也没有 **`quality`** 时，**`combined_score`** 中取 **`outcome_effective = 1`**（见 **`compute_scoring`**）。

### 其它

- **Oracle** 单次校验失败通常**不会**中止整场 suite，多体现在较低的 **`outcome_score`** / checks；**`hooks`** 抛错或适配器崩溃等仍可能中止。

<a id="architecture-overview"></a>

## 代码主逻辑

主流程分三部分：

1. `cli`
   入口在 `src/harnessbench/cli.py`，负责解析命令、加载任务和模型配置，并调用 runner。

2. `runner`
   核心逻辑在 `src/harnessbench/runner.py`：
   - 创建本次任务的 `sandbox`（位于 `work_root/<model_id>/<api_model_slug>/...`）
   - 在 `sandbox/workspace` 中复制任务 `fixtures`
   - 渲染 prompt
   - 调用 task hooks （据题目而异，有些题目会有对应任务）
   - 调用 adapter 执行目标框架
   - 跑 oracle：`tasks.run_oracle`（`oracle_grade.score_workspace` → `merge_oracle_quality`：写入 `outcome_llm_weight`；仅 **`w > 0`** 且尚无 **`quality`** 时才可能跑通用文本 `quality` LLM，默认仅 **08** / **13** 的 **`w` 为 0.9**）
   - 过程分：`usage-proxy` → `extract_proxy_trace` → 若 `oracle_result.outcome_llm_weight > 0` 则拼 `workspace/out/` 文本节选 → `rubric_llm`（见 `src/harnessbench/grading/`）
   - 汇总结果并写入结果 JSON（`adapter_result.stdout` 写入抽取后的 trace JSON 字符串）
   - 如果能读到框架 session 日志或 usage proxy 记录，就额外统计 `usage_summary`

3. `adapter`
   adapter 在 `src/harnessbench/adapters/`：
   - `openclaw.py`：调用 OpenClaw
   - `picoclaw.py`：调用 PicoClaw
   - `nanobot.py`：调用 NanoBot
   - `nanoclaw.py`：调用 NanoClaw
   - `demo.py`：本地假实现，用于自测 benchmark 流程

任务定义在 `tasks/`。
每个任务通常包含：

- `task.yaml`
- `prompt.txt` 或多轮 `prompt_files`
- `fixtures/`
- `oracle_grade.py`（程序化 `outcome_score`；**08** / **13** 在模块内可调 `rubric_llm` 产出 `quality` 并与 **`outcome_score`** 按 **`w=0.9`** 融合；其余题默认 **`w=0`**，结果分仅以 oracle **`outcome_score`** 为准）
- 可选 `hooks.py`

OpenClaw 运行时会使用三层路径：

- 源配置：`config/openclaw.json`
- sandbox 内源副本：`openclaw_src.json`
- sandbox 内最终运行配置：`openclaw.json`

工作区相关路径：

- `sandbox`：本次任务的临时目录
- `workspace`：`sandbox/workspace`，agent 真正读写任务文件的目录
- `state_dir`：OpenClaw 的状态目录，默认放在 `sandbox/.openclaw`
