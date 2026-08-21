# Writer Harness + Director Harness + OpenHarness

本仓库是一个面向 Agent 执行链路的开发与评测工作区，核心问题是：在真实工具执行前增加结构化规划与审查，在每次工具调用前增加运行时保障，是否能够改善任务完成质量、可审计性和安全性，以及这些收益是否值得额外的 token 与时延。

项目由三层组成：

- **Writer Harness**：让 Actor 先产出结构化执行剧本，再进行规则完整性检查和 LLM 充分性评审；必要时要求 Actor 重新生成剧本。
- **Director Harness**：在 OpenHarness 真正调用工具前检查工具注册、参数和运行时能力；未知工具只能从人工备案的 MCP 目录中寻找候选并临时接入。
- **OpenHarness**：负责 Agent 循环、模型会话、权限控制和真实工具执行，也是两个 benchmark 的统一执行后端。

当前正式实验范围为 HarnessBench 全部 106 题，以及 MCP-Persona 173 题中的固定 Verified52 子集。每个正式实验臂运行两轮，因此分别对应 212 和 104 个任务槽位。所有成绩均为 **OpenHarness-compatible 本地结果**，不等同于 benchmark 官方榜单成绩。

## 实验模式

| 模式 | 执行路径 | 用途 |
|---|---|---|
| `original` / `vanilla` | 用户任务直接进入 OpenHarness | Original 对照组 |
| `writer_harness` | Actor 生成剧本 -> Writer 审查 -> OpenHarness 执行 | Writer 实验组 |
| `writer_director` | Writer 流程 -> Director 逐次预检 -> OpenHarness 工具执行 | Writer + Director 实验组 |

`vanilla` 是在线 CLI/UI 使用的名称，`original` 是正式 benchmark 配置中的名称；两者都表示不经过 Writer。Director 是可选的运行时层，不替代 OpenHarness 原有的 hook、schema 校验和权限系统。

## 当前实验状态

当前工作区附带的历史归档覆盖 Original 和 Writer 两个实验臂：HarnessBench 均完成 `106 x 2`，MCP-Persona 均完成 Verified52 `52 x 2`，Actor 与 Writer 使用 `qwen3.6-plus`。这些结果可用于描述性比较和逐题分析，但历史 Original 运行的 runner SHA 与其记录的 Git commit 不一致，因此不能据此得出严格配对的因果结论。

小规模同模型领域验证尚未确认 Writer 的稳定优势，只有垂直专业工作流呈方向性正增益且置信区间仍跨越 0。因此 Writer v1 当前不是全局硬门；是否启用应继续依据任务类型、额外时延和 token 成本评估。Writer + Director 的集成、离线预检和两轮全量入口已经就绪，但其结果不属于现有 `results/full/` 中的 Original / Writer 历史归档，新运行默认进入 `results/runs/writer-director-full/`。

## 核心链路

```text
用户任务
  |
  +-- Original / vanilla -----------------------------------+
  |                                                          |
  +-- writer_harness                                         |
       -> Actor 生成结构化执行剧本                            |
       -> Writer 规则完整性检查 + LLM 充分性评审              |
       -> 不完整时由 Actor 最多再生成一次                      |
       -> final_script_report / final_scripts ---------------+
                                                                  |
                                                                  v
                                                        OpenHarness Agent
                                                                  |
                                                     每次模型工具调用
                                                                  |
                                           hook / schema -> Director（可选）
                                                                  |
                                                  OpenHarness 权限检查
                                                                  |
                                                      真实工具执行与回传
                                                                  |
                                      benchmark Oracle / 过程 / 安全 / 语义评分
                                                                  |
                                                          results/ 结构化产物
```

Writer 剧本的主要字段为：

- `task_profile`：任务类型、目标、成功标准和预期交付物；
- `difficulty_profile`：难度、已知/未知条件、可用能力、缺失能力及解决策略；
- `execution_plan`：执行前检查、推荐步骤和验证步骤；
- `difficulty_judgment`、`judgment_rationale`、`execution_suggestion`：难度结论和执行建议。

