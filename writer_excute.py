"""基于 writer_harness 运行单条在线执行链路。

功能定位：
1. 面向单条在线 query，而不是离线数据集样本；
2. 支持 vanilla / writer_harness 两种进入演员 Harness 前的输入策略；其中 vanilla 对应“不启用 writer_harness”；
3. 调用 `python -m writer_harness` 生成执行剧本或直接触发演员 Harness；
4. 在 writer_harness 模式下，对演员 Harness 输出的结构化剧本做完整性评估；
5. 当执行剧本评分达标时，把演员 Harness 当前输出视为执行前依据，进入真实执行阶段；
6. 支持在执行阶段启用 `stream-json`，并自动提取工具调用轨迹。

说明：
- 该脚本是面向 writer_harness 新语义的在线执行入口；
- 当前 actor 与 writer 侧都不再提供 mock 模式，确保核心对比维度聚焦于 writer_harness 是否启用；
- script_report 表示编剧或链路中抽取出的结构化执行剧本；
- actor_harness_output 表示演员 Harness 当前轮次原始输出文本；
- final_script_report 表示演员 Harness 产出的最终结构化剧本快照，可作为真实执行阶段的输入依据。
"""

from __future__ import annotations

import argparse
import copy
import json
import os
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from writer_harness.capability_matching import match_openharness_capabilities


class Style:
    """终端样式常量集合，用于在支持 ANSI 的终端中增强摘要可读性。"""

    RESET = "\033[0m"
    BOLD = "\033[1m"
    DIM = "\033[2m"
    CYAN = "\033[36m"
    GREEN = "\033[32m"
    YELLOW = "\033[33m"
    RED = "\033[31m"


class ConsoleUI:
    """封装命令行摘要打印逻辑，统一处理样式与兼容性。"""

    @staticmethod
    def supports_ansi() -> bool:
        return sys.stdout.isatty() and os.environ.get("TERM", "") != "dumb"

    @staticmethod
    def styled(text: str, *styles: str) -> str:
        if not ConsoleUI.supports_ansi():
            return text
        return "".join(styles) + text + Style.RESET

    @staticmethod
    def print_header(text: str) -> None:
        print(ConsoleUI.styled(text, Style.BOLD, Style.CYAN))

    @staticmethod
    def print_success(text: str) -> None:
        print(ConsoleUI.styled(text, Style.GREEN))

    @staticmethod
    def print_warning(text: str) -> None:
        print(ConsoleUI.styled(text, Style.YELLOW))

    @staticmethod
    def print_error(text: str) -> None:
        print(ConsoleUI.styled(text, Style.RED))


@dataclass
class ExecutionDecision:
    """描述是否进入真实执行阶段，以及对应的执行指令与判断理由。

    参数说明：
    - should_execute: 是否允许进入真实执行。
    - score_band: 评分分档，通常用于区分高分直接执行与中分谨慎执行。
    - execution_prompt: 真正交给演员 Harness 的执行阶段 prompt。
    - rationale: 解释为什么进入或阻止执行阶段。
    """

    should_execute: bool
    score_band: str
    execution_prompt: str | None = None
    rationale: str = ""


def extract_tool_trace_from_stream_json(stdout: str) -> dict[str, Any]:
    """从 `stream-json` 文本中提取工具调用轨迹与演员输出。

    参数说明：
    - stdout: OpenHarness `stream-json` 模式下的逐行事件输出。

    返回值包含：
    - tool_events: 详细工具事件；
    - tool_sequence: 仅保留开始/结束节点的紧凑顺序；
    - assistant_text: 合并后的演员文本输出；
    - used_ask_user_question: 是否触发过 ask_user_question。
    """

    tool_events: list[dict[str, Any]] = []
    tool_sequence: list[dict[str, str]] = []
    assistant_chunks: list[str] = []
    for line in stdout.splitlines():
        text = line.strip()
        if not text or not text.startswith("{"):
            continue
        try:
            payload = json.loads(text)
        except json.JSONDecodeError:
            continue
        event_type = payload.get("type")
        if event_type == "assistant_delta":
            assistant_chunks.append(str(payload.get("text", "")))
            continue
        if event_type == "assistant_complete":
            complete_text = str(payload.get("text", ""))
            if complete_text:
                assistant_chunks = [complete_text]
            continue
        if event_type in {"tool_started", "tool_completed"}:
            normalized = {
                "type": event_type,
                "tool_name": str(payload.get("tool_name", "")),
                "tool_input": payload.get("tool_input") if event_type == "tool_started" else None,
                "output": payload.get("output") if event_type == "tool_completed" else None,
                "is_error": payload.get("is_error") if event_type == "tool_completed" else None,
            }
            tool_events.append(normalized)
            tool_name = normalized["tool_name"]
            if tool_name:
                tool_sequence.append(
                    {
                        "name": tool_name,
                        "type": "start" if event_type == "tool_started" else "complete",
                    }
                )
    return {
        "tool_events": tool_events,
        "tool_sequence": tool_sequence,
        "assistant_text": "".join(assistant_chunks).strip(),
        "used_ask_user_question": any(item.get("name") == "ask_user_question" for item in tool_sequence),
    }


