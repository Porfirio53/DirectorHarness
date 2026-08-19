当前分支为 `writer-director-optimazation`。核心 OpenHarness、Writer v1、Director 和 Actor 源码未改动；新增的是局部实验配置与通用启动脚本。当前没有运行中的实验，也没有在本轮调用付费 API。

**1. 结果与核心原因**

全量结果来源：

- Original：[results/full/results_without_Writer](/home/patton/projects/harness/results/full/results_without_Writer)
- Writer+Director：[results/runs/writer-director-full/20260817T040809Z-group-writer-v1](/home/patton/projects/harness/results/runs/writer-director-full/20260817T040809Z-group-writer-v1)

| 指标 | MCP-Persona Original | Writer+Director | 变化 |
|---|---:|---:|---:|
| execution | 0.716308 | 0.632772 | -11.66% |
| expected-tool recall | 0.853816 | 0.766434 | -10.23% |
| sequence | 0.779571 | 0.695070 | -10.84% |
| semantic task macro | 0.551296 | 0.476296 | -13.60% |
| rehearsal combined | 0.440434 | 0.366109 | -16.88% |
| 总工具调用 | 1153 | 762 | -33.91% |
| 平均耗时 | 70.66s | 114.20s | +61.61% |

| 指标 | HarnessBench Original | Writer+Director | 变化 |
|---|---:|---:|---:|
| outcome | 0.839027 | 0.832846 | -0.74% |
| process | 0.994812 | 0.894733 | -10.06% |
| combined | 0.828411 | 0.742902 | -10.32% |
| 总工具调用 | 2323 | 2554 | +9.94% |
| 平均耗时 | 147.38s | 309.86s | +110.24% |

原因不是两个数据集的统计口径不一致，而是它们对“少调用”的惩罚方式不同：

- MCP-Persona 的 execution、recall、sequence 直接按期望工具链计算。以任务 19 为例，工具调用从 `23` 降到 `2.5`，execution 从 `0.611` 降到 `0.167`；遗漏一个必需步骤会同时损失 recall 和 LCS sequence。
- HarnessBench 的 `combined` 近似为 `outcome × process × security`。Director 没有检查业务 postcondition，导致工具轨迹即使存在，过程分仍下降。例如任务 019 的 process 从 `1.0` 降到 `0.5`，combined 从 `0.8575` 降到 `0.4125`。
- Writer 先进行无工具的剧本生成、judge，必要时再 regeneration，然后把自然语言 `final_scripts` 附加给 Actor，见 [writer_handoff.py:901](/home/patton/projects/harness/OpenHarness/src/openharness/rehearsal/writer_handoff.py:901)。
- Writer 的事件记录器是 observe-only；`action_proposed()` 永远返回 `allow`，不能阻止错误工具，也不能强制计划中的步骤，见 [writer_handoff.py:214](/home/patton/projects/harness/OpenHarness/src/openharness/rehearsal/writer_handoff.py:214)。
- Director 目前主要做“工具是否注册、首次调用是否可用”的会话级缓存检查，不读取 Writer 的业务 milestone、调用 cardinality、参数继承或完成条件，见 [director_harness/harness.py:78](/home/patton/projects/harness/director_harness/harness.py:78)。
- 真正的执行前位置是正确的：参数校验后、权限检查和副作用执行前，见 [query.py:897](/home/patton/projects/harness/OpenHarness/src/openharness/engine/query.py:897)。但它拿不到完整的 Writer 计划状态，因此只能检查单个 action。

**2. 最值得优先优化的代码文件和关键点**

优先级 1：把 Writer 的自然语言计划改成可执行的结构化契约。

重点文件：

- [writer_handoff.py:154](/home/patton/projects/harness/OpenHarness/src/openharness/rehearsal/writer_handoff.py:154)
- [writer_handoff.py:624](/home/patton/projects/harness/OpenHarness/src/openharness/rehearsal/writer_handoff.py:624)
- [writer_harness/models.py](/home/patton/projects/harness/writer_harness/models.py)
- [writer_harness/prompts.py:128](/home/patton/projects/harness/writer_harness/prompts.py:128)

每个必需步骤应至少包含：精确 live tool、依赖边、必需参数、调用次数/cardinality、允许重试次数、postcondition。尤其要保留 fan-out、写后验证、分页和多阶段任务。

