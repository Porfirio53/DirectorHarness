# Writer Harness + OpenHarness

## 项目定位

本项目验证 Writer Harness 作为 OpenHarness 前置编导层的实际价值。Writer Harness 不直接替代执行智能体，而是在真实工具调用前生成结构化剧本，明确目标、步骤、风险、依赖和验证方式，再由 OpenHarness 完成执行。

项目保留两条长期对照路径：

- `vanilla`：用户任务直接交给 OpenHarness，作为 Original 对照组。
- `writer_harness`：Writer 先生成并审核剧本，再将最终剧本交给 OpenHarness，作为实验组。

当前正式评价范围为 HarnessBench 全部 106 题和 MCP-Persona Verified52 全部 52 题。所有成绩均为本地 OpenHarness-compatible 结果，不代表基准官方榜单成绩。

## 总体路线

1. 保持 Writer Harness 与 OpenHarness 的职责边界清晰，Writer 负责执行前规划，OpenHarness 负责真实执行。
2. 对 Original 和 Writer 两组使用固定任务集、重复次数、模型配置、工具范围、超时和评分口径，形成可复现对照。
3. 同时评价任务结果、执行过程、安全性、语义正确性、token 和时延，避免只用单一分数判断收益。
4. 根据全量结果识别 Writer 的适用边界；Writer v1 不作为全局硬门，后续仅研究轻量任务路由和定向启用。

## 执行链路

```text
用户任务
  -> Writer 生成结构化剧本（仅 writer_harness 模式）
  -> 完整性与充分性判断
  -> OpenHarness Agent / tools / session
  -> Benchmark Oracle、过程与安全评分
  -> results/ 中的结构化结果和汇总
```

Writer 输出的核心对象包括任务画像、难度画像、执行计划、风险与未知条件、验证步骤和最终剧本。真实执行以 `final_script_report` / `final_scripts` 为输入，同时保留 Writer 原始输出和工具轨迹作为审计证据。

## 工作区结构

| 路径 | 职责 |
|---|---|
| `OpenHarness/` | Agent 运行时、Writer 接口、正式运行与评分脚本 |
| `HarnessBench/` | 106 题基准、任务 fixtures 和 Oracle |
| `MCP-Persona/` | MCP-Persona 任务与模拟服务 |
| `tau3-bench/` | 已有基准依赖；仓库自带的参考数据不属于本项目运行结果 |
| `results/` | 本项目唯一的实验结果目录 |
| `results/config/` | 全量复跑所需的冻结任务清单和评分规范 |
| `writer_excute.py` | Writer 在线执行入口 |

## 结果目录规范

```text
results/
├── config/
├── runs/                       # 今后全量复跑产物
├── results_without_Writer/
│   ├── HarnessBench/
│   └── MCP-Persona/
└── results_with_Writer/
    ├── HarnessBench/
    └── MCP-Persona/
```

- 今后所有新生成的运行结果、日志、评分和汇总只能写入 `/home/patton/projects/harness/results/`。
- `results_without_Writer/` 只保存 Original 对照组，`results_with_Writer/` 只保存 Writer 实验组。
- 今后的完整复跑放入 `results/runs/<运行名>/`，默认运行名为 `original-full` 或 `writer-full`；需要保留多次运行时通过脚本参数指定新的运行目录。
- 每次正式全量运行保留任务清单、运行配置、版本信息、逐题结果和最终摘要；不得只保留人工整理后的结论。
- 基准仓库随源码版本管理的任务 fixtures、测试期望数据和官方参考数据不是本项目生成的实验结果，不迁入 `results/`。

## 全量复跑入口

运行前需要准备 `OpenHarness/.venv`、三个源码仓库，并按根目录 `.env.example` 在根目录 `.env` 中配置模型。以下脚本默认运行两轮，并写入 `results/runs/`：

```bash
# Original 对照组（两个基准各两轮，并完成评分）
bash OpenHarness/scripts/run_original_full.sh

# Writer 实验组（两个基准各两轮，并统一评分）
bash OpenHarness/scripts/run_writer_full.sh
```

低层运行器、两轮合并器、语义复核器和汇总器保留在 `OpenHarness/scripts/`，只用于上述全量流程或结果重算。Smoke、单题补跑、超时恢复、失败重试和历史阶段脚本不作为正式入口。

## 评价规范

- 数据集成员只由冻结任务范围决定，不得按模型成功与否删除低分或失败任务。
- Original 与 Writer 对照应尽量保持任务、模型、temperature、seed、工具范围、超时和重复次数一致；任何历史配置差异必须在进度文档中说明。
- MCP-Persona 同时保留 execution、expected-tool recall、sequence、semantic checkpoint 和过程分；无公开 checkpoint 的任务不得伪造语义零分。
- HarnessBench 同时保留 programmatic outcome、process、security 和 combined score；过程或安全分不能覆盖任务 Oracle 的失败证据。
- Writer 事件必须可证明 Writer 必经、规划状态隔离和事件完整；不得把预演内容写回任务 Prompt、fixture 或 Oracle。
- API Key、代理凭证和其他密钥不得写入结果；大体积工具输出使用摘要或 artifact 引用。

## 开发规范

1. 核心代码与实验编排分离。实验整理、报告更新和结果归档不得顺带修改 OpenHarness、Writer Harness 或基准核心逻辑。
2. 正式实验脚本必须可从干净环境完成全量运行，并以环境变量或显式参数接收路径与凭证。
3. 一次性排障脚本不长期保留；问题解决后应删除，通用能力才并入正式运行器。
4. 结果目录不得出现 `final-v2`、`retry-final` 等含义不清的平行副本；重跑应沿用固定分组和可审计的 repeat 结构。
5. 原始轨迹和机器可读评分是数字冲突时的最终依据，人工文档不得修改或掩盖失败结果。
6. 不提交缓存、临时沙箱、临时交付包或本地试跑数据。
7. 对核心逻辑的后续变更必须有对应测试；仅整理文档和历史产物时不运行无关测试。
