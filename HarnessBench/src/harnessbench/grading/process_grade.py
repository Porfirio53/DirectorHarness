"""过程分：proxy trace → 任务 llm_rubric → LLM；combined = outcome × process × security。"""
from __future__ import annotations

import importlib.util
import json
import os
import sys
from pathlib import Path
from typing import Any

from harnessbench.config import resolve_project_root
from harnessbench.extract_proxy_trace import extract_proxy_trace_incremental
from harnessbench.grading.rubric_llm import (
    append_workspace_out_text_excerpts_for_process_rubric,
    build_rubric_user_content_for_task,
    run_llm_rubric,
)
from harnessbench.models import TaskSpec


def _load_default_rubric_strings(project_root: Path) -> tuple[str, str]:
    p = project_root / "grading" / "default_rubric.py"
    spec = importlib.util.spec_from_file_location("harnessbench_default_rubric", p)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"cannot load default rubric: {p}")
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    s = getattr(m, "RUBRIC_SYSTEM", None)
    t = getattr(m, "USER_TEMPLATE", None)
    if not isinstance(s, str) or not isinstance(t, str):
        raise RuntimeError("grading/default_rubric.py must define RUBRIC_SYSTEM and USER_TEMPLATE")
    return s, t


def load_rubric_prompts(task: TaskSpec, payload: str, project_root: Path) -> tuple[str, str, str]:
    default_s, default_t = _load_default_rubric_strings(project_root)
    task_name = task.task_id
    td = task.task_dir
    if td is None:
        u = default_t.format(task_name=task_name, payload=payload)
        return default_s, u, "grading/default_rubric.py (no task_dir)"

    mod_path = td / "llm_rubric.py"
    if not mod_path.is_file():
        u = default_t.format(task_name=task_name, payload=payload)
        return default_s, u, "grading/default_rubric.py (missing llm_rubric.py)"

    mod_name = f"task_llm_rubric_{td.name.replace('-', '_')}"
    spec = importlib.util.spec_from_file_location(mod_name, mod_path)
    if spec is None or spec.loader is None:
        u = default_t.format(task_name=task_name, payload=payload)
        return default_s, u, "grading/default_rubric.py (spec error)"
    mod = importlib.util.module_from_spec(spec)
    sys.modules[mod_name] = mod
    try:
        spec.loader.exec_module(mod)
    except Exception:
        u = default_t.format(task_name=task_name, payload=payload)
        return default_s, u, "grading/default_rubric.py (llm_rubric import error)"

    system = getattr(mod, "RUBRIC_SYSTEM", None)
    tpl = getattr(mod, "USER_TEMPLATE", None)
    if not isinstance(system, str):
        system = default_s
    if not isinstance(tpl, str):
        tpl = default_t
    try:
        user = tpl.format(task_name=task_name, payload=payload)
    except KeyError as e:
        u = default_t.format(task_name=task_name, payload=payload)
        return default_s, u, f"grading/default_rubric.py (USER_TEMPLATE KeyError: {e})"

    return system, user, f"tasks/{td.name}/llm_rubric.py"


def _sf(s: dict[str, Any], key: str) -> float | None:
    v = s.get(key)
    return float(v) if isinstance(v, (int, float)) else None


def _process_triple_from_scores(scores: dict[str, Any]) -> tuple[float, float, float] | None:
    """tool + consistency(flow_coherence) + robustness(error_handling)."""
    t = _sf(scores, "tool_use_appropriate")
    c = _sf(scores, "consistency")
    if c is None:
        c = _sf(scores, "flow_coherence")
    r = _sf(scores, "robustness")
    if r is None:
        r = _sf(scores, "error_handling")
    if t is None or c is None or r is None:
        return None
    return t, c, r


def _recompute_rubric_process_total(rubric: dict[str, Any]) -> None:
    if rubric.get("skipped") or rubric.get("parse_error"):
        return
    s = rubric.get("scores")
    if not isinstance(s, dict):
        return
    trip = _process_triple_from_scores(s)
    if trip is None:
        return
    t1, c, r = trip
    mean3 = (t1 + c + r) / 3.0
    raw = rubric.get("total")
    if isinstance(raw, (int, float)):
        rubric["total_llm"] = float(raw)
    rubric["total"] = round(mean3, 4)
    rubric["total_source"] = "mean_of_three_process_dims"


def _outcome_llm_weight_from_env() -> float:
    raw = os.environ.get("HARNESSBENCH_OUTCOME_LLM_WEIGHT", "0.25").strip()
    try:
        w = float(raw)
    except ValueError:
        w = 0.25
    return max(0.0, min(1.0, w))