Writer 同时保留 `online_completeness_judgment` 和 `judge_completeness_evaluation`。当前 v1 的执行语义不是“低分一律阻断”：剧本结构完整且有有效总分时，`>85` 直接执行、`70-85` 谨慎执行、`<70` 按低分谨慎策略执行；只有缺少可执行剧本或有效评分时才阻断。需要只观察剧本阶段时，应显式使用 `--skip-execute`。

Director 的行为边界如下：

- 已注册工具先通过 OpenHarness 的参数模型校验，再由 Director 放行；同名工具在当前会话内缓存检查结果，真实调用报错后缓存失效。
- 未注册工具只从 `director_harness/director_mcp_catalog.json` 的人工备案候选中匹配，不进行开放式在线搜索。
- 候选 MCP 只写入当前进程的内存配置；连接、`tools/list` 和工具注册成功后才能替换原调用，不持久化修改用户配置。
- Director 事件可进入 `stream-json`，也可追加到脱敏后的 JSONL 日志；OpenHarness 的权限检查仍位于真实副作用之前。

## 目录结构

| 路径 | 职责 |
|---|---|
| `writer_harness/` | 剧本模板、Actor 适配、完整性判断、充分性评审和能力匹配 |
| `director_harness/` | 工具预检、备案 MCP 匹配/临时注册、事件与脱敏日志 |
| `OpenHarness/` | Agent 运行时，以及 Writer/Director 的集成、正式运行器和评分脚本 |
| `writer_excute.py` | 单轮在线入口；支持 Writer、真实执行、工具轨迹和 Director |
| `writer_excute_multiturn.py` | UI 友好的多轮入口；负责计划继承/修订判断和本地会话状态 |
| `writer_harness_ui/` | React + Vite 对比界面，展示 Original、Writer、Director 事件和工具轨迹 |
| `HarnessBench/` | 106 个真实工作区任务、fixtures、hooks、Oracle 和过程评分 |
| `MCP-Persona/` | 173 个发布任务、模拟 MCP 服务及官方评估代码；正式实验使用 Verified52 |
| `results/config/` | 冻结任务清单、Verified52 rehearsal spec 和领域验证配置 |
| `results/full/` | 已归档的 Original / Writer 两轮全量结果 |
| `results/partly/` | 小规模配对实验和领域验证结果 |
| `results/runs/` | 新运行、断点续跑和 Writer + Director 全量结果的默认位置 |
| `docs/writer_director_0812.zip` | 正式实验用于校验 Writer/Director 源码一致性的参考归档 |
| `tau3-bench/` | 随工作区保留的独立 benchmark 源码；当前正式全量脚本不调用它 |

`docs/`、`results/`、运行日志和多轮 session 都是本地工作区数据，默认被 Git 忽略。正式复跑前必须确认参考归档、冻结配置和基准数据已经随实验材料准备好。

## 环境准备

基础要求：Python 3.10+；推荐使用 `uv` 管理 OpenHarness 环境。只有运行 Web UI 时才需要 Node.js 和 npm。

```bash
cd OpenHarness
uv sync --extra dev
cd ..
source OpenHarness/.venv/bin/activate
```

也可以使用根目录的 `requirements.txt` 创建轻量在线运行环境，但正式 benchmark 脚本固定从 `OpenHarness/.venv/bin/python` 启动。

复制环境变量模板并填写真实配置：

```bash
cp .env.example .env
```

变量按使用场景分组：

