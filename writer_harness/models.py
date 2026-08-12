"""定义演员/编剧 Harness 交互所共享的数据结构和执行结果结构。"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any


class InteractionMode(str, Enum):
    """两类交互模式：直连执行，或启用编剧 Harness。"""

    VANILLA = "vanilla"
    WRITER_HARNESS = "writer_harness"


class ActorBackend(str, Enum):
    """演员 Harness 的执行后端。

    当前仅保留真实 OpenHarness 执行路径，用于对比：
    1. actor_harness 单独执行；
    2. actor_harness 在 writer_harness 编导辅助下执行。
    """

    OPENHARNESS = "openharness"


class LLMBackend(str, Enum):
    """编剧 Harness 的模型后端。

    当前仅保留真实模型接口，避免 mock 模式干扰 actor-only 与 actor+writer 的实验对比。
    """

    OPENAI_COMPATIBLE = "openai-compatible"


@dataclass
class TaskProfile:
    """描述任务目标本身的基础画像。

    该结构回答“用户到底要做什么、成功后应交付什么”两个问题，
    是编剧 Harness 生成执行剧本时最上游的一层抽象。
    """

    task_type: str
    task_goal: str = ""
    success_criteria: list[str] = field(default_factory=list)
    expected_output: str = ""


@dataclass
class DifficultyProfile:
    """描述任务难度、资源条件与能力缺口。

    该结构用于帮助演员 Harness 判断是否可以直接执行、是否需要谨慎执行，
    以及是否必须先向用户补充澄清。
    """

    difficulty: str
    available_tools: list[str] = field(default_factory=list)
    missing_tools: list[str] = field(default_factory=list)
    known_conditions: list[str] = field(default_factory=list)
    unknown_conditions: list[str] = field(default_factory=list)
    estimated_cost: str = ""


@dataclass
class ExecutionPlan:
    """描述执行前思考、推荐步骤与验证步骤。

    这是执行剧本中最贴近“可操作流程”的部分，既保留规划，也保留验证要求，
    以避免演员 Harness 进入只执行不校验的状态。
    """

    pre_execution_thoughts: list[str] = field(default_factory=list)
    recommended_steps: list[str] = field(default_factory=list)
    validation_steps: list[str] = field(default_factory=list)


@dataclass
class WriterHarnessReport:
    """编剧 Harness 生成的结构化执行剧本，可注入演员 Harness 的最终 prompt。"""

    task_profile: TaskProfile
    difficulty_profile: DifficultyProfile
    execution_plan: ExecutionPlan
    difficulty_judgment: str = ""
    judgment_rationale: list[str] = field(default_factory=list)
    execution_suggestion: str = ""
    raw_response: str | None = None

    def to_dict(self) -> dict[str, Any]:
        """将结构化执行剧本转换为适合 JSON 输出的字典。

        返回结果既可用于 CLI 输出，也可作为后续在线执行脚本的中间输入。
        """

        return {
            "task_profile": self.task_profile.__dict__,
            "difficulty_profile": self.difficulty_profile.__dict__,
            "execution_plan": self.execution_plan.__dict__,
            "difficulty_judgment": self.difficulty_judgment,
            "judgment_rationale": self.judgment_rationale,
            "execution_suggestion": self.execution_suggestion,
            "raw_response": self.raw_response,
        }


@dataclass
class OnlineCompletenessJudgment:
    """描述对演员 Harness 当前输出是否足够完整的快速在线判断。"""

    is_complete: bool
    completeness_level: str
    matched_sections: list[str]
    missing_sections: list[str]
    matched_checks: list[str]
    missing_checks: list[str]
    rationale: str
    next_action: str
    round_index: int = 1


@dataclass
class JudgeFieldScore:
    """记录单个节区或检查项的存在性与内容质量评分。"""

    present: bool
    content_score: int | None
    reason: str


@dataclass
class JudgeCompletenessEvaluation:
    """描述对执行剧本充分性的细粒度评估结果。

    与 OnlineCompletenessJudgment 的快速规则判断不同，这里保留总分、分项分、
    优势、缺陷与理由，适合后续做摘要展示或驱动执行决策。
    """

    overall_score: int
    planning_score: int | None
    structure_score: int | None
    risk_score: int | None
    clarification_score: int | None
    overall_sufficiency: str
    next_action: str
    section_scores: dict[str, JudgeFieldScore] = field(default_factory=dict)
    check_scores: dict[str, JudgeFieldScore] = field(default_factory=dict)
    strengths: list[str] = field(default_factory=list)
    weaknesses: list[str] = field(default_factory=list)
    rationale: str = ""

    def to_dict(self) -> dict[str, Any]:
        """把评估对象序列化为普通字典，便于 JSON 输出与外部脚本消费。"""

        return {
            "overall_score": self.overall_score,
            "planning_score": self.planning_score,
            "structure_score": self.structure_score,
            "risk_score": self.risk_score,
            "clarification_score": self.clarification_score,
            "overall_sufficiency": self.overall_sufficiency,
            "next_action": self.next_action,
            "section_scores": {key: value.__dict__ for key, value in self.section_scores.items()},
            "check_scores": {key: value.__dict__ for key, value in self.check_scores.items()},
            "strengths": self.strengths,
            "weaknesses": self.weaknesses,
            "rationale": self.rationale,
        }


@dataclass
class HarnessRequest:
    """描述一次完整的编剧/演员 Harness 交互请求。

    参数说明：
    - query: 用户原始任务描述，是整条链路的唯一必需输入。
    - mode: 交互模式，决定是否直连演员 Harness，或先启用 writer_harness 生成结构化执行剧本。
    - metadata: 预留给调用方挂载额外上下文，目前不参与核心逻辑判断。
    """

    query: str
    mode: InteractionMode
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass
class ExecutionResult:
    """描述演员/编剧 Harness 执行链路的统一输出结果。

    参数说明：
    - final_prompt: 最终真正交给演员 Harness 的输入文本。
    - script_report: 编剧 Harness 生成或回填的结构化执行剧本。
    - actor_harness_report: 演员 Harness 返回的结构化结果视图。
    - final_script_report: 若演员 Harness 输出了可直接复用的结构化剧本，
      则在此字段中保存，用于后续执行阶段或二次判断。
    - online_completeness_judgment: 基于规则的在线快速完整性判断。
    - judge_completeness_evaluation: 更细粒度的充分性评分结果。
    """

    ok: bool
    mode: str
    final_prompt: str
    stdout: str
    stderr: str = ""
    return_code: int = 0
    script_report: WriterHarnessReport | dict[str, Any] | None = None
    script_report_source: str | None = None
    script_report_object_origin: str | None = None
    script_report_transport_path: str | None = None
    actor_harness_report: dict[str, Any] | None = None
    final_script_report: dict[str, Any] | None = None
    online_completeness_judgment: dict[str, Any] | None = None
    judge_completeness_evaluation: dict[str, Any] | None = None

    def __post_init__(self):
        """在两个剧本结果字段之间做最小同步，避免出现单边为空。"""

        if self.final_script_report is None:
            self.final_script_report = self.actor_harness_report
        if self.actor_harness_report is None:
            self.actor_harness_report = self.final_script_report