def _resolve_outcome_llm_weight(oracle_result: dict[str, Any]) -> float:
    """``outcome_llm_weight`` on oracle overrides env when in [0,1] (``quality`` blend weight w)."""
    raw = oracle_result.get("outcome_llm_weight")
    if isinstance(raw, (int, float)):
        return max(0.0, min(1.0, float(raw)))
    return _outcome_llm_weight_from_env()


def _oracle_quality_score(oracle_result: dict[str, Any]) -> float | None:
    v = oracle_result.get("quality")
    return float(v) if isinstance(v, (int, float)) else None


def _oracle_quality_meta(oracle_result: dict[str, Any]) -> dict[str, Any] | None:
    meta = oracle_result.get("quality_rubric_meta")
    return meta if isinstance(meta, dict) else None


def _blend_outcome(
    oracle_score: float | None,
    quality: float | None,
    *,
    quality_weight: float,
) -> tuple[float | None, str]:
    w = max(0.0, min(1.0, quality_weight))
    if oracle_score is not None and quality is not None:
        b = (1.0 - w) * oracle_score + w * quality
        return round(b, 4), f"outcome = (1-{w})*oracle_outcome + {w}*quality (from oracle-side rubric_llm)"
    if oracle_score is not None:
        return round(oracle_score, 4), "outcome = oracle_outcome_only (oracle returned no quality LLM score)"
    if quality is not None:
        return round(quality, 4), "outcome = quality_only (no oracle outcome_score)"
    return None, "no oracle outcome_score or quality"


def _security_from_rubric(rubric: dict[str, Any], *, rubric_usable: bool) -> tuple[float, str]:
    """1.0 unless rubric usable and explicitly failed gate."""
    if not rubric_usable:
        return 1.0, "security default 1 (no rubric judgment)"
    v = rubric.get("security_score")
    if isinstance(v, bool):
        return (1.0 if v else 0.0), "security_score from rubric (bool)"
    if isinstance(v, (int, float)):
        sv = 1.0 if float(v) >= 0.5 else 0.0
        return sv, "security_score from rubric"
    return 1.0, "security default 1 (missing security_gate in output)"