| 变量 | 用途 |
|---|---|
| `OPENAI_API_BASE`、`OPENAI_API_KEY` | 所有正式 benchmark，以及 Web UI 发起的单轮/多轮 Actor、Writer 与评分模型；切换 API 工作空间只需修改这两项 |
| `WRITER_MODEL` | 单轮、多轮和 Web UI 的 Writer 模型；`WRITER_BASE_URL`、`WRITER_API_KEY` 仅作旧入口兼容 |
| `ACTOR_MODEL`、`ACTOR_API_FORMAT` | 在线入口中的 OpenHarness Actor；`ACTOR_BASE_URL`、`ACTOR_API_KEY` 仅作旧入口兼容 |
| `OH_BIN`、`OPENHARNESS_SRC` | OpenHarness CLI 和本仓库源码路径 |

正式实验脚本和 Web UI 会直接读取根目录 `.env`。为了保持 Writer source-lock，裸 Python CLI 仍沿用 `WRITER_*`、`ACTOR_*` 兼容变量；需要直接调用时可先使用 `.env.example` 中的别名导出方式。

## 在线运行

### 单轮 Writer + Director

下面的命令会调用真实模型并允许 OpenHarness 执行工具。建议先在隔离工作区中运行，并确认 OpenHarness 权限配置。

```bash
python writer_excute.py \
  --query "检查当前项目并给出可验证的改进建议" \
  --mode writer_harness \
  --writer-model "$WRITER_MODEL" \
  --writer-base-url "$WRITER_BASE_URL" \
  --writer-api-key "$WRITER_API_KEY" \
  --actor-model "$ACTOR_MODEL" \
  --actor-base-url "$ACTOR_BASE_URL" \
  --actor-api-key "$ACTOR_API_KEY" \
  --actor-api-format "$ACTOR_API_FORMAT" \
  --oh-bin "$OH_BIN" \
  --openharness-src "$OPENHARNESS_SRC" \
  --director-harness-enabled \
  --oh-real-run \
  --execute-output-format stream-json \
  --json
```

去掉 `--director-harness-enabled` 即为 Writer-only；增加 `--skip-execute` 可完成剧本生成和评审，但不进入后续业务执行阶段。仓库也提供了 Writer-only 示例：

```bash
bash examples/run_writer_harness.sh
```

未指定 `--oh-real-run` 时，底层 OpenHarness 使用 dry-run，只预览配置和能力，不应把它当作真实任务结果。

### 多轮会话

```bash
python writer_excute_multiturn.py \
  --query "沿用上一轮计划，先完成其中的检查步骤" \
  --session-id demo-session \
  --mode writer_harness \
  --director-harness-enabled \
  --oh-real-run \
  --execute-output-format stream-json \
  --json
```

多轮入口会在 `.writer_harness_sessions/` 保存本地 JSON，并对本轮执行选择 `inherit_previous_plan`、`refine_plan`、`split_plan`、`continue_plan` 或 `new_plan`。它通过摘要化历史和上一轮剧本构造新请求，并不是 OpenHarness 原生 `--continue` / `--resume` 会话。可用 `--reset-session` 清空指定 session，或用 `--delete-turn-id` 删除单轮记录。

### Web UI

```bash
cd writer_harness_ui
npm ci
npm run dev
```

访问 <http://127.0.0.1:8090>。开发服务器内置本地 API middleware，会从项目根目录启动 `writer_excute.py` 或 `writer_excute_multiturn.py`。UI 可并排比较 Original 与 Writer，切换 Director，查看剧本评分、工具序列和 Director 事件。

`npm run build` 只构建静态前端，不包含 Python 执行后端；完整交互应通过当前 Vite 开发服务器，或由部署方实现等价 API。

## 正式实验

正式脚本固定任务清单、两轮重复、模型、temperature、seed、工具范围、超时和评分口径。默认并发为 1，并支持在兼容的 `run-config.json` 上续跑。

### 离线预检

Writer + Director 全量运行前，先执行不调用外部模型的预检：

```bash
bash OpenHarness/scripts/run_writer_director_full.sh \
  --check-only \
  "$PWD" \
  "$PWD/.env" \
  "$PWD/results/runs/writer-director-full/preflight"
```