优先级 2：修正 capability matching 的过宽关键词匹配。

- [capability_matching.py:242](/home/patton/projects/harness/writer_harness/capability_matching.py:242)

当前逻辑会把 query、剧本、工具名和说明文本做关键词命中，容易把“能力类别”误当成“可执行工具”，也容易把多个必需动作压成一个泛化步骤。应优先依据 live schema、操作类型和参数进行窄匹配。

优先级 3：让 Actor handoff 保留步骤与工具的一一映射。

- [writer_handoff.py:580](/home/patton/projects/harness/OpenHarness/src/openharness/rehearsal/writer_handoff.py:580)
- [actor_harness.py:19](/home/patton/projects/harness/writer_harness/actor_harness.py:19)
- [orchestrator.py:20](/home/patton/projects/harness/writer_harness/orchestrator.py:20)

不能只传 `recommended_steps` 文本；应传递结构化 plan，并明确哪些步骤是 mandatory，避免 Actor 为了“minimal necessary script”删掉任务必需动作。

优先级 4：给 Director 增加有限的 plan-state 检查。

- [director_harness/harness.py:78](/home/patton/projects/harness/director_harness/harness.py:78)
- [query.py:973](/home/patton/projects/harness/OpenHarness/src/openharness/engine/query.py:973)

建议只增加最小状态机：当前允许的下一个 milestone、参数继承、fan-out 计数、写操作 postcondition 和 bounded retry。安全读操作可放行，不能无条件阻断正常的必要写操作。

优先级 5：正确性稳定后再降低 Writer 额外请求成本。

当前 draft/judge/regeneration 是 HarnessBench 耗时翻倍的重要原因。应在必需步骤不再丢失后，再考虑合并 judge、减少 regeneration 或缓存只读计划判断。

**3. 固定局部任务集**

MCP-Persona：

`19, 65, 36, 10, 1`

HarnessBench：

`086-sql-migration-preflight-rollback`、`008-image-recognize`、`052-metric-definition-audit`、`078-local-api-cursor-retry-ledger`、`019-incident-runbook-synthesis`

这些任务都满足：Writer+Director 得分低于 Original，工具调用数不增加，并且分别覆盖长链截断、写操作遗漏、认证重试、fan-out、分页恢复、结构化产物和多模态输入等可优化失败模式。详细逐任务数据已保存在 [writer_director_partly_selection.json](/home/patton/projects/harness/OpenHarness/scripts/writer_director_partly_selection.json)。

基线快照已建立在：

[results/runs/writer-director-partly/2026_0819_1455](/home/patton/projects/harness/results/runs/writer-director-partly/2026_0819_1455)

其中包含 Original 和 Writer-Director 两个 arm，共 145 个文件，SHA256 校验无失败。

**4. 通用局部实验命令**

首次运行两个数据集：

```bash
bash OpenHarness/scripts/run_writer_director_partly.sh
```

只运行 MCP-Persona：

```bash
bash OpenHarness/scripts/run_writer_director_partly.sh --dataset mcp
```

只运行 HarnessBench：

```bash
bash OpenHarness/scripts/run_writer_director_partly.sh --dataset harnessbench
```

中断后恢复：

```bash
bash OpenHarness/scripts/run_writer_director_partly.sh \
  --resume \
  --run-id <YYYY_MMDD_HHMM>
```

离线检查：

```bash
bash OpenHarness/scripts/run_writer_director_partly.sh --check-only
bash OpenHarness/scripts/run_writer_director_partly.sh --preflight-only
bash OpenHarness/scripts/run_writer_director_partly.sh --dry-run
```

脚本会自动：

- 强制检查当前分支；
- 默认使用根目录 `.env`；
- 固定 Writer v1、Director、`qwen3.6-plus` 和两次 repeat；
- 创建时间戳目录；
- 复制并校验 Original/Writer-Director 子集基线；
- 支持按结果目录恢复；
- 源码优化后在本次运行目录生成独立 `source-lock`，不会把修改后的 Writer 误标成组长原始压缩包版本。

离线验证结果：Writer/Director/HarnessBench 相关测试 `44 passed`，配置与 MCP 运行链 `117 passed, 4 skipped`，脚本语法、`ruff` 和 HarnessBench preflight 均通过。此前上下文中误触发的 `2026_0819_1456` 目录是已中止的测试目录，不应纳入后续分析。