def build_execute_prompt(query: str, summary: dict[str, Any], score: int, score_band: str) -> str:
    """把当前执行剧本摘要重组为真实执行阶段 prompt。

    参数说明：
    - query: 用户原始任务。
    - summary: 在线评估后的摘要对象。
    - score: 剧本充分性总分。
    - score_band: 高分/中分执行分档，决定提示语气。
    """

    actor_harness_output = str(summary.get("actor_harness_output", "")).strip()
    final_scripts = summary.get("final_script_report") or summary.get("script_report")
    execute_instruction = {
        "high": "请将以下执行剧本视为已经通过审核。执行时优先遵循剧本中明确的目标、步骤和验证方式，直接进入真实执行，并输出清晰的执行步骤与最终结果。",
        "medium": "请将以下执行剧本视为基本合格。执行时必须保留谨慎策略：先简要确认关键前提，再按照剧本中的计划执行，遇到不确定项时明确说明假设，并输出执行步骤与最终结果。",
    }[score_band]
    payload = {
        "user_original_query": query,
        "judge_overall_score": score,
        "judge_next_action": summary.get("judge_next_action"),
        "actor_harness_output_evidence": actor_harness_output,
    }
    if final_scripts is not None:
        payload["final_scripts"] = final_scripts
    return "\n\n".join(
        [
            execute_instruction,
            "下面给出演员 Harness 当前输出证据与 final_scripts。无需再次生成剧本，请直接依据 final_scripts 进入真实执行，并优先复用其中已经形成的步骤、风险提示、验证思路与工具判断。",
            json.dumps(payload, ensure_ascii=False, indent=2),
            "请开始真实执行，输出内容至少包含：1）执行步骤；2）关键观察；3）最终结果；4）若仍有残余风险，给出简短提示。",
        ]
    )


def decide_execution(summary: dict[str, Any]) -> ExecutionDecision:
    """根据剧本评分决定是否进入真实执行阶段。"""

    score = summary.get("judge_overall_score")
    if not isinstance(score, int):
        if isinstance(score, float):
            score = round(score)
        else:
            return ExecutionDecision(False, "blocked", None, "缺少 judge_overall_score，暂不进入真实执行")
    query = str(summary.get("query", "")).strip()
    if score > 85:
        return ExecutionDecision(True, "high", build_execute_prompt(query, summary, score, "high"), "总体评分高于 85，直接进入真实执行")
    if score >= 70:
        return ExecutionDecision(True, "medium", build_execute_prompt(query, summary, score, "medium"), "总体评分位于 70-85，按谨慎执行策略进入真实执行")
    return ExecutionDecision(False, "blocked", None, "总体评分低于 70，保持为重新生成执行剧本")


def run_writer_harness(args, mode: str, query: str, root_dir: Path) -> dict[str, Any]:
    """调用 `python -m writer_harness`，并以 JSON 模式解析结果。"""

    query_args, query_file_path = build_query_args(query)
    command = [
        sys.executable,
        "-m",
        "writer_harness",
        "--mode",
        mode,
        "--actor-backend",
        args.actor_backend,
        "--writer-backend",
        args.writer_backend,
        "--json",
    ]
    command.extend(query_args)
    if args.writer_backend == "openai-compatible":
        if not getattr(args, "writer_model", ""):
            raise ValueError("--writer-backend openai-compatible 时必须提供 --writer-model")
        command.extend(["--writer-model", args.writer_model])
        if getattr(args, "writer_base_url", None):
            command.extend(["--writer-base-url", args.writer_base_url])
        if getattr(args, "writer_api_key", None):
            command.extend(["--writer-api-key", args.writer_api_key])
    append_actor_harness_args(command, args, getattr(args, "actor_output_format", None))
    try:
        return run_json_command(command, root_dir)
    finally:
        if query_file_path:
            try:
                os.unlink(query_file_path)
            except OSError:
                pass