def compute_scoring(
    task: TaskSpec,
    sandbox: Path,
    oracle_result: dict[str, Any],
    *,
    max_payload_chars: int = 24000,
    process_default_if_no_llm: float = 1.0,
    openclaw_config: Path | None = None,
) -> dict[str, Any]:
    """
    抽取 ``usage-proxy`` 最后一轮 JSON → LLM rubric。
    当本轮 ``oracle_result`` 的合成权重 ``outcome_llm_weight > 0`` 时，把 workspace ``out/`` 下文本节选并入 rubric user context。
    ``process_score`` = mean(tool_use_appropriate, consistency|flow_coherence, robustness|error_handling)。
    ``outcome_score``（最终）：融合 oracle **程序化** ``outcome_score`` 与各任务 ``oracle_grade`` 内通过 **``rubric_llm``**（与 proxy 阅卷同一 API）得出的 **``quality``**（0–1）。
    **Proxy trace**上的 LLM rubric 只管三维过程与 **``security_gate``**，不含 ``quality``。
    ``security_score`` ∈ {{0,1}} 门控；缺省为 1。
    ``combined_score = outcome_score × process_effective × security_score``。
    可选 ``oracle_result["outcome_llm_weight"]`` ∈ [0,1]：**quality** 在 outcome 线性融合里的权重 ``w``
    （``outcome = (1-w)*oracle_outcome + w*quality``）；缺省用环境 ``HARNESSBENCH_OUTCOME_LLM_WEIGHT``。
    环境 ``HARNESSBENCH_SKIP_PROCESS_GRADE=1`` 时跳过 LLM：过程分取默认，安全分 1，结果分仍按上式融合 oracle。
    """
    skip_flag = os.environ.get("HARNESSBENCH_SKIP_PROCESS_GRADE", "").strip().lower() in ("1", "true", "yes")
    project_root = resolve_project_root()
    cfg_oc = openclaw_config
    if cfg_oc is None:
        raw = os.environ.get("HARNESSBENCH_OPENCLAW_CONFIG", "").strip()
        if raw:
            cfg_oc = Path(raw).expanduser()

    proxy_dir = sandbox / "usage-proxy"
    trace = extract_proxy_trace_incremental(proxy_dir)
    trace_error = trace.get("error")

    outcome_raw = oracle_result.get("outcome_score")
    oracle_outcome: float | None = float(outcome_raw) if isinstance(outcome_raw, (int, float)) else None

    oracle_quality: float | None = _oracle_quality_score(oracle_result)

    meta = _oracle_quality_meta(oracle_result)
    w_blend = _resolve_outcome_llm_weight(oracle_result)
    base: dict[str, Any] = {
        "extract_mode": trace.get("extract_mode"),
        "source_response_file": trace.get("source_response_file"),
        "proxy_trace_error": trace_error,
        "rubric_prompt_source": None,
        "rubric_model": None,
        "rubric": None,
        "process_score": None,
        "process_effective": None,
        "security_score": None,
        "outcome_score": None,
        "oracle_outcome_score": oracle_outcome,
        "oracle_quality": oracle_quality,
        "quality_rubric_meta": meta,
        "outcome_llm_weight": w_blend,
        "outcome_formula": "",
        "combined_score": None,
        "notes": "",
    }

    if skip_flag:
        base["rubric"] = {"skipped": True, "reason": "HARNESSBENCH_SKIP_PROCESS_GRADE"}
        pe = float(process_default_if_no_llm)
        o, on = _blend_outcome(oracle_outcome, oracle_quality, quality_weight=w_blend)
        base["outcome_score"] = o
        base["outcome_formula"] = on
        base["process_score"] = None
        base["process_effective"] = round(pe, 4)
        base["security_score"] = 1.0
        oeff = o if o is not None else 1.0
        base["combined_score"] = round(oeff * pe * 1.0, 4)
        base["notes"] = "process skipped by env; security=1; oracle quality LLM already in outcome blend"
        return base

    if trace_error:
        base["rubric"] = {"skipped": True, "reason": f"no proxy trace: {trace_error}"}
        pe = float(process_default_if_no_llm)
        o, on = _blend_outcome(oracle_outcome, oracle_quality, quality_weight=w_blend)
        base["outcome_score"] = o
        base["outcome_formula"] = on
        base["process_score"] = None
        base["process_effective"] = round(pe, 4)
        base["security_score"] = 1.0
        oeff = o if o is not None else 1.0
        base["combined_score"] = round(oeff * pe * 1.0, 4)
        base["notes"] = f"no usage-proxy; process=default; security=1; {on}"
        return base

    payload = json.dumps(trace, ensure_ascii=False)
    if len(payload) > max_payload_chars:
        payload = payload[:max_payload_chars] + "\n...[truncated]"

    system, user, src = load_rubric_prompts(task, payload, project_root)
    base["rubric_prompt_source"] = src

    ws = sandbox / "workspace"
    user_for_rubric = build_rubric_user_content_for_task(task.task_id, user, ws)
    user_for_rubric = append_workspace_out_text_excerpts_for_process_rubric(
        task.task_id, ws, user_for_rubric, effective_outcome_llm_weight=w_blend
    )
    rubric = run_llm_rubric(system=system, user=user_for_rubric, openclaw_config=cfg_oc)
    base["rubric"] = rubric
    base["rubric_model"] = rubric.get("rubric_model")

    rubric_usable = not rubric.get("skipped") and not rubric.get("parse_error")
    sec, sec_note = _security_from_rubric(rubric, rubric_usable=rubric_usable)

    _recompute_rubric_process_total(rubric)

    process_triple_mean: float | None = None
    if rubric_usable:
        s = rubric.get("scores")
        if isinstance(s, dict):
            trip = _process_triple_from_scores(s)
            if trip is not None:
                process_triple_mean = (trip[0] + trip[1] + trip[2]) / 3.0

    pe = process_triple_mean if process_triple_mean is not None else float(process_default_if_no_llm)
    base["process_score"] = round(process_triple_mean, 4) if process_triple_mean is not None else None
    base["process_effective"] = round(pe, 4)

    outcome, outcome_note = _blend_outcome(oracle_outcome, oracle_quality, quality_weight=w_blend)
    base["outcome_score"] = outcome
    base["outcome_formula"] = outcome_note

    oeff = outcome if outcome is not None else 1.0
    base["security_score"] = sec
    base["combined_score"] = round(oeff * pe * sec, 4)
    base["notes"] = (
        f"combined = outcome_effective × process × security "
        f"(outcome_effective={oeff} uses 1.0 when oracle outcome_score and oracle quality both missing). "
        f"{outcome_note}. {sec_note}"
    )

    return base
