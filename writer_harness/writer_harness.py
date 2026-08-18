"""实现编剧 Harness：负责在线完整性判断与执行剧本充分性评估。"""

from __future__ import annotations

from .llm_clients import LLMClient
from .models import DifficultyProfile, ExecutionPlan, JudgeCompletenessEvaluation, JudgeFieldScore, OnlineCompletenessJudgment, TaskProfile, WriterHarnessReport
from .prompts import (
    MULTITURN_PLAN_DECISION_SYSTEM_PROMPT_EN,
    MULTITURN_PLAN_DECISION_SYSTEM_PROMPT_ZH,
    detect_language,
    get_judge_completeness_system_prompt,
)
import json

class WriterHarness:
    """编剧 Harness：不执行任务，只负责判断演员输出的剧本是否完整且足够可执行。"""

    def __init__(self, llm_client: LLMClient):
        """初始化编剧 Harness。

        参数说明：
        - llm_client: 负责执行剧本充分性评估的大模型客户端。
          当前默认面向真实模型 API，用于保持 actor-only 与 actor+writer 的对比一致性。
        """

        self.llm_client = llm_client

    def judge_multiturn_plan_transition(
        self,
        user_query: str,
        history_context: str,
        previous_plan: dict | None,
    ) -> dict:
        language = detect_language(user_query)
        system_prompt = MULTITURN_PLAN_DECISION_SYSTEM_PROMPT_ZH if language == "zh" else MULTITURN_PLAN_DECISION_SYSTEM_PROMPT_EN
        user_prompt = json.dumps(
            {
                "history_context": history_context,
                "previous_plan": previous_plan or {},
                "current_user_query": user_query,
            },
            ensure_ascii=False,
            indent=2,
        )
        raw = self.llm_client.complete(system_prompt, user_prompt)
        return self._parse_json_object(raw, "多轮计划编排判断")

    def judge_online_completeness(self, actor_harness_output: str, round_index: int = 1) -> OnlineCompletenessJudgment:
        """快速判断演员 Harness 当前输出是否已经具备完整剧本结构。

        参数说明：
        - actor_harness_output: 演员 Harness 当前轮次输出的文本。
        - round_index: 第几轮在线判断，用于区分首次生成与重试补全。
        """

        text = actor_harness_output or ""
        lowered = text.lower()
        required_sections = [
            "task_profile",
            "difficulty_profile",
            "execution_plan",
            "difficulty_judgment",
            "judgment_rationale",
            "execution_suggestion",
        ]
        section_patterns = {s: [s, f"【{s}】", f"[{s}]"] for s in required_sections}
        matched = [section for section, patterns in section_patterns.items() if any(p in lowered for p in patterns)]
        missing = [section for section in required_sections if section not in matched]

        checks = {
            "task_goal_or_objective": self._contains_any(lowered, ["task_goal", "objective", "goal", "任务目标"]),
            "expected_output": self._contains_any(lowered, ["expected_output", "output", "产出", "输出"]),
            "success_criteria": self._contains_any(lowered, ["success_criteria", "success criteria", "成功标准", "验证标准"]),
            "known_conditions": self._contains_any(lowered, ["known_conditions", "known conditions", "已知条件"]),
            "unknown_or_risk": self._contains_any(lowered, ["unknown_conditions", "missing_tools", "missing_tool_requirements", "risk", "风险", "未知条件", "缺失工具", "缺失工具需求"]),
            "missing_tools": self._contains_any(lowered, ["missing_tools", "缺失工具"]),
            "missing_tool_requirements": self._contains_any(lowered, ["missing_tool_requirements", "缺失工具需求"]),
            "resolution_strategies": self._contains_any(lowered, ["resolution_strategies", "解决策略", "补全策略"]),
            "recommended_steps": self._contains_any(lowered, ["recommended_steps", "steps", "next step", "建议步骤", "执行步骤"]),
            "validation_steps": self._contains_any(lowered, ["validation_steps", "validation", "verify", "验证步骤", "校验"]),
            "judgment_rationale": self._contains_any(lowered, ["judgment_rationale", "rationale", "依据", "原因"]),
            "explicit_suggestion": self._contains_any(lowered, ["execution_suggestion", "execute", "cautious_execute", "re_generate_scripts", "执行", "谨慎执行"]),
        }
        structured_checks = self._check_plan_tool_linkage(text)
        checks.update(structured_checks)

        matched_checks = [name for name, ok in checks.items() if ok]
        missing_checks = [name for name, ok in checks.items() if not ok]

        hard_missing = any(name in missing_checks for name in ["task_goal_or_objective", "expected_output", "recommended_steps", "validation_steps", "explicit_suggestion"])
        has_uncertainty_modeling = checks["unknown_or_risk"]

        if not matched:
            completeness_level = "incomplete"
        elif hard_missing:
            completeness_level = "incomplete"
        elif not has_uncertainty_modeling or len(missing_checks) > 1:
            completeness_level = "borderline"
        else:
            completeness_level = "complete"

        is_complete = completeness_level == "complete"
        next_action = "execute" if completeness_level == "complete" else ("cautious_execute" if completeness_level == "borderline" else "re_generate_scripts")
        rationale = f"matched_sections={matched}; missing_sections={missing}; matched_checks={matched_checks}; missing_checks={missing_checks}; level={completeness_level}"
        return OnlineCompletenessJudgment(
            is_complete=is_complete,
            completeness_level=completeness_level,
            matched_sections=matched,
            missing_sections=missing,
            matched_checks=matched_checks,
            missing_checks=missing_checks,
            rationale=rationale,
            next_action=next_action,
            round_index=round_index,
        )

    def _check_plan_tool_linkage(self, raw: str) -> dict[str, bool]:
        try:
            report = self._parse_json_object(raw, "演员 Harness 剧本")
        except ValueError:
            return {
                "execution_plan_structure": False,
                "execution_plan_tool_linkage": False,
                "missing_tool_step_linkage": False,
            }

        difficulty_profile = report.get("difficulty_profile")
        execution_plan = report.get("execution_plan")
        if not isinstance(difficulty_profile, dict) or not isinstance(execution_plan, dict):
            return {
                "execution_plan_structure": False,
                "execution_plan_tool_linkage": False,
                "missing_tool_step_linkage": False,
            }

        recommended_steps = execution_plan.get("recommended_steps")
        validation_steps = execution_plan.get("validation_steps")
        available_tools = difficulty_profile.get("available_tools", [])
        missing_tools = difficulty_profile.get("missing_tools", [])
        requirements = difficulty_profile.get("missing_tool_requirements", [])
        structure_ok = all(
            isinstance(value, list) and all(isinstance(item, str) and item.strip() for item in value)
            for value in [recommended_steps, validation_steps]
        ) and bool(recommended_steps) and bool(validation_steps)
        if not structure_ok:
            return {
                "execution_plan_structure": False,
                "execution_plan_tool_linkage": False,
                "missing_tool_step_linkage": False,
            }

        normalized_steps = [self._normalize_linkage_text(step) for step in recommended_steps]
        references = [str(tool).strip() for tool in available_tools if str(tool).strip()]
        requirement_steps: dict[str, list[str]] = {}
        for requirement in requirements if isinstance(requirements, list) else []:
            if not isinstance(requirement, dict):
                continue
            missing_tool = str(requirement.get("missing_tool", "")).strip()
            sub_steps = requirement.get("required_for_steps", [])
            if missing_tool and isinstance(sub_steps, list):
                requirement_steps[missing_tool] = [str(step).strip() for step in sub_steps if str(step).strip()]

        direct_or_decomposed = all(
            any(self._normalize_linkage_text(reference) in step for reference in references)
            or any(self._normalize_linkage_text(sub_step) == step for sub_steps in requirement_steps.values() for sub_step in sub_steps)
            for step in normalized_steps
        )
        missing_links_ok = all(
            missing_tool in requirement_steps
            and any(self._normalize_linkage_text(sub_step) in normalized_steps for sub_step in requirement_steps[missing_tool])
            for missing_tool in missing_tools if isinstance(missing_tool, str) and missing_tool.strip()
        )
        return {
            "execution_plan_structure": True,
            "execution_plan_tool_linkage": direct_or_decomposed,
            "missing_tool_step_linkage": missing_links_ok,
        }

    @staticmethod
    def _normalize_linkage_text(value: str) -> str:
        return " ".join(value.lower().split())

    def judge_scripts_content(
        self,
        query: str,
        actor_harness_output: str,
        script_report: dict,
        matched_sections: list[str],
        missing_sections: list[str],
        matched_checks: list[str],
        missing_checks: list[str],
    ) -> JudgeCompletenessEvaluation:
        """对执行剧本内容做细粒度充分性评估。

        参数说明：
        - query: 用户原始任务，作为评估上下文。
        - actor_harness_output: 演员 Harness 的文本输出。
        - script_report: 已抽取出的结构化执行剧本。
        - matched_sections/missing_sections: 在线规则判断出的节区匹配结果。
        - matched_checks/missing_checks: 在线规则判断出的检查项匹配结果。
        """

        language = detect_language(query)
        system_prompt = get_judge_completeness_system_prompt(language)
        user_prompt = json.dumps(
            {
                "query": query,
                "actor_harness_output": actor_harness_output,
                "script_report": script_report,
                "matched_sections": matched_sections,
                "missing_sections": missing_sections,
                "matched_checks": matched_checks,
                "missing_checks": missing_checks,
                "output_schema": {
                    "overall_score": 0,
                    "planning_score": 0,
                    "structure_score": 0,
                    "risk_score": 0,
                    "clarification_score": 0,
                    "overall_sufficiency": "sufficient|partially_sufficient|insufficient",
                    "next_action": "execute|cautious_execute|re_generate_scripts",
                    "section_scores": {"<section_name>": {"present": True, "content_score": 0, "reason": ""}},
                    "check_scores": {"<check_name>": {"present": True, "content_score": 0, "reason": ""}},
                    "strengths": [],
                    "weaknesses": [],
                    "rationale": "",
                },
            },
            ensure_ascii=False,
            indent=2,
        )
        raw = self.llm_client.complete(system_prompt, user_prompt)
        data = self._parse_json_object(raw, "编剧 Harness 评估输出")
        normalized_sections = self._normalize_presence_scores(data.get("section_scores"), matched_sections, missing_sections)
        normalized_checks = self._normalize_presence_scores(data.get("check_scores"), matched_checks, missing_checks)
        section_scores = {
            key: JudgeFieldScore(
                present=bool(value.get("present")),
                content_score=value.get("content_score"),
                reason=str(value.get("reason", "")),
            )
            for key, value in normalized_sections.items()
            if isinstance(value, dict)
        }
        check_scores = {
            key: JudgeFieldScore(
                present=bool(value.get("present")),
                content_score=value.get("content_score"),
                reason=str(value.get("reason", "")),
            )
            for key, value in normalized_checks.items()
            if isinstance(value, dict)
        }
        overall_score = int(data.get("overall_score", 0) or 0)
        return JudgeCompletenessEvaluation(
            overall_score=overall_score,
            planning_score=self._optional_int(data.get("planning_score")),
            structure_score=self._optional_int(data.get("structure_score")),
            risk_score=self._optional_int(data.get("risk_score")),
            clarification_score=self._optional_int(data.get("clarification_score")),
            overall_sufficiency=str(data.get("overall_sufficiency", "insufficient")),
            next_action=self._score_to_next_action(overall_score),
            section_scores=section_scores,
            check_scores=check_scores,
            strengths=[str(item) for item in (data.get("strengths") or [])],
            weaknesses=[str(item) for item in (data.get("weaknesses") or [])],
            rationale=str(data.get("rationale", "")),
        )

    def _contains_any(self, text: str, candidates: list[str]) -> bool:
        """判断文本中是否命中任一候选关键词。"""

        return any(candidate.lower() in text for candidate in candidates if candidate)

    def _optional_int(self, value: object) -> int | None:
        """将可选值尽量安全地转成整数；失败时返回 None。"""

        if value is None:
            return None
        try:
            return int(value)
        except (TypeError, ValueError):
            return None

    def _score_to_next_action(self, overall_score: int) -> str:
        """根据总体评分映射下一步动作。"""

        if overall_score > 85:
            return "execute"
        if overall_score >= 70:
            return "cautious_execute"
        return "re_generate_scripts"

    def _normalize_presence_scores(self, raw_scores: dict | None, matched_fields: list[str], missing_fields: list[str]) -> dict[str, dict]:
        """把模型评分结果规整为稳定的 presence-score 字典结构。"""

        normalized: dict[str, dict] = {}
        source = raw_scores or {}
        for name in matched_fields:
            raw_value = source.get(name) if isinstance(source, dict) else None
            if isinstance(raw_value, dict):
                normalized[name] = {"present": True, "content_score": raw_value.get("content_score"), "reason": str(raw_value.get("reason", ""))}
            else:
                normalized[name] = {"present": True, "content_score": None, "reason": ""}
        for name in missing_fields:
            raw_value = source.get(name) if isinstance(source, dict) else None
            if isinstance(raw_value, dict):
                normalized[name] = {"present": False, "content_score": None, "reason": str(raw_value.get("reason", ""))}
            else:
                normalized[name] = {"present": False, "content_score": None, "reason": ""}
        return normalized

    def _build_minimal_report_dict(self, report: WriterHarnessReport) -> dict:
        """删除仅供调试使用的原始响应，保留最小必要剧本字段。"""

        report_dict = report.to_dict()
        report_dict.pop("raw_response", None)
        return report_dict

    def _parse_json_object(self, raw: str, label: str = "编剧 Harness 输出") -> dict:
        """解析模型返回的 JSON object 文本。"""

        text = raw.strip()
        if text.startswith("```"):
            text = text.strip("`")
            if text.lower().startswith("json"):
                text = text[4:].strip()
        try:
            data = json.loads(text)
        except json.JSONDecodeError as exc:
            raise ValueError(f"{label}不是合法 JSON：{raw}") from exc
        if not isinstance(data, dict):
            raise ValueError(f"{label}必须是 JSON object")
        return data
