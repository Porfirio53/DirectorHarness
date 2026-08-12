"""After ``oracle_grade``, set default ``outcome_llm_weight``; optional generic text ``quality`` when ``w>0``.

With current defaults, only vision tasks use ``w=0.9``; generic-text quality LLM does not run for other tasks.
"""
from __future__ import annotations

import os
from pathlib import Path
from typing import Any

from harnessbench.grading.oracle_quality_llm import oracle_openclaw_config_path, run_oracle_quality_llm
from harnessbench.grading.rubric_llm import collect_out_dir_text_snippets, read_text_file_capped
from harnessbench.grading.task_outcome_llm_weights import outcome_llm_weight_for_task


VISION_TASKS = frozenset({"008-image-recognize", "013-image-edit"})

_QUALITY_SYSTEM_GENERIC = """You are a benchmark deliverable-quality grader.
You ONLY output one JSON object, no markdown fences, no extra text.
Given the TASK PROMPT excerpt, optional REFERENCE excerpt, and AGENT OUTPUT text excerpts from the workspace,
score how well the outputs fulfill the apparent task intent (coverage, coherence, no obvious hallucinated paths).
Penalize empty/missing substantive outputs relative to what the prompt demands.
Return ONLY: {"quality": <float 0.0-1.0>, "notes": "<one short sentence>"}."""


def merge_oracle_quality(
    task_id: str,
    task_dir: Path,
    workspace: Path,
    oracle_result: dict[str, Any],
    *,
    openclaw_config: Path | None = None,
) -> dict[str, Any]:
    """Set ``outcome_llm_weight`` when absent; run generic text ``quality`` LLM when w>0 and quality missing.

    Vision tasks **08** / **13** skip generic LLM (their oracle attaches images).
    """
    r = oracle_result
    if r.get("error"):
        return r

    ow = r.get("outcome_llm_weight")
    if not isinstance(ow, (int, float)):
        r["outcome_llm_weight"] = round(float(outcome_llm_weight_for_task(task_id)), 4)
    else:
        r["outcome_llm_weight"] = max(0.0, min(1.0, float(ow)))

    w = float(r["outcome_llm_weight"])
    if w <= 0:
        return r

    if isinstance(r.get("quality"), (int, float)):
        return r

    if task_id in VISION_TASKS:
        return r

    if os.environ.get("HARNESSBENCH_SKIP_ORACLE_QUALITY_LLM", "").strip().lower() in ("1", "true", "yes"):
        r["quality_rubric_meta"] = {
            "skipped": True,
            "reason": "HARNESSBENCH_SKIP_ORACLE_QUALITY_LLM",
            "source": "harness_oracle_quality_layer",
        }
        return r

    td = task_dir.resolve()
    ws = workspace.resolve()

    prompt_excerpt = ""
    pp = td / "prompt.txt"
    if pp.is_file():
        prompt_excerpt = read_text_file_capped(pp, 2200) or ""

    ref_excerpt = ""
    gt = td / "ground_truth.json"
    if gt.is_file():
        ref_excerpt = read_text_file_capped(gt, 3500) or ""

    snippets = collect_out_dir_text_snippets(ws)

    user = (
        f"Task ID: {task_id}\n\n"
        "### Task prompt excerpt\n"
        + (prompt_excerpt or "(no prompt.txt)")
        + "\n\n### Reference / ground_truth excerpt (if any)\n"
        + (ref_excerpt or "(none)")
        + "\n\n### Agent workspace text outputs (primarily under out/)\n"
        + snippets
    )

    try:
        q, meta = run_oracle_quality_llm(
            system=_QUALITY_SYSTEM_GENERIC,
            user=user,
            openclaw_config=openclaw_config or oracle_openclaw_config_path(None),
        )
        base_meta: dict[str, Any] = {
            "source": "harness_oracle_quality_layer",
            "mode": "generic_text",
        }
        if isinstance(meta, dict):
            base_meta.update(meta)
        r["quality_rubric_meta"] = base_meta
        if q is not None:
            r["quality"] = q
    except Exception as exc:
        r["quality_rubric_meta"] = {
            "skipped": False,
            "source": "harness_oracle_quality_layer",
            "mode": "generic_text",
            "error": repr(exc),
            "notes": "generic oracle quality LLM failed",
        }

    return r