预检会验证 OpenHarness 虚拟环境、两个 benchmark、非空凭证配置、106 题冻结清单、Verified52 协议、Writer/Director 与参考归档的字节一致性、MCP 目录格式，以及已有输出是否可安全续跑。

### 三个实验臂

```bash
# Original：HarnessBench 106x2 + MCP-Persona Verified52x2
bash OpenHarness/scripts/run_original_full.sh \
  "$PWD" "$PWD/.env" "$PWD/results/runs/original-full"

# Writer：分别完成两次单轮运行，合并后统一评分
bash OpenHarness/scripts/run_writer_full.sh \
  "$PWD" "$PWD/.env" "$PWD/results/runs/writer-full"

# Writer + Director：两个 benchmark 均直接运行两轮并统一评分
bash OpenHarness/scripts/run_writer_director_full.sh \
  "$PWD" "$PWD/.env" "$PWD/results/runs/writer-director-full/run-001"
```

上述脚本是全量实验的正式入口。`OpenHarness/scripts/` 中其余 runner、repeat 合并器、语义复核器和汇总器属于下层实现，适合断点恢复或重算，不应随意混用不同实验臂的输出目录。

## 评分与产物

### HarnessBench

- `outcome_score`：任务 Oracle 对工作区交付物的程序化结果评分；
- `process_effective`：基于 trace 的工具适配性、一致性和鲁棒性评分；
- `security_score`：二元安全门；
- `combined_score = outcome_effective × process_effective × security_score`；
- 图像识别与图像编辑任务可按各自配置融合多模态 quality，其余任务默认以 Oracle outcome 为主。

### MCP-Persona Verified52

- execution 与 expected-tool recall；
- 工具顺序、checkpoint 语义复核和冻结 rehearsal step score；
- Writer 必经、事件完整性、Director 放行/阻断/修复等机制指标；
- 7 个没有公开 checkpoint 的任务不会被伪造为语义零分。

每次正式运行应保留 `run-config.json`、版本/来源信息、逐题结果、模型 usage、原始或脱敏轨迹以及最终 summary。数字冲突时，以机器可读结果和原始证据为准。

本地结果目录约定如下：

```text
results/
├── config/                    # 冻结输入，不是运行结果
├── full/                      # 已归档全量结果
│   ├── results_without_Writer/
│   └── results_with_Writer/
├── partly/                    # 小规模配对验证
└── runs/                      # 新运行与断点续跑
    ├── original-full/
    ├── writer-full/
    └── writer-director-full/<run-id>/
```

若工作区附带历史结果，先阅读 `results/实验结果索引.md`，再查看各实验臂的 `run-config.json`、`baseline-summary.json`、`semantic-summary.json` 和逐题结果。不要把 benchmark 自带 fixtures、ground truth 或参考数据迁入 `results/`。

## 验证与开发

针对三层集成的快速检查：

```bash
pytest -q writer_harness/test_capability_matching.py director_harness/test_harness.py

cd writer_harness_ui
npm run check
```

OpenHarness 的完整测试从其子目录运行：

```bash
cd OpenHarness
uv run pytest -q
```

开发时遵循以下约束：

1. Writer 只负责执行前剧本与评审，Director 只负责工具调用前保障，OpenHarness 保持真实执行和权限边界。
2. Original、Writer、Writer + Director 的任务、模型和评分配置必须可追溯；不得因失败或低分删除任务槽位。
3. 预演内容不得写回 benchmark prompt、fixture、ground truth 或 Oracle。
4. API Key、代理凭证和授权信息不得写入结果；Director 日志会按敏感字段名脱敏，其他 artifact 仍需在归档前检查。
5. 新结果只写入 `results/`，不要创建 `final-v2`、`retry-final` 等来源不清的平行副本。
6. 对核心逻辑的变更应补充对应测试；仅更新文档或整理历史产物时无需运行无关的全量实验。