def run_execute_stage(args, execution_prompt: str, root_dir: Path) -> dict[str, Any]:
    """以 vanilla 模式调用演员 Harness，进入真实执行阶段。"""

    query_args, query_file_path = build_query_args(execution_prompt)
    command = [
        sys.executable,
        "-m",
        "writer_harness",
        "--mode",
        "vanilla",
        "--actor-backend",
        args.actor_backend,
        "--writer-backend",
        "openai-compatible",
        "--json",
    ]
    command.extend(query_args)
    append_actor_harness_args(command, args, getattr(args, "execute_output_format", None))
    try:
        result = run_json_command(command, root_dir)
        if getattr(args, "execute_output_format", None) == "stream-json":
            trace = extract_tool_trace_from_stream_json(str(result.get("stdout", "")))
            result["tool_trace"] = trace
            assistant_text = trace.get("assistant_text") or ""
            if assistant_text:
                result["stdout"] = assistant_text
        return result
    finally:
        if query_file_path:
            try:
                os.unlink(query_file_path)
            except OSError:
                pass


def build_parser() -> argparse.ArgumentParser:
    """构建 writer 在线执行脚本参数解析器。

    参数说明：
    - --mode: 决定是否直连演员 Harness，或先生成执行剧本；其中 vanilla 对应不启用 writer_harness。
    - --actor-backend: 当前固定为真实 OpenHarness 执行后端。
    - --writer-backend: 当前固定为真实 OpenAI-compatible 编剧模型后端。
    - --execute-output-format: 单独控制真实执行阶段输出格式，常用于打开 stream-json 轨迹抽取。
    """

    parser = argparse.ArgumentParser(
        description="Run online writer_harness interaction without ground truth.",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument("--query", required=True, help="在线任务原始 query")
    parser.add_argument("--mode", choices=["vanilla", "writer_harness"], default="writer_harness", help="演员 Harness 输入策略")
    parser.add_argument("--actor-backend", choices=["openharness"], default="openharness", help="演员 Harness 执行后端")
    parser.add_argument("--writer-backend", choices=["openai-compatible"], default="openai-compatible", help="编剧 Harness 执行后端")
    parser.add_argument("--writer-model", default=os.environ.get("WRITER_MODEL", ""), help="编剧 Harness 模型名，openai-compatible 模式必填")
    parser.add_argument("--writer-base-url", default=os.environ.get("WRITER_BASE_URL"), help="编剧 Harness base_url")
    parser.add_argument("--writer-api-key", default=os.environ.get("WRITER_API_KEY"), help="编剧 Harness api_key")
    parser.add_argument("--oh-bin", default="oh", help="OpenHarness CLI 可执行文件名或路径")
    parser.add_argument("--openharness-src", default=None, help="OpenHarness 源码 src 目录")
    parser.add_argument("--oh-real-run", action="store_true", help="默认 dry-run；开启后执行真实 OpenHarness")
    parser.add_argument("--actor-model", default=os.environ.get("ACTOR_MODEL"), help="覆盖演员 Harness 模型")
    parser.add_argument("--actor-base-url", default=os.environ.get("ACTOR_BASE_URL"), help="覆盖演员 Harness base_url")
    parser.add_argument("--actor-api-key", default=os.environ.get("ACTOR_API_KEY"), help="覆盖演员 Harness api_key")
    parser.add_argument("--actor-api-format", default=os.environ.get("ACTOR_API_FORMAT"), help="覆盖演员 Harness api_format")
    parser.add_argument("--actor-output-format", default=None, help="覆盖剧本生成阶段演员 Harness 输出格式，如 text / json / stream-json")
    parser.add_argument("--execute-output-format", default=None, help="覆盖真实执行阶段演员 Harness 输出格式，如 text / json / stream-json")
    parser.add_argument("--skip-execute", action="store_true", help="仅运行剧本生成与评估，不进入真实执行阶段")
    parser.add_argument("--json", action="store_true", help="以 JSON 摘要形式输出结果")
    parser.add_argument("--print-preview", type=int, default=800, help="命令行打印输入/输出预览长度；0 表示不打印")
    return parser


def preview_text(text: str, limit: int) -> str:
    """生成命令行可读预览，避免一次打印完整长文本。"""

    if limit <= 0:
        return ""
    normalized = text.replace("\r\n", "\n")
    if len(normalized) <= limit:
        return normalized
    return normalized[:limit].rstrip() + "\n...<truncated>"


def append_actor_harness_args(command: list[str], args, output_format: str | None = None) -> list[str]:
    """把 actor_harness 运行参数追加到子进程命令中。"""

    if getattr(args, "actor_backend", None) == "openharness":
        command.extend(["--oh-bin", args.oh_bin])
        if getattr(args, "openharness_src", None):
            command.extend(["--openharness-src", args.openharness_src])
        if getattr(args, "oh_real_run", False):
            command.append("--oh-real-run")
        if getattr(args, "actor_model", None):
            command.extend(["--actor-model", args.actor_model])
        if getattr(args, "actor_base_url", None):
            command.extend(["--actor-base-url", args.actor_base_url])
        if getattr(args, "actor_api_key", None):
            command.extend(["--actor-api-key", args.actor_api_key])
        if getattr(args, "actor_api_format", None):
            command.extend(["--actor-api-format", args.actor_api_format])
        resolved_output_format = output_format if output_format is not None else getattr(args, "actor_output_format", None)
        if resolved_output_format:
            command.extend(["--actor-output-format", resolved_output_format])
    return command


def run_json_command(command: list[str], root_dir: Path) -> dict:
    """执行子进程命令，并从 stdout 中提取 JSON 结果。"""

    env = os.environ.copy()
    env.setdefault("PYTHONIOENCODING", "utf-8")
    env.setdefault("PYTHONUTF8", "1")
    completed = subprocess.run(command, cwd=root_dir, capture_output=True, text=True, encoding="utf-8", errors="replace", env=env)
    if completed.returncode != 0:
        raise RuntimeError(f"command exited with code {completed.returncode}\nstderr: {completed.stderr}")

    stdout = completed.stdout
    start = stdout.find("{")
    end = stdout.rfind("}")
    if start < 0 or end < start:
        raise RuntimeError(f"Could not find JSON output in command stdout:\n{stdout}")
    return json.loads(stdout[start : end + 1])


def build_query_args(query: str) -> tuple[list[str], str | None]:
    """根据 query 长度决定使用直接传参还是临时文件传参。"""

    if len(query) <= 4000:
        return ["--query", query], None
    temp_file = tempfile.NamedTemporaryFile("w", suffix=".txt", delete=False, encoding="utf-8")
    temp_file.write(query)
    temp_file.close()
    return ["--query-file", temp_file.name], temp_file.name


def build_regeneration_suggestions(judgment: dict[str, Any]) -> list[str]:
    """当剧本不完整时，生成建议补全的剧本节区提示。"""

    missing_checks = judgment.get("missing_checks", []) or []
    mapping = {
        "task_goal_or_objective": "补充任务目标或目标边界说明",
        "expected_output": "补充预期输出或交付物说明",
        "success_criteria": "补充成功标准或最小完成条件",
        "known_conditions": "补充当前已知条件、约束和输入前提",
        "unknown_or_risk": "补充未知条件、潜在风险或缺失工具说明",
        "recommended_steps": "补充建议执行步骤",
        "validation_steps": "补充验证步骤或结果确认方式",
        "judgment_rationale": "补充难度判断或执行建议的依据",
        "explicit_suggestion": "补充明确的下一步建议：执行 / 谨慎执行 / 再次生成剧本",
    }
    return [mapping[item] for item in missing_checks if item in mapping]


def summarize_writer_result(result: dict[str, Any]) -> dict[str, Any]:
    """把 writer_harness 返回结果整理成更直观的在线摘要结构。"""

    judgment = result.get("online_completeness_judgment") or {}
    next_action = judgment.get("next_action", "unknown")
    suggestions = build_regeneration_suggestions(judgment)
    judge_evaluation = result.get("judge_completeness_evaluation") or {}
    script_report = copy.deepcopy(result.get("script_report")) if isinstance(result.get("script_report"), dict) else None
    final_script_report = copy.deepcopy(result.get("final_script_report")) if isinstance(result.get("final_script_report"), dict) else None
    if final_script_report is None and script_report is not None:
        final_script_report = copy.deepcopy(script_report)
    actor_harness_output = str(result.get("stdout", ""))
    capability_match = match_openharness_capabilities(
        str(result.get("query") or ""),
        final_script_report or script_report,
        actor_harness_output,
    )
    if final_script_report and isinstance(final_script_report.get("difficulty_profile"), dict):
        final_script_report["difficulty_profile"]["available_tools"] = capability_match["available_tools"]
        final_script_report["difficulty_profile"]["missing_tools"] = capability_match["missing_tools"]
        final_script_report["difficulty_profile"]["required_capabilities"] = capability_match["required_capabilities"]
    return {
        "mode": result.get("mode"),
        "ok": result.get("ok"),
        "return_code": result.get("return_code"),
        "stderr": result.get("stderr", ""),
        "final_prompt": result.get("final_prompt", ""),
        "actor_harness_output": actor_harness_output,
        "script_report": script_report,
        "actor_harness_report": result.get("actor_harness_report"),
        "final_script_report": final_script_report,
        "script_report_source": result.get("script_report_source"),
        "script_report_object_origin": result.get("script_report_object_origin"),
        "script_report_transport_path": result.get("script_report_transport_path"),
        "online_completeness_judgment": judgment,
        "judge_completeness_evaluation": judge_evaluation,
        "judge_overall_score": judge_evaluation.get("overall_score") if isinstance(judge_evaluation, dict) else None,
        "judge_next_action": judge_evaluation.get("next_action") if isinstance(judge_evaluation, dict) else None,
        "next_action": next_action,
        "regeneration_suggestions": suggestions,
        "capability_match": capability_match,
        "execution": {
            "executed": False,
            "status": "not_started",
            "score_band": None,
            "decision_rationale": "尚未进入真实执行阶段",
            "prompt": None,
            "execution_output": "",
            "stdout": "",
            "stderr": "",
            "return_code": None,
            "result_ok": None,
        },
    }


def print_writer_summary(summary: dict[str, Any], query: str, preview_limit: int) -> None:
    """以更适合人读的方式打印 writer 在线执行摘要。"""

    ConsoleUI.print_header("writer execution summary")
    print(f"mode: {summary.get('mode', 'unknown')}")
    print(f"execution_ok: {summary.get('ok')}")
    print(f"return_code: {summary.get('return_code')}")
    print(f"next_action: {summary.get('next_action', 'unknown')}")
    print(f"script_report_source: {summary.get('script_report_source') or 'none'}")
    if summary.get("script_report_object_origin"):
        print(f"script_report_object_origin: {summary.get('script_report_object_origin')}")
        print(f"script_report_transport_path: {summary.get('script_report_transport_path')}")

    if preview_limit > 0:
        print(ConsoleUI.styled("-" * 80, Style.DIM))
        ConsoleUI.print_header("query preview")
        print(preview_text(query, preview_limit))

        final_prompt = summary.get("final_prompt", "")
        if final_prompt:
            print(ConsoleUI.styled("-" * 80, Style.DIM))
            ConsoleUI.print_header("first_input_to_actor_harness preview")
            print(preview_text(final_prompt, preview_limit))

        actor_output = summary.get("actor_harness_output", "")
        print(ConsoleUI.styled("-" * 80, Style.DIM))
        ConsoleUI.print_header("actor_harness_output preview")
        print(preview_text(actor_output, preview_limit))

        script_report = summary.get("script_report")
        if script_report:
            print(ConsoleUI.styled("-" * 80, Style.DIM))
            ConsoleUI.print_header("script_report preview")
            print(preview_text(json.dumps(script_report, ensure_ascii=False, indent=2), preview_limit))

        final_script_report = summary.get("final_script_report")
        if final_script_report:
            print(ConsoleUI.styled("-" * 80, Style.DIM))
            ConsoleUI.print_header("final_script_report preview")
            print(preview_text(json.dumps(final_script_report, ensure_ascii=False, indent=2), preview_limit))

        stderr_text = summary.get("stderr", "")
        if stderr_text:
            print(ConsoleUI.styled("-" * 80, Style.DIM))
            ConsoleUI.print_error("stderr preview")
            print(preview_text(stderr_text, preview_limit))

    judgment = summary.get("online_completeness_judgment") or {}
    print(ConsoleUI.styled("-" * 80, Style.DIM))
    if judgment.get("is_complete") is True:
        ConsoleUI.print_success("online_completeness_judgment")
    elif judgment:
        ConsoleUI.print_warning("online_completeness_judgment")
    else:
        ConsoleUI.print_warning("online_completeness_judgment")
    print(json.dumps(judgment, ensure_ascii=False, indent=2))

    judge_evaluation = summary.get("judge_completeness_evaluation") or {}
    if judge_evaluation:
        print(ConsoleUI.styled("-" * 80, Style.DIM))
        ConsoleUI.print_header("judge_completeness_evaluation")
        print(json.dumps(judge_evaluation, ensure_ascii=False, indent=2))

    suggestions = summary.get("regeneration_suggestions") or []
    if suggestions:
        print(ConsoleUI.styled("-" * 80, Style.DIM))
        ConsoleUI.print_warning("regeneration_suggestions")
        for item in suggestions:
            print(f"- {item}")

    execution = summary.get("execution") or {}
    if execution.get("status") and execution.get("status") != "not_started":
        print(ConsoleUI.styled("-" * 80, Style.DIM))
        ConsoleUI.print_header("execution stage")
        print(json.dumps(execution, ensure_ascii=False, indent=2))


def main() -> None:
    """运行 writer 在线单条交互，并根据剧本评分决定是否进入真实执行。"""

    parser = build_parser()
    args = parser.parse_args()
    root_dir = Path(__file__).resolve().parent
    started_at = time.perf_counter()
    result = run_writer_harness(args, args.mode, args.query, root_dir)
    result["query"] = args.query
    summary = summarize_writer_result(result)
    summary["query"] = args.query
    decision = decide_execution(summary)
    summary["execution"]["score_band"] = decision.score_band
    summary["execution"]["decision_rationale"] = decision.rationale
    if not args.skip_execute and decision.should_execute and decision.execution_prompt:
        execution_result = run_execute_stage(args, decision.execution_prompt, root_dir)
        summary["execution"] = {
            "executed": True,
            "status": "done" if execution_result.get("ok") else "failed",
            "score_band": decision.score_band,
            "decision_rationale": decision.rationale,
            "prompt": decision.execution_prompt,
            "prompt_inputs": {
                "user_original_query": args.query,
                "judge_overall_score": summary.get("judge_overall_score"),
                "judge_next_action": summary.get("judge_next_action"),
                "actor_harness_output": summary.get("actor_harness_output", ""),
                "final_scripts": summary.get("final_script_report"),
            },
            "execution_output": execution_result.get("stdout", ""),
            "stdout": execution_result.get("stdout", ""),
            "stderr": execution_result.get("stderr", ""),
            "return_code": execution_result.get("return_code"),
            "result_ok": execution_result.get("ok"),
            "final_prompt": execution_result.get("final_prompt", ""),
            "tool_trace": execution_result.get("tool_trace"),
        }
    elif decision.execution_prompt:
        summary["execution"]["prompt"] = decision.execution_prompt
        summary["execution"]["prompt_inputs"] = {
            "user_original_query": args.query,
            "judge_overall_score": summary.get("judge_overall_score"),
            "judge_next_action": summary.get("judge_next_action"),
            "actor_harness_output": summary.get("actor_harness_output", ""),
            "final_scripts": summary.get("final_script_report"),
        }
        summary["execution"]["status"] = "skipped"
    elapsed_seconds = round(time.perf_counter() - started_at, 4)
    summary["timing"] = {
        "total_seconds": elapsed_seconds,
        "script_composed_by_actor": args.mode == "writer_harness",
        "prompt_composed": True,
        "execution_attempted": bool(summary.get("execution", {}).get("executed")),
    }

    if args.json:
        print(json.dumps(summary, ensure_ascii=False, indent=2))
        return

    print_writer_summary(summary, args.query, args.print_preview)


if __name__ == "__main__":
    main()
