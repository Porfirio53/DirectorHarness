"""面向 UI 的多轮 writer_harness 执行入口。

文件定位：
1. 该文件不是直接替代 OpenHarness 原生会话机制，而是在现有 `writer_excute.py`
   的单轮执行能力外，补上一层“面向 UI 的多轮会话适配层”；
2. 主要解决当前前端只有聊天记录展示、但后端执行仍然是单轮调用的问题；
3. 通过 session 文件保存历史 turn，并在新一轮请求到来时把最近历史压缩注入到
   当前 query 中，从而提供可控的多轮上下文能力；
4. 返回结构尽量保持与现有 UI 兼容，继续输出 `vanilla` / `writer_harness` 两种策略
   的结果，方便前端低成本切换接入。

适用边界：
- 适合当前 UI 这种“前端管理聊天记录、后端按请求执行”的集成模式；
- 适合作为过渡方案，在不重写 `writer_harness` / OpenHarness 原生会话机制的前提下
  快速补齐多轮体验；
- 若未来要深度接入 OpenHarness 原生 `--continue` / `--resume`，本文件仍可作为 UI
  适配层保留，但历史注入逻辑可以逐步收缩。
"""

from __future__ import annotations

import argparse
import json
import os
import time
from pathlib import Path
from typing import Any
from uuid import uuid4

import writer_excute as single_turn
from writer_harness.llm_clients import OpenAICompatibleClient
from writer_harness.writer_harness import WriterHarness


SESSION_SCHEMA_VERSION = 3
DEFAULT_HISTORY_LIMIT = 8
DEFAULT_SESSION_DIR = Path(__file__).resolve().parent / ".writer_harness_sessions"


def now_seconds() -> float:
    """返回统一格式的秒级时间戳。

    设计原因：
    - session 文件里需要记录 created_at / updated_at / turn 时间；
    - 这里统一保留 4 位小数，便于调试时看出先后顺序，同时避免浮点值过长。
    """

    return round(time.time(), 4)


def get_session_dir(args) -> Path:
    """确定多轮会话状态目录，并在首次使用时自动创建。

    关键参数：
    - args.session_dir: 允许 UI 或命令行显式覆盖 session 保存目录；
    - DEFAULT_SESSION_DIR: 默认落在脚本同级目录下的 `.writer_harness_sessions/`。

    返回值：
    - 返回最终可写入的目录路径，后续 session 文件全部保存在这里。
    """

    path = Path(args.session_dir or DEFAULT_SESSION_DIR)
    path.mkdir(parents=True, exist_ok=True)
    return path


def sanitize_session_id(value: str | None) -> str:
    """清洗或生成 session_id，确保可安全映射为本地文件名。

    设计原因：
    - UI 传入的 session_id 可能包含空格、特殊字符或为空；
    - 该 ID 会直接影响 session 文件名，所以这里仅保留字母、数字、`-`、`_`；
    - 若清洗后为空，则自动回退为随机 ID。
    """

    if not value:
        return uuid4().hex[:12]
    normalized = "".join(char for char in value.strip() if char.isalnum() or char in {"-", "_"})
    return normalized or uuid4().hex[:12]


def get_session_path(args, session_id: str) -> Path:
    """根据 session_id 映射到单个会话文件路径。"""

    return get_session_dir(args) / f"session-{session_id}.json"


def load_session(args, session_id: str) -> dict[str, Any]:
    """加载某个会话的持久化状态。

    返回结构说明：
    - schema_version: session 文件结构版本，便于未来兼容迁移；
    - session_id: 当前 UI 会话标识；
    - turns: 已完成轮次的历史摘要；
    - metadata: 额外的运行信息，例如最近一次使用的 mode；

    若文件不存在，会返回一个“空会话骨架”，让上层逻辑统一处理。
    """

    path = get_session_path(args, session_id)
    if not path.exists():
        return {
            "schema_version": SESSION_SCHEMA_VERSION,
            "session_id": session_id,
            "created_at": now_seconds(),
            "updated_at": now_seconds(),
            "turns": [],
            "metadata": {},
        }
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data.get("turns"), list):
        data["turns"] = []
    data.setdefault("schema_version", SESSION_SCHEMA_VERSION)
    data.setdefault("session_id", session_id)
    data.setdefault("created_at", now_seconds())
    data.setdefault("metadata", {})
    return data


