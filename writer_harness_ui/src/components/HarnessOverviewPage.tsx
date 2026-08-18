import { MarkdownPreview } from "./MarkdownPreview";

const overviewMarkdown = `# 编导 / 导演 Harness 说明

## 定位

当前页面除了提供在线对话与双策略对比，也承担“把方法讲清楚”的职责。

- **当前 UI**：展示用户对话、vanilla 与编导 Harness 两种回复策略、剧本产物、评估指标与报错信息
- **编导 Harness**：在真实执行前增加“任务理解、剧本生成、执行决策”的前置层
- **导演 Harness**：位于执行链决策点，负责判断“这场戏能不能拍、该怎么拍、是否需要澄清或阻断”

整个方法的核心不是替代主 OpenHarness，而是在它之前叠加一层更可解释、更可控的编排逻辑。

## 流程图

下图概括了原始执行链与“编剧 + 导演”叠加后的执行链差异。

![编导 Harness 流程图](/assets/harness-overview.svg)

## 方法概览

### 1. vanilla 对照链

vanilla 模式保留原始执行思路：

1. 用户输入任务
2. 主 Harness 直接结合工具与环境进入执行
3. 产出结果、日志与执行评估

它适合做基线对照，帮助观察“仅执行”与“先编排再执行”之间的差异。

### 2. 编剧前置层

writer_harness 的核心目标包括：

- 在真实执行前生成结构化执行剧本
- 明确任务目标、成功标准与预期输出
- 梳理推荐执行步骤与验证步骤
- 提前识别风险、未知条件与能力缺口
- 为 actor_harness 提供更可控、更可审查的输入

这部分解决的是：演员 Harness 擅长接到任务立即执行，但不天然擅长在执行前先识别边界、补足信息并整理剧本。

### 3. 导演决策层

导演层的重点不是补写内容，而是做执行判断：

- 当前信息是否足够执行
- 是否应该直接执行、谨慎执行、降级执行或阻断
- 是否需要先向用户澄清条件
- 是否应该把工具调用顺序、环境补全与验证步骤进一步收紧

因此，导演层更像一个执行闸门，而不是新的回答层。

## 当前 UI 在体系中的位置

这套在线 UI 不是简单的聊天窗口，而是整个实验链路的可视化观察面板。

### 主要展示内容

- **对话区**：展示用户问题与两种策略的回复气泡
- **回复内容区**：严格展示执行阶段的 \`execution_output\`
- **剧本展示区**：只在 writer_harness 下展示结构化剧本
- **评价指标区**：展示完整性判断、充分性评分、执行状态与决策依据
- **报错区**：集中呈现 stderr、执行错误与调试信息

### 当前 UI 设计目标

- 让 vanilla 与 writer_harness 形成清晰可比的双栏视图
- 让“剧本”和“最终执行输出”严格分离
- 让执行链条中的中间判断、错误与轨迹更容易被观察和解释

## 核心数据与产物

在已有实现中，编导 Harness 主要围绕以下结构工作：

- **TaskProfile**：描述任务目标、成功标准与预期产物
- **DifficultyProfile**：描述工具条件、缺失信息与难度估计
- **ExecutionPlan**：组织前置思考、推荐步骤与验证步骤
- **WriterHarnessReport**：结构化执行剧本，是 actor_harness 的上游输入
- **ExecutionResult**：统一返回 \`final_prompt\`、\`script_report\`、\`final_script_report\`、\`online_completeness_judgment\`、\`judge_completeness_evaluation\` 等结果

## 为什么要引入编导层

单纯执行往往能更快开始，但也更容易出现：

- 在信息不足时直接进入工具调用
- 忽略环境边界与风险条件
- 执行顺序不稳定
- 输出能看懂，但中间决策不可解释

引入编导层后，链路会更强调：

- **执行前可解释性**
- **风险识别能力**
- **执行可控性**
- **结果可验证性**

## 当前模式与后续演进

当前主要支持两类对比路径：

| 模式 | 是否启用编剧层 | 是否进入导演决策 | 主要用途 |
| --- | --- | --- | --- |
| vanilla | 否 | 否 | 作为 actor-only 对照组 |
| writer_harness | 是 | 是 | 作为 actor + writer / director 实验组 |

后续可以继续扩展：

- 多轮会话与 session 绑定
- 工具轨迹摘要页面
- 标准化对比报告页面
- 将在线评估、真实执行与前端说明页进一步打通

## 阅读建议

如果把这个系统理解成拍一场戏：

- **用户** 提出需求
- **编剧** 先把戏写清楚
- **导演** 判断怎么拍、能不能拍
- **演员 / 主 Harness** 真正去执行
- **当前 UI** 负责把全过程展示给你看

这样更容易理解为什么这里既有聊天结果，也要有剧本、决策、评估与错误信息。`;

export function HarnessOverviewPage() {
  return (
    <section className="overview-page">
      <header className="overview-hero">
        <span className="eyebrow overview-hero__eyebrow">Method & UI Overview</span>
        <h1>当前 UI 与编导 Harness 介绍</h1>
        <p>以说明文档式页面整理当前系统的定位、执行链路、方法差异与界面职责，便于汇报、演示与后续页面扩展。</p>
      </header>
      <div className="overview-layout">
        <aside className="overview-toc">
          <h2>导读</h2>
          <a href="#定位">定位</a>
          <a href="#流程图">流程图</a>
          <a href="#方法概览">方法概览</a>
          <a href="#当前-ui-在体系中的位置">UI 角色</a>
          <a href="#核心数据与产物">数据结构</a>
          <a href="#当前模式与后续演进">模式对比</a>
        </aside>
        <div className="overview-content">
          <MarkdownPreview content={overviewMarkdown} />
        </div>
      </div>
    </section>
  );
}
