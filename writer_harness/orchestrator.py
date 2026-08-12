"""编排 vanilla 与 writer_harness 两类交互流程。"""

from __future__ import annotations

import json

from .actor_harness import ActorHarnessExecutor
from .models import ExecutionResult, HarnessRequest, InteractionMode
from .prompts import detect_language, get_generated_scripts_template, get_user_task_label
from .writer_harness import WriterHarness


class InteractionOrchestrator:
    """双模式统一编排器，负责决定是否启用编剧 Harness。"""

    def __init__(self, actor_executor: ActorHarnessExecutor, writer_harness: WriterHarness | None = None):
        self.actor_executor = actor_executor
        self.writer_harness = writer_harness

    def run(self, request: HarnessRequest) -> ExecutionResult:
        language = detect_language(request.query)
        user_task_label = get_user_task_label(language)

        if request.mode == InteractionMode.VANILLA:
            final_prompt = request.query
            return self.actor_executor.execute(final_prompt, request.mode)

        if request.mode == InteractionMode.WRITER_HARNESS:
            if self.writer_harness is None:
                raise ValueError("writer_harness 模式需要启用编剧 Harness")
            template = get_generated_scripts_template(language)
            final_prompt = f"{template}\n\n{user_task_label}\n{request.query}"
            result = self.actor_executor.execute(final_prompt, request.mode)
            result.script_report = result.final_script_report
            online_judgment = self.writer_harness.judge_online_completeness(result.stdout, round_index=1)
            result.online_completeness_judgment = {
                "is_complete": online_judgment.is_complete,
                "completeness_level": online_judgment.completeness_level,
                "matched_sections": online_judgment.matched_sections,
                "missing_sections": online_judgment.missing_sections,
                "matched_checks": online_judgment.matched_checks,
                "missing_checks": online_judgment.missing_checks,
                "rationale": online_judgment.rationale,
                "next_action": online_judgment.next_action,
                "round_index": online_judgment.round_index,
            }
            actor_report = result.final_script_report or self._extract_script_report_from_stdout(result.stdout)
            judge_input_report = actor_report or {}
            if actor_report is not None:
                result.final_script_report = actor_report
                result.script_report = actor_report
                result.script_report_source = result.script_report_source or "actor_harness_output"
                result.script_report_object_origin = result.script_report_object_origin or "actor_harness"
                result.script_report_transport_path = result.script_report_transport_path or "actor_harness_output"
            else:
                result.script_report_source = result.script_report_source or "actor_harness_output_parse_failed"
                result.script_report_object_origin = result.script_report_object_origin or "actor_harness"
                result.script_report_transport_path = result.script_report_transport_path or "actor_harness_output_parse_failed"
            result.judge_completeness_evaluation = self.writer_harness.judge_scripts_content(
                request.query,
                result.stdout,
                judge_input_report,
                online_judgment.matched_sections,
                online_judgment.missing_sections,
                online_judgment.matched_checks,
                online_judgment.missing_checks,
            ).to_dict()
            if not online_judgment.is_complete:
                retry_instruction = "\n\n---\nPlease revise the script content so that it explicitly includes these core sections as named fields or labeled sections: " + ", ".join(online_judgment.missing_sections) + ". Return the revised script content only."
                retry_prompt = final_prompt + retry_instruction
                retry_result = self.actor_executor.execute(retry_prompt, request.mode)
                retry_judgment = self.writer_harness.judge_online_completeness(retry_result.stdout, round_index=2)
                retry_result.online_completeness_judgment = {
                    "is_complete": retry_judgment.is_complete,
                    "completeness_level": retry_judgment.completeness_level,
                    "matched_sections": retry_judgment.matched_sections,
                    "missing_sections": retry_judgment.missing_sections,
                    "matched_checks": retry_judgment.matched_checks,
                    "missing_checks": retry_judgment.missing_checks,
                    "rationale": retry_judgment.rationale,
                    "next_action": retry_judgment.next_action,
                    "round_index": retry_judgment.round_index,
                }
                retry_actor_report = retry_result.final_script_report or self._extract_script_report_from_stdout(retry_result.stdout)
                retry_judge_input_report = retry_actor_report or {}
                if retry_actor_report is not None:
                    retry_result.final_script_report = retry_actor_report
                    retry_result.script_report = retry_actor_report
                    retry_result.script_report_source = retry_result.script_report_source or "actor_harness_output"
                    retry_result.script_report_object_origin = retry_result.script_report_object_origin or "actor_harness"
                    retry_result.script_report_transport_path = retry_result.script_report_transport_path or "actor_harness_output"
                else:
                    retry_result.script_report_source = retry_result.script_report_source or "actor_harness_output_parse_failed"
                    retry_result.script_report_object_origin = retry_result.script_report_object_origin or "actor_harness"
                    retry_result.script_report_transport_path = retry_result.script_report_transport_path or "actor_harness_output_parse_failed"
                retry_result.judge_completeness_evaluation = self.writer_harness.judge_scripts_content(
                    request.query,
                    retry_result.stdout,
                    retry_judge_input_report,
                    retry_judgment.matched_sections,
                    retry_judgment.missing_sections,
                    retry_judgment.matched_checks,
                    retry_judgment.missing_checks,
                ).to_dict()
                return retry_result
            return result

        raise ValueError(f"不支持的交互模式：{request.mode}")

    def _extract_script_report_from_stdout(self, stdout: str) -> dict | None:
        text = (stdout or "").strip()
        start = text.find("{")
        end = text.rfind("}")
        if start < 0 or end < start:
            return None
        try:
            parsed = json.loads(text[start : end + 1])
        except json.JSONDecodeError:
            return None
        required_keys = {"task_profile", "difficulty_profile", "execution_plan", "difficulty_judgment", "judgment_rationale", "execution_suggestion"}
        if not isinstance(parsed, dict):
            return None
        if not required_keys.issubset(parsed.keys()):
            return None
        return parsed