def save_session(args, session: dict[str, Any]) -> None:
    """把多轮会话状态写回磁盘。

    设计选择：
    - 当前使用 JSON 单文件持久化，方便调试和人工检查；
    - 每次写入前都会刷新 `updated_at`，便于 UI 或后续后台任务判断最近活跃时间。
    """

    session["updated_at"] = now_seconds()
    path = get_session_path(args, str(session["session_id"]))
    path.write_text(json.dumps(session, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def reset_session(args, session_id: str) -> dict[str, Any]:
    """清空指定 session 的后端历史状态。

    与 UI 的关系：
    - 前端“清除对话历史”如果只调用 `setTurns([])`，只会清空显示；
    - 若联动本函数，对应 session 文件也会被删除，下一轮将从真正的空上下文开始。
    """

    path = get_session_path(args, session_id)
    if path.exists():
        path.unlink()
    return {
        "ok": True,
        "action": "reset",
        "session_id": session_id,
        "session_reset": True,
        "turns": [],
    }


def delete_session_turn(args, session_id: str, turn_id: str) -> dict[str, Any]:
    session = load_session(args, session_id)
    turns = session.get("turns", [])
    retained_turns = [turn for turn in turns if str(turn.get("turn_id")) != turn_id]
    deleted = len(retained_turns) != len(turns)
    if deleted and not retained_turns:
        path = get_session_path(args, session_id)
        if path.exists():
            path.unlink()
    elif deleted:
        session["turns"] = retained_turns
        save_session(args, session)
    return {
        "ok": deleted,
        "action": "delete_turn",
        "session_id": session_id,
        "turn_id": turn_id,
        "deleted": deleted,
        "turns": retained_turns,
        "remaining_turn_count": len(retained_turns),
        "removed_fields": ["input", "output", "response", "assistant_output", "effective_query", "response_summary"] if deleted else [],
    }


def compact_text(value: str, limit: int) -> str:
    """截断历史文本，避免多轮上下文无限膨胀。

    关键参数：
    - value: 原始 query 或 assistant 输出；
    - limit: 单段文本允许注入到 prompt 的最大字符数。

    设计原因：
    - 多轮上下文如果不裁剪，很容易让 query 迅速变长；
    - 这里做的是“保守压缩”，只截断并保留前缀，适合 UI 场景下的上下文提醒。
    """

    text = str(value or "").strip()
    if limit <= 0 or len(text) <= limit:
        return text
    return text[:limit].rstrip() + "\n...<truncated>"


def get_execution_output(summary: dict[str, Any]) -> str:
    """从单轮执行摘要中提取最适合作为“助手回复”的文本。

    提取优先级：
    1. execution.execution_output
    2. execution.stdout

    设计原因：
    - session 历史不需要完整结构化结果，只需要一份可供后续轮次参考的自然语言回复；
    - 未实际执行的剧本中间产物不作为后续对话事实注入。
    """

    execution = summary.get("execution") if isinstance(summary.get("execution"), dict) else {}
    return str(execution.get("execution_output") or execution.get("stdout") or "")


def get_turn_writer_result(turn: dict[str, Any]) -> dict[str, Any]:
    response = turn.get("response") if isinstance(turn.get("response"), dict) else {}
    result = response.get("writer_harness") if isinstance(response.get("writer_harness"), dict) else {}
    return result


def get_previous_script(session: dict[str, Any]) -> tuple[dict[str, Any] | None, dict[str, Any]]:
    turns = session.get("turns") if isinstance(session.get("turns"), list) else []
    if not turns:
        return None, {}
    previous_turn = turns[-1] if isinstance(turns[-1], dict) else {}
    previous_result = get_turn_writer_result(previous_turn)
    previous_script = previous_result.get("final_script_report") or previous_result.get("script_report")
    return (previous_script if isinstance(previous_script, dict) else None), previous_turn


def build_plan_decision(session: dict[str, Any], query: str, args=None) -> dict[str, Any]:
    previous_script, previous_turn = get_previous_script(session)
    history_context = build_history_context(session.get("turns", []), getattr(args, "history_limit", DEFAULT_HISTORY_LIMIT), getattr(args, "history_content_limit", 1200))
    if args is None or not getattr(args, "writer_model", ""):
        return {
            "action": "new_plan",
            "turn_intent": "execute_task",
            "effective_task_goal": query,
            "reason": "未配置编剧模型，无法进行模型驱动的多轮计划判断。",
            "plan_reference_usage": "不参考上一轮计划。",
            "prior_turn_id": previous_turn.get("turn_id"),
        }
    try:
        client = OpenAICompatibleClient(
            model=args.writer_model,
            base_url=getattr(args, "writer_base_url", None),
            api_key=getattr(args, "writer_api_key", None),
        )
        judgment = WriterHarness(client).judge_multiturn_plan_transition(query, history_context, previous_script)
    except Exception as exc:
        return {
            "action": "new_plan",
            "turn_intent": "execute_task",
            "effective_task_goal": query,
            "reason": f"多轮计划判断模型不可用，本轮不复用历史计划：{exc}",
            "plan_reference_usage": "不参考上一轮计划。",
            "prior_turn_id": previous_turn.get("turn_id"),
        }
    action_map = {
        "inherit_previous_plan": "reuse_existing_plan",
        "refine_plan": "refine_existing_plan",
        "split_plan": "split_existing_plan",
        "continue_plan": "continue_existing_plan",
        "new_plan": "new_plan",
    }
    effective_task_goal = normalize_effective_task_goal(
        judgment.get("effective_task_goal"),
        previous_script,
        query,
    )
    return {
        "action": action_map.get(str(judgment.get("plan_action")), "new_plan"),
        "turn_intent": str(judgment.get("turn_intent") or "execute_task"),
        "effective_task_goal": effective_task_goal,
        "reason": str(judgment.get("reason") or "编剧模型未提供判断依据。"),
        "plan_reference_usage": str(judgment.get("plan_reference_usage") or "未说明计划引用方式。"),
        "prior_turn_id": previous_turn.get("turn_id"),
    }


def normalize_effective_task_goal(value: Any, previous_script: dict[str, Any] | None, query: str) -> str:
    goal = str(value or "").strip()
    internal_markers = (
        "writer harness",
        "actor harness",
        "json schema",
        "execution script",
        "script generation",
        "revise the script",
        "生成执行剧本",
        "剧本生成",
        "修订剧本",
        "计划的计划",
    )
    if goal and not any(marker in goal.lower() for marker in internal_markers):
        return goal
    task_profile = previous_script.get("task_profile") if isinstance(previous_script, dict) else {}
    previous_goal = task_profile.get("task_goal") if isinstance(task_profile, dict) else ""
    return str(previous_goal or query).strip()


def build_plan_context(session: dict[str, Any], plan_decision: dict[str, Any]) -> str:
    if plan_decision.get("action") not in {"reuse_existing_plan", "refine_existing_plan", "split_existing_plan", "continue_existing_plan"}:
        return ""
    turns = session.get("turns") if isinstance(session.get("turns"), list) else []
    if not turns or not isinstance(turns[-1], dict):
        return ""
    previous_result = get_turn_writer_result(turns[-1])
    previous_script = previous_result.get("final_script_report") or previous_result.get("script_report")
    if not isinstance(previous_script, dict):
        return ""
    return "\n\n".join(
        [
            f"编剧已判断本轮计划处理方式为 {plan_decision.get('action')}。以下是上一轮计划；请按该判断继承、更新、细化或接续相关部分，保留仍然有效的业务目标、步骤、风险与验证方式。",
            json.dumps(previous_script, ensure_ascii=False, indent=2),
            f"当前用户业务目标：{plan_decision.get('effective_task_goal', '')}。不得把“补全/修改计划”写成任务目标；请输出服务于实际任务的更新后执行计划。",
        ]
    )


def build_history_context(turns: list[dict[str, Any]], limit: int, content_limit: int) -> str:
    """把最近若干轮历史 turn 压缩成可注入当前 query 的上下文文本。

    关键参数：
    - turns: session 中已完成的历史轮次；
    - limit: 最多注入多少轮历史；
    - content_limit: 每轮 query / answer 的最大注入字符数。

    输出形式：
    - 使用“历史轮次 N / 用户 / 助手”的显式结构，便于模型理解；
    - 只保留最近若干轮，而不是把完整 session 全量拼进去。
    """

    recent_turns = turns[-limit:] if limit > 0 else []
    if not recent_turns:
        return ""
    blocks = ["以下是同一 UI 会话中的历史对话摘要。请把它作为上下文参考，但本轮仍以最后的用户请求为准。"]
    for index, turn in enumerate(recent_turns, 1):
        input_data = turn.get("input") if isinstance(turn.get("input"), dict) else {}
        output_data = turn.get("output") if isinstance(turn.get("output"), dict) else {}
        query = compact_text(str(input_data.get("query") or turn.get("query") or ""), content_limit)
        answer = compact_text(str(output_data.get("final_output") or turn.get("assistant_output") or ""), content_limit)
        blocks.append(f"[历史轮次 {index}]\n用户：{query}\n助手：{answer or '未产生可用输出'}")
    return "\n\n".join(blocks)


def build_multiturn_query(query: str, session: dict[str, Any], args, plan_decision: dict[str, Any]) -> str:
    """构造本轮真正送入执行层的 query。

    行为说明：
    - 如果 `--disable-history-context` 打开，则直接使用当前 query；
    - 否则先把最近若干轮历史压缩成上下文，再把当前用户请求附在后面。

    这一步是当前多轮机制的核心：
    - UI 仍然按“单次提交”触发后端；
    - 但后端通过拼接历史摘要，让模型感知这是同一会话里的连续提问。
    """

    plan_context = build_plan_context(session, plan_decision)
    if args.disable_history_context:
        return "\n\n".join([part for part in [plan_context, "当前用户请求：", query] if part]) if plan_context else query
    history_context = build_history_context(session.get("turns", []), args.history_limit, args.history_content_limit)
    # #region debug-point C:history-injection
    exec("try:\n import json as _j, urllib.request as _u\n _p='.dbg/multiturn-permissions.env'; _c=open(_p, encoding='utf-8').read(); _url=next((x.split('=',1)[1] for x in _c.splitlines() if x.startswith('DEBUG_SERVER_URL=')), 'http://127.0.0.1:7777/event'); _sid=next((x.split('=',1)[1] for x in _c.splitlines() if x.startswith('DEBUG_SESSION_ID=')), 'multiturn-permissions'); _u.urlopen(_u.Request(_url, data=_j.dumps({'sessionId': _sid, 'runId': 'pre-fix', 'hypothesisId': 'C', 'location': 'writer_excute_multiturn.py:241', 'msg': '[DEBUG] history context assembled', 'data': {'turnCount': len(session.get('turns', [])), 'historyEnabled': bool(history_context), 'containsAccessDenial': 'can’t access your local Windows path' in history_context or \"can't access your local Windows path\" in history_context}, 'ts': int(time.time() * 1000)}).encode(), headers={'Content-Type': 'application/json'}), timeout=1).read()\nexcept: pass")
    # #endregion
    intent_context = ""
    if plan_decision.get("turn_intent") == "user_plan_generation":
        intent_context = "本轮用户的交付物是面向实际业务目标的用户可读计划。不要输出 Writer Harness 的内部 JSON 执行剧本、schema 或计划完整性评估；请直接形成用户要求的计划。"
    return "\n\n".join([part for part in [history_context, plan_context, intent_context, "当前用户请求：", query] if part])


def build_single_turn_args(args, query: str, mode: str) -> argparse.Namespace:
    """把多轮脚本参数投影成旧单轮脚本可直接复用的参数对象。

    设计原因：
    - 现有 `writer_excute.py` 已经沉淀了完整的单轮执行逻辑；
    - 为了降低风险，这里不重写执行链，而是构造一个兼容的 Namespace 交给旧逻辑。

    关键参数：
    - query: 当前真正执行的文本，可能已注入多轮历史；
    - mode: `vanilla` 或 `writer_harness`，一次只运行一种策略。
    """

    return argparse.Namespace(
        query=query,
        mode=mode,
        actor_backend=args.actor_backend,
        writer_backend=args.writer_backend,
        writer_model=args.writer_model,
        writer_base_url=args.writer_base_url,
        writer_api_key=args.writer_api_key,
        oh_bin=args.oh_bin,
        openharness_src=args.openharness_src,
        director_harness_enabled=args.director_harness_enabled,
        director_log_path=args.director_log_path,
        oh_real_run=args.oh_real_run,
        actor_model=args.actor_model,
        actor_base_url=args.actor_base_url,
        actor_api_key=args.actor_api_key,
        actor_api_format=args.actor_api_format,
        actor_output_format=args.actor_output_format,
        execute_output_format=args.execute_output_format,
        skip_execute=args.skip_execute,
        json=True,
        print_preview=0,
    )


def run_single_mode(
    args,
    query: str,
    user_query: str,
    mode: str,
    root_dir: Path,
    plan_decision: dict[str, Any] | None = None,
    previous_script: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """运行单个策略的一轮执行，并复用旧单轮脚本的全部核心流程。

    内部流程：
    1. 调用 `run_writer_harness` 获取首次 actor 输出；
    2. 调用 `summarize_writer_result` 整理中间结果；
    3. 调用 `decide_execution` 判断是否进入真实执行；
    4. 如满足条件，再调用 `run_execute_stage` 进入真实执行阶段。

    返回值：
    - 返回结构尽量与旧 `writer_excute.py` 保持一致，便于前端继续复用现有渲染逻辑。
    """

    stage_started_at = time.perf_counter()
    single_args = build_single_turn_args(args, query, mode)
    # #region debug-point C:writer-harness-invocation
    exec("try:\n import json as _j, urllib.request as _u\n _p='.dbg/plan-inheritance-writer.env'; _c=open(_p, encoding='utf-8').read(); _url=next((x.split('=',1)[1] for x in _c.splitlines() if x.startswith('DEBUG_SERVER_URL=')), 'http://127.0.0.1:7777/event'); _sid=next((x.split('=',1)[1] for x in _c.splitlines() if x.startswith('DEBUG_SESSION_ID=')), 'plan-inheritance-writer'); _u.urlopen(_u.Request(_url, data=_j.dumps({'sessionId': _sid, 'runId': 'pre-fix', 'hypothesisId': 'C', 'location': 'writer_excute_multiturn.py:388', 'msg': '[DEBUG] run_writer_harness invoked', 'data': {'mode': mode, 'queryMatchesUserQuery': query == user_query, 'queryLength': len(query)}, 'ts': int(time.time() * 1000)}).encode(), headers={'Content-Type': 'application/json'}), timeout=1).read()\nexcept: pass")
    # #endregion
    result = single_turn.run_writer_harness(single_args, mode, query, root_dir)
    result["query"] = query
    summary = single_turn.summarize_writer_result(result)
    summary["query"] = user_query
    if plan_decision and mode == "writer_harness":
        summary["execution_plan_context"] = {
            "action": plan_decision.get("action"),
            "effective_task_goal": plan_decision.get("effective_task_goal"),
            "plan_reference_usage": plan_decision.get("plan_reference_usage"),
            "prior_turn_id": plan_decision.get("prior_turn_id"),
            "prior_final_script": previous_script,
        }
    decision = single_turn.decide_execution(summary)
    script_stage_seconds = round(time.perf_counter() - stage_started_at, 4)
    summary["execution"]["score_band"] = decision.score_band
    summary["execution"]["decision_rationale"] = decision.rationale
    if not args.skip_execute and decision.should_execute and decision.execution_prompt:
        execution_started_at = time.perf_counter()
        execution_result = single_turn.run_execute_stage(single_args, decision.execution_prompt, root_dir)
        summary["execution"] = {
            "executed": True,
            "status": "done" if execution_result.get("ok") else "failed",
            "score_band": decision.score_band,
            "decision_rationale": decision.rationale,
            "prompt": decision.execution_prompt,
            "prompt_inputs": {
                "user_original_query": query,
                "judge_overall_score": summary.get("judge_overall_score"),
                "judge_next_action": summary.get("judge_next_action"),
                "actor_harness_output": summary.get("actor_harness_output", ""),
                "final_scripts": summary.get("final_script_report"),
                "execution_plan_context": summary.get("execution_plan_context"),
            },
            "execution_output": execution_result.get("stdout", ""),
            "stdout": execution_result.get("stdout", ""),
            "stderr": execution_result.get("stderr", ""),
            "return_code": execution_result.get("return_code"),
            "result_ok": execution_result.get("ok"),
            "final_prompt": execution_result.get("final_prompt", ""),
            "tool_trace": execution_result.get("tool_trace"),
            "execution_seconds": round(time.perf_counter() - execution_started_at, 4),
        }
    elif decision.execution_prompt:
        summary["execution"]["prompt"] = decision.execution_prompt
        summary["execution"]["prompt_inputs"] = {
            "user_original_query": query,
            "judge_overall_score": summary.get("judge_overall_score"),
            "judge_next_action": summary.get("judge_next_action"),
            "actor_harness_output": summary.get("actor_harness_output", ""),
            "final_scripts": summary.get("final_script_report"),
            "execution_plan_context": summary.get("execution_plan_context"),
        }
        summary["execution"]["status"] = "skipped"
        summary["execution"]["execution_seconds"] = 0.0
    summary["timing"] = {"script_stage_seconds": script_stage_seconds}
    return summary


def run_user_plan_generation(args, effective_query: str, user_query: str, root_dir: Path) -> dict[str, Any]:
    stage_started_at = time.perf_counter()
    plan_prompt = "\n\n".join(
        [
            "用户明确要求交付业务计划。请只输出面向用户的计划、步骤、前置条件、风险与验证方式；不要调用工具、不要执行任务、不要输出 Writer Harness 内部 JSON、schema 或计划完整性评分。",
            effective_query,
        ]
    )
    single_args = build_single_turn_args(args, plan_prompt, "vanilla")
    result = single_turn.run_writer_harness(single_args, "vanilla", plan_prompt, root_dir)
    summary = single_turn.summarize_writer_result(result)
    summary["mode"] = "writer_harness"
    summary["query"] = user_query
    summary["execution"] = {
        "executed": False,
        "status": "skipped_plan_deliverable",
        "score_band": "plan_only",
        "decision_rationale": "本轮用户目标是生成业务计划，跳过 Writer Harness 内部剧本模板、完整性评估与真实执行。",
        "prompt": None,
        "execution_output": str(result.get("stdout", "")),
        "stdout": str(result.get("stdout", "")),
        "stderr": str(result.get("stderr", "")),
        "return_code": result.get("return_code"),
        "result_ok": result.get("ok"),
        "execution_seconds": 0.0,
    }
    summary["timing"] = {"script_stage_seconds": round(time.perf_counter() - stage_started_at, 4)}
    return summary


def build_turn_record(turn_id: str, user_query: str, effective_query: str, mode: str, response: dict[str, Any]) -> dict[str, Any]:
    """把本轮执行结果压缩成一个适合写入 session 的 turn 记录。

    字段说明：
    - turn_id: 前端当前轮唯一标识；
    - input / output: 用于下一轮轻量上下文注入的原始输入与最终执行输出；
    - response: 完整执行响应，用于会话审计和按需详情展示；
    - response_summary: 快速读取的轻量状态索引。
    """

    vanilla = response.get("vanilla") if isinstance(response.get("vanilla"), dict) else {}
    writer_harness = response.get("writer_harness") if isinstance(response.get("writer_harness"), dict) else {}
    selected_result = writer_harness if mode == "writer_harness" else vanilla
    assistant_output = get_execution_output(selected_result)
    execution = selected_result.get("execution") if isinstance(selected_result.get("execution"), dict) else {}
    return {
        "turn_id": turn_id,
        "created_at": now_seconds(),
        "mode": mode,
        "input": {
            "query": user_query,
            "effective_query": effective_query,
        },
        "output": {
            "final_output": assistant_output,
        },
        "response": response,
        "query": user_query,
        "effective_query": effective_query,
        "assistant_output": assistant_output,
        "response_summary": {
            "mode": mode,
            "execution_status": execution.get("status"),
            "executed": execution.get("executed"),
            "score_band": execution.get("score_band"),
            "plan_action": response.get("plan_decision", {}).get("action") if isinstance(response.get("plan_decision"), dict) else None,
            "vanilla_status": vanilla.get("execution", {}).get("status") if isinstance(vanilla.get("execution"), dict) else None,
            "writer_harness_status": writer_harness.get("execution", {}).get("status") if isinstance(writer_harness.get("execution"), dict) else None,
        },
    }


def run_multiturn(args) -> dict[str, Any]:
    """多轮执行主流程。

    主流程说明：
    1. 解析并清洗 session_id；
    2. 若指定 `--reset-session`，直接清空并返回；
    3. 读取当前 session 历史；
    4. 生成注入历史后的 effective_query；
    5. 按 `mode` 运行 `vanilla` / `writer_harness` / `both`；
    6. 把当前轮摘要写回 session；
    7. 返回兼容 UI 的 JSON 响应。

    与 UI 的接口语义：
    - `session_id`: 用于把多次提交绑定为一个会话；
    - `turn_id`: 用于把某次提交与前端聊天记录项一一对应；
    - `effective_query`: 方便调试历史上下文是否被正确注入；
    - `session.reset_supported`: 告诉前端可安全接入“清空历史并重置后端上下文”。
    """

    session_id = sanitize_session_id(args.session_id)
    if args.reset_session:
        return reset_session(args, session_id)
    if args.delete_turn_id:
        return delete_session_turn(args, session_id, args.delete_turn_id)
    session = load_session(args, session_id)
    root_dir = Path(__file__).resolve().parent
    # #region debug-point A:multiturn-entry
    exec("try:\n import json as _j, urllib.request as _u\n _p='.dbg/multiturn-permissions.env'; _c=open(_p, encoding='utf-8').read(); _url=next((x.split('=',1)[1] for x in _c.splitlines() if x.startswith('DEBUG_SERVER_URL=')), 'http://127.0.0.1:7777/event'); _sid=next((x.split('=',1)[1] for x in _c.splitlines() if x.startswith('DEBUG_SESSION_ID=')), 'multiturn-permissions'); _u.urlopen(_u.Request(_url, data=_j.dumps({'sessionId': _sid, 'runId': 'pre-fix', 'hypothesisId': 'A', 'location': 'writer_excute_multiturn.py:393', 'msg': '[DEBUG] multi-turn execution starts', 'data': {'cwd': str(root_dir), 'ohRealRun': args.oh_real_run, 'skipExecute': args.skip_execute, 'mode': args.mode}, 'ts': int(time.time() * 1000)}).encode(), headers={'Content-Type': 'application/json'}), timeout=1).read()\nexcept: pass")
    # #endregion
    started_at = time.perf_counter()
    user_query = args.query.strip()
    plan_decision_started_at = time.perf_counter()
    plan_decision = build_plan_decision(session, user_query, args)
    plan_decision_seconds = round(time.perf_counter() - plan_decision_started_at, 4)
    effective_query = build_multiturn_query(user_query, session, args, plan_decision)
    # #region debug-point A:plan-decision
    exec("try:\n import json as _j, urllib.request as _u\n _p='.dbg/plan-inheritance-writer.env'; _c=open(_p, encoding='utf-8').read(); _url=next((x.split('=',1)[1] for x in _c.splitlines() if x.startswith('DEBUG_SERVER_URL=')), 'http://127.0.0.1:7777/event'); _sid=next((x.split('=',1)[1] for x in _c.splitlines() if x.startswith('DEBUG_SESSION_ID=')), 'plan-inheritance-writer'); _u.urlopen(_u.Request(_url, data=_j.dumps({'sessionId': _sid, 'runId': 'pre-fix', 'hypothesisId': 'A', 'location': 'writer_excute_multiturn.py:582', 'msg': '[DEBUG] plan decision resolved', 'data': {'action': plan_decision.get('action'), 'turnIntent': plan_decision.get('turn_intent'), 'priorTurnId': plan_decision.get('prior_turn_id'), 'historyTurnCount': len(session.get('turns', [])), 'effectiveQueryDiffers': effective_query != user_query}, 'ts': int(time.time() * 1000)}).encode(), headers={'Content-Type': 'application/json'}), timeout=1).read()\nexcept: pass")
    # #endregion
    response: dict[str, Any] = {
        "ok": False,
        "query": user_query,
        "effective_query": effective_query,
        "session_id": session_id,
        "history_turn_count": len(session.get("turns", [])),
        "history_context_enabled": not args.disable_history_context,
        "plan_decision": plan_decision,
    }
    modes = ["vanilla", "writer_harness"] if args.mode == "both" else [args.mode]
    for mode in modes:
        try:
            # #region debug-point B:writer-route
            exec("try:\n import json as _j, urllib.request as _u\n _p='.dbg/plan-inheritance-writer.env'; _c=open(_p, encoding='utf-8').read(); _url=next((x.split('=',1)[1] for x in _c.splitlines() if x.startswith('DEBUG_SERVER_URL=')), 'http://127.0.0.1:7777/event'); _sid=next((x.split('=',1)[1] for x in _c.splitlines() if x.startswith('DEBUG_SESSION_ID=')), 'plan-inheritance-writer'); _route='user_plan_generation' if mode == 'writer_harness' and plan_decision.get('turn_intent') == 'user_plan_generation' else 'run_single_mode'; _u.urlopen(_u.Request(_url, data=_j.dumps({'sessionId': _sid, 'runId': 'pre-fix', 'hypothesisId': 'B', 'location': 'writer_excute_multiturn.py:601', 'msg': '[DEBUG] writer route selected', 'data': {'mode': mode, 'route': _route, 'runWriterHarness': _route == 'run_single_mode', 'action': plan_decision.get('action'), 'turnIntent': plan_decision.get('turn_intent')}, 'ts': int(time.time() * 1000)}).encode(), headers={'Content-Type': 'application/json'}), timeout=1).read()\nexcept: pass")
            # #endregion
            if mode == "writer_harness" and plan_decision.get("turn_intent") == "user_plan_generation":
                response[mode] = run_user_plan_generation(args, effective_query, user_query, root_dir)
            else:
                previous_script, _ = get_previous_script(session)
                response[mode] = run_single_mode(
                    args,
                    effective_query,
                    user_query,
                    mode,
                    root_dir,
                    plan_decision,
                    previous_script,
                )
            execution = response[mode].get("execution") if isinstance(response[mode].get("execution"), dict) else {}
            execution["plan_action"] = plan_decision["action"]
            execution["plan_decision_reason"] = plan_decision["reason"]
            execution["plan_reference_usage"] = plan_decision.get("plan_reference_usage", "")
            response[mode]["execution"] = execution
        except Exception as exc:
            response[mode] = {
                "mode": mode,
                "ok": False,
                "error": str(exc),
                "execution": {
                    "executed": False,
                    "status": "failed",
                    "execution_output": "",
                    "stderr": str(exc),
                },
            }
    response["ok"] = any(isinstance(response.get(mode), dict) and bool(response[mode].get("ok")) for mode in modes)
    turn_id = args.turn_id or uuid4().hex[:12]
    session.setdefault("turns", []).append(build_turn_record(turn_id, user_query, effective_query, args.mode, response))
    if args.max_session_turns > 0:
        session["turns"] = session["turns"][-args.max_session_turns :]
    session["metadata"] = {
        "last_mode": args.mode,
        "last_auto_execute": not args.skip_execute,
        "last_oh_real_run": args.oh_real_run,
    }
    save_session(args, session)
    response["turn_id"] = turn_id
    response["session"] = {
        "session_id": session_id,
        "turn_count": len(session.get("turns", [])),
        "session_dir": str(get_session_dir(args)),
        "reset_supported": True,
    }
    response["timing"] = {
        "total_seconds": round(time.perf_counter() - started_at, 4),
        "multiturn_context_injected": effective_query != user_query,
        "plan_decision_seconds": plan_decision_seconds,
    }
    writer_result = response.get("writer_harness") if isinstance(response.get("writer_harness"), dict) else {}
    writer_timing = writer_result.get("timing") if isinstance(writer_result.get("timing"), dict) else {}
    writer_execution = writer_result.get("execution") if isinstance(writer_result.get("execution"), dict) else {}
    response["timing"]["writer_harness_seconds"] = round(plan_decision_seconds + float(writer_timing.get("script_stage_seconds") or 0), 4)
    response["timing"]["execution_seconds"] = float(writer_execution.get("execution_seconds") or 0)
    trace = writer_execution.get("tool_trace") if isinstance(writer_execution.get("tool_trace"), dict) else {}
    director_timestamps = [event.get("timestamp") for event in trace.get("director_events", []) if isinstance(event, dict) and isinstance(event.get("timestamp"), (int, float))]
    response["timing"]["director_seconds"] = round(max(director_timestamps) - min(director_timestamps), 4) if len(director_timestamps) > 1 else None
    return response


def build_parser() -> argparse.ArgumentParser:
    """定义多轮脚本的 CLI 参数。

    参数分组说明：
    - 会话控制：`--session-id` / `--turn-id` / `--session-dir` / `--reset-session`
    - 多轮注入：`--disable-history-context` / `--history-limit` /
      `--history-content-limit` / `--max-session-turns`
    - 策略控制：`--mode` / `--skip-execute`
    - 后端透传：`--oh-bin` / `--openharness-src` / `--writer-model` / `--actor-*`
    - 输出控制：`--json` / `--actor-output-format` / `--execute-output-format`

    设计原则：
    - 对 UI 友好：保留 session 相关参数；
    - 对旧逻辑兼容：继续透传单轮脚本依赖的运行参数；
    - 对调试友好：保留关闭历史注入、限制历史长度等开关。
    """

    parser = argparse.ArgumentParser(description="Run writer_harness with UI-friendly multi-turn session context.")
    parser.add_argument("--query", default="", help="当前轮用户输入。正常执行时必填；仅 `--reset-session` 可省略。")
    parser.add_argument("--session-id", default=None, help="UI 会话 ID。为空时自动生成，用于把多次请求绑定到同一后端 session。")
    parser.add_argument("--turn-id", default=None, help="UI 当前轮唯一 ID。若前端已生成 turn id，可原样透传，方便前后端对齐。")
    parser.add_argument("--session-dir", default=None, help="多轮会话状态保存目录。默认使用脚本同级 `.writer_harness_sessions/`。")
    parser.add_argument("--reset-session", action="store_true", help="清空指定 session 后立即退出，不执行本轮 query。")
    parser.add_argument("--delete-turn-id", default=None, help="删除指定 session 中的一条历史轮次后立即退出，不执行本轮 query。")
    parser.add_argument("--disable-history-context", action="store_true", help="禁用历史对话注入，仅按当前 query 执行。适合调试单轮/多轮差异。")
    parser.add_argument("--history-limit", type=int, default=DEFAULT_HISTORY_LIMIT, help="最多向当前 query 注入最近多少轮历史。")
    parser.add_argument("--history-content-limit", type=int, default=1200, help="每轮历史 query/assistant_output 允许注入的最大字符数。")
    parser.add_argument("--max-session-turns", type=int, default=50, help="session 文件最多保留多少轮历史；0 表示不裁剪。")
    parser.add_argument("--mode", choices=["vanilla", "writer_harness", "both"], default="both", help="执行策略：只跑 vanilla、只跑 writer_harness，或同时跑两者。")
    parser.add_argument("--actor-backend", choices=["openharness"], default="openharness", help="演员执行后端。当前多轮脚本沿用 OpenHarness。")
    parser.add_argument("--writer-backend", choices=["openai-compatible"], default="openai-compatible", help="编剧模型后端类型。当前与旧单轮脚本保持一致。")
    parser.add_argument("--writer-model", default=os.environ.get("WRITER_MODEL", ""), help="编剧模型名。若环境变量未配置，可在此显式覆盖。")
    parser.add_argument("--writer-base-url", default=os.environ.get("WRITER_BASE_URL"), help="编剧后端 base URL。")
    parser.add_argument("--writer-api-key", default=os.environ.get("WRITER_API_KEY"), help="编剧后端 API Key。")
    parser.add_argument("--oh-bin", default="oh", help="OpenHarness CLI 路径或可执行文件名。")
    parser.add_argument("--openharness-src", default=None, help="OpenHarness 源码 `src` 目录，用于本地源码模式运行。")
    parser.add_argument("--director-harness-enabled", action="store_true", help="启用 Director Harness 的工具调用预检与 MCP 替代。")
    parser.add_argument("--director-log-path", default=None, help="Director Harness JSONL 事件日志路径；默认使用项目 logs/director-events.jsonl。")
    parser.add_argument("--oh-real-run", action="store_true", help="开启真实执行；默认沿用 dry-run 行为。")
    parser.add_argument("--actor-model", default=os.environ.get("ACTOR_MODEL"), help="覆盖演员模型。")
    parser.add_argument("--actor-base-url", default=os.environ.get("ACTOR_BASE_URL"), help="覆盖演员后端 base URL。")
    parser.add_argument("--actor-api-key", default=os.environ.get("ACTOR_API_KEY"), help="覆盖演员后端 API Key。")
    parser.add_argument("--actor-api-format", default=os.environ.get("ACTOR_API_FORMAT"), help="覆盖演员后端 API 格式。")
    parser.add_argument("--actor-output-format", default=None, help="首次生成阶段的 actor 输出格式，如 `text` / `json` / `stream-json`。")
    parser.add_argument("--execute-output-format", default=None, help="真实执行阶段输出格式。若设为 `stream-json`，可提取工具轨迹。")
    parser.add_argument("--skip-execute", action="store_true", help="只跑剧本生成/评估，不进入真实执行阶段。")
    parser.add_argument("--json", action="store_true", help="以 JSON 输出，供 UI 或脚本程序读取。")
    return parser


def main() -> None:
    """命令行入口。

    执行行为：
    - 解析参数；
    - 对 query 必填性做最后一层校验；
    - 调用 `run_multiturn` 生成结果；
    - 默认与 `--json` 都输出 JSON，方便 UI 直接消费。
    """

    parser = build_parser()
    args = parser.parse_args()
    if not args.reset_session and not args.delete_turn_id and not args.query.strip():
        parser.error("--query 不能为空；仅 --reset-session 或 --delete-turn-id 可省略 query")
    single_turn.configure_director_harness_environment(args, Path(__file__).resolve().parent)
    result = run_multiturn(args)
    if args.json:
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
