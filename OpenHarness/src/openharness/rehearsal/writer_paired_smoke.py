"""Validate and summarize the locked stage-2 Writer paired smoke."""

from __future__ import annotations

import json
from pathlib import Path
from statistics import mean, median
from typing import Any, Mapping, Sequence


MCP_TARGET_TASK_IDS = (10, 13, 18, 50, 51, 141, 173)
MCP_CONTROL_TASK_IDS = (4, 5, 23)
HARNESSBENCH_TARGET_TASK_IDS = (
    "079-smallfile-batch-reject-ledger",
    "092-schema-drift-audit",
    "098-three-source-decision-record-synthesis",
    "105-partial-batch-resume-ledger",
    "106-release-approval-gate-plan",
)
HARNESSBENCH_CONTROL_TASK_IDS = (
    "001-file",
    "059-event-update-replan",
    "060-task-cancellation-cleanup",
)
TASK_DOMAINS = {
    "mcp:10": "fan-out messaging with dependency and write-cardinality risk",
    "mcp:13": "calendar and chat workflow with cross-step identifiers",
    "mcp:18": "calendar mutation with prerequisite discovery",
    "mcp:50": "Slack retrieval and reaction workflow with recovery risk",
    "mcp:51": "Obsidian search and mutation with failed-write recovery",
    "mcp:141": "cross-server feed lookup with prerequisite identifiers",
    "mcp:173": "long cross-server workflow with safe-stop branches",
    "hb:079-smallfile-batch-reject-ledger": (
        "batch normalization, deterministic rejects, and duplicate handling"
    ),
    "hb:092-schema-drift-audit": "schema drift and row-level data quality",
    "hb:098-three-source-decision-record-synthesis": (
        "multi-source synthesis and conflict resolution"
    ),
    "hb:105-partial-batch-resume-ledger": ("multi-round resume, idempotency, and state adaptation"),
    "hb:106-release-approval-gate-plan": (
        "approval blockers, safe planning, and non-execution boundaries"
    ),
}
MATERIAL_SCORE_DELTA = 0.05
MCP_REHEARSAL_STEP_SCORES = "rehearsal-step-scores-v1.jsonl"


def _read_json(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"JSON file must contain an object: {path}")
    return value


def _read_jsonl(path: Path) -> list[dict[str, Any]]:
    values: list[dict[str, Any]] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        value = json.loads(line)
        if isinstance(value, dict):
            values.append(value)
    return values


def _ratio(values: Sequence[float]) -> float | None:
    return round(mean(values), 6) if values else None


def _numeric_total(value: Mapping[str, Any] | None) -> int | None:
    if not isinstance(value, Mapping):
        return None
    input_tokens = value.get("input_tokens")
    output_tokens = value.get("output_tokens")
    if not isinstance(input_tokens, int) or not isinstance(output_tokens, int):
        return None
    return input_tokens + output_tokens


def _mcp_rehearsal_step_scores(
    path: Path,
    *,
    expected_tasks: Sequence[int],
) -> dict[int, float]:
    score_path = path / MCP_REHEARSAL_STEP_SCORES
    if not score_path.is_file():
        return {}
    scores: dict[int, float] = {}
    for row in _read_jsonl(score_path):
        task_id = row.get("task_id")
        trial = row.get("trial")
        score = row.get("step_score")
        if not isinstance(task_id, int) or trial != 1 or not isinstance(score, (int, float)):
            raise ValueError(f"Invalid MCP rehearsal step score row: {score_path}")
        if task_id in scores:
            raise ValueError(f"Duplicate MCP rehearsal step score for task {task_id}: {score_path}")
        scores[task_id] = float(score)
    if set(scores) != set(expected_tasks):
        raise ValueError(
            f"MCP rehearsal step scores do not match the paired-smoke lock: {score_path}"
        )
    return scores


def load_mcp_arm(path: Path, *, expected_mode: str) -> dict[str, Any]:
    config = _read_json(path / "run-config.json")
    summary = _read_json(path / "baseline-summary.json")
    rows = _read_jsonl(path / "results.jsonl")
    expected_tasks = list(MCP_TARGET_TASK_IDS + MCP_CONTROL_TASK_IDS)
    if config.get("tasks") != expected_tasks:
        raise ValueError(f"MCP smoke tasks do not match the lock: {path}")
    if config.get("repeats") != 1:
        raise ValueError(f"MCP smoke must use one repeat: {path}")
    if config.get("experiment_stage") != "paired-smoke":
        raise ValueError(f"MCP run is not marked paired-smoke: {path}")
    if config.get("openharness_mode") != expected_mode:
        raise ValueError(f"MCP arm mode mismatch: {path}")
    scores = {
        int(row["task_id"]): float(row.get("local_scores", {}).get("execution_score", 0.0))
        for row in rows
        if isinstance(row.get("local_scores"), Mapping)
    }
    rehearsal_step_scores = _mcp_rehearsal_step_scores(
        path,
        expected_tasks=expected_tasks,
    )
    metrics: dict[int, dict[str, Any]] = {}
    for row in rows:
        local_scores = row.get("local_scores")
        if not isinstance(local_scores, Mapping):
            continue
        task_id = int(row["task_id"])
        actor_tokens = _numeric_total(row.get("usage"))
        writer = row.get("writer")
        writer_tokens = _numeric_total(writer.get("usage")) if isinstance(writer, Mapping) else None
        if actor_tokens is not None:
            total_tokens = actor_tokens + (writer_tokens or 0)
        else:
            total_tokens = None
        tool_calls = row.get("tool_calls")
        state = row.get("state")
        metrics[task_id] = {
            "score": float(local_scores.get("execution_score", 0.0)),
            "rehearsal_step_score": rehearsal_step_scores.get(task_id),
            "elapsed_seconds": (
                float(row["duration_seconds"])
                if isinstance(row.get("duration_seconds"), (int, float))
                else None
            ),
            "actor_tokens": actor_tokens,
            "writer_tokens": writer_tokens or 0,
            "total_tokens": total_tokens,
            "tool_calls": (len(tool_calls) if isinstance(tool_calls, list) else None),
            "request_count": None,
            "trial_state": row.get("trial_state"),
            "exact_reset": (state.get("exact_reset") if isinstance(state, Mapping) else None),
        }
    return {
        "config": config,
        "summary": summary,
        "scores": scores,
        "rehearsal_step_scores": rehearsal_step_scores,
        "metrics": metrics,
        "complete": (
            summary.get("run_complete") is True
            and summary.get("result_count") == len(expected_tasks)
        ),
        "writer_gate": summary.get("writer_smoke_gate"),
    }


def _harnessbench_scores(summary: Mapping[str, Any]) -> dict[str, float]:
    repeats = summary.get("repeats")
    if not isinstance(repeats, list) or len(repeats) != 1:
        return {}
    tasks = repeats[0].get("tasks")
    if not isinstance(tasks, list):
        return {}
    return {
        str(value["task_id"]): float(value["outcome_score"])
        for value in tasks
        if isinstance(value, Mapping) and isinstance(value.get("outcome_score"), (int, float))
    }


def _harnessbench_task_rows(
    summary: Mapping[str, Any],
) -> dict[str, Mapping[str, Any]]:
    repeats = summary.get("repeats")
    if not isinstance(repeats, list) or len(repeats) != 1:
        return {}
    tasks = repeats[0].get("tasks")
    if not isinstance(tasks, list):
        return {}
    return {
        str(value["task_id"]): value
        for value in tasks
        if isinstance(value, Mapping) and value.get("task_id")
    }


def _result_path_for_harnessbench_task(
    root: Path,
    task_id: str,
    summary_row: Mapping[str, Any] | None,
) -> Path | None:
    if isinstance(summary_row, Mapping):
        raw = summary_row.get("result_file")
        if isinstance(raw, str) and Path(raw).is_file():
            return Path(raw)
    matches = sorted(root.glob(f"repeat-01/results/openharness-local/*/{task_id}.json"))
    return matches[0] if len(matches) == 1 else None


def _adapter_tool_calls(result: Mapping[str, Any]) -> int | None:
    rounds = result.get("adapter_results")
    if not isinstance(rounds, list):
        return None
    total = 0
    parsed = 0
    for value in rounds:
        if not isinstance(value, Mapping):
            continue
        stdout = value.get("stdout")
        if not isinstance(stdout, str):
            continue
        try:
            payload = json.loads(stdout)
        except json.JSONDecodeError:
            continue
        if isinstance(payload, Mapping) and isinstance(payload.get("tool_calls"), int):
            total += int(payload["tool_calls"])
            parsed += 1
    return total if parsed else None


def _harnessbench_metrics(
    root: Path,
    summary: Mapping[str, Any],
) -> dict[str, dict[str, Any]]:
    rows = _harnessbench_task_rows(summary)
    metrics: dict[str, dict[str, Any]] = {}
    for task_id, row in rows.items():
        score = row.get("outcome_score")
        result_path = _result_path_for_harnessbench_task(
            root,
            task_id,
            row,
        )
        result = _read_json(result_path) if result_path is not None else {}
        usage = result.get("usage_summary")
        metrics[task_id] = {
            "score": (float(score) if isinstance(score, (int, float)) else None),
            "elapsed_seconds": (
                float(result["elapsed_sec"])
                if isinstance(result.get("elapsed_sec"), (int, float))
                else None
            ),
            "actor_tokens": None,
            "writer_tokens": None,
            "total_tokens": (
                int(usage["total_tokens"])
                if isinstance(usage, Mapping) and isinstance(usage.get("total_tokens"), int)
                else None
            ),
            "tool_calls": _adapter_tool_calls(result),
            "request_count": (
                int(usage["request_count"])
                if isinstance(usage, Mapping) and isinstance(usage.get("request_count"), int)
                else None
            ),
            "trial_state": row.get("state"),
            "exact_reset": None,
        }
    return metrics


def load_harnessbench_arm(
    path: Path,
    *,
    expected_mode: str,
) -> dict[str, Any]:
    config = _read_json(path / "run-config.json")
    summary = _read_json(path / "baseline-summary.json")
    expected_tasks = list(HARNESSBENCH_TARGET_TASK_IDS + HARNESSBENCH_CONTROL_TASK_IDS)
    if config.get("tasks") != expected_tasks:
        raise ValueError(f"HarnessBench smoke tasks do not match the lock: {path}")
    if config.get("repeats") != 1:
        raise ValueError(f"HarnessBench smoke must use one repeat: {path}")
    if config.get("openharness_mode") != expected_mode:
        raise ValueError(f"HarnessBench arm mode mismatch: {path}")
    return {
        "config": config,
        "summary": summary,
        "scores": _harnessbench_scores(summary),
        "metrics": _harnessbench_metrics(path, summary),
        "complete": (
            summary.get("baseline_ready") is True
            and summary.get("result_count") == len(expected_tasks)
        ),
        "writer_gate": summary.get("writer_smoke_gate"),
    }


def _comparisons(
    *,
    dataset: str,
    task_ids: Sequence[int | str],
    target_ids: set[int | str],
    original: Mapping[int | str, Mapping[str, Any]],
    writer: Mapping[int | str, Mapping[str, Any]],
) -> list[dict[str, Any]]:
    values: list[dict[str, Any]] = []
    for task_id in task_ids:
        original_metrics = original.get(task_id, {})
        writer_metrics = writer.get(task_id, {})
        original_score = original_metrics.get("score")
        writer_score = writer_metrics.get("score")
        delta = (
            round(writer_score - original_score, 6)
            if isinstance(original_score, (int, float)) and isinstance(writer_score, (int, float))
            else None
        )

        def metric_delta(name: str) -> dict[str, Any]:
            original_value = original_metrics.get(name)
            writer_value = writer_metrics.get(name)
            if not (
                isinstance(original_value, (int, float))
                and not isinstance(original_value, bool)
                and isinstance(writer_value, (int, float))
                and not isinstance(writer_value, bool)
            ):
                return {
                    "original": original_value,
                    "writer": writer_value,
                    "delta": None,
                    "delta_percent": None,
                }
            original_number = float(original_value)
            writer_number = float(writer_value)
            difference = round(writer_number - original_number, 6)
            percentage = (
                round((writer_number / original_number - 1) * 100, 6)
                if original_number != 0
                else None
            )
            return {
                "original": original_value,
                "writer": writer_value,
                "delta": difference,
                "delta_percent": percentage,
            }

        if isinstance(delta, (int, float)) and delta >= MATERIAL_SCORE_DELTA:
            quality_class = "preliminary_gain"
        elif isinstance(delta, (int, float)) and delta > 0:
            quality_class = "marginal_positive"
        elif isinstance(delta, (int, float)) and delta <= -MATERIAL_SCORE_DELTA:
            quality_class = "material_regression"
        elif isinstance(delta, (int, float)) and delta < 0:
            quality_class = "marginal_negative"
        else:
            quality_class = "stable"
        key = f"{dataset}:{task_id}"
        values.append(
            {
                "dataset": dataset,
                "task_id": task_id,
                "role": "target" if task_id in target_ids else "control",
                "domain": TASK_DOMAINS.get(key, "control task"),
                "original_score": original_score,
                "writer_score": writer_score,
                "delta": delta,
                "quality_class": quality_class,
                "mcp_frozen_rehearsal_step": metric_delta("rehearsal_step_score"),
                "resources": {
                    "total_tokens": metric_delta("total_tokens"),
                    "elapsed_seconds": metric_delta("elapsed_seconds"),
                    "tool_calls": metric_delta("tool_calls"),
                    "request_count": metric_delta("request_count"),
                },
            }
        )
    return values


def _aggregate_resources(
    comparisons: Sequence[Mapping[str, Any]],
    *,
    role: str,
    dataset: str | None = None,
) -> dict[str, Any]:
    selected = [
        value
        for value in comparisons
        if value.get("role") == role and (dataset is None or value.get("dataset") == dataset)
    ]
    output: dict[str, Any] = {}
    for metric in (
        "total_tokens",
        "elapsed_seconds",
        "tool_calls",
        "request_count",
    ):
        pairs: list[tuple[float, float]] = []
        for value in selected:
            resources = value.get("resources")
            metric_value = resources.get(metric) if isinstance(resources, Mapping) else None
            if not isinstance(metric_value, Mapping):
                continue
            original = metric_value.get("original")
            writer = metric_value.get("writer")
            if (
                isinstance(original, (int, float))
                and not isinstance(original, bool)
                and isinstance(writer, (int, float))
                and not isinstance(writer, bool)
            ):
                pairs.append((float(original), float(writer)))
        original_total = sum(value[0] for value in pairs)
        writer_total = sum(value[1] for value in pairs)
        per_task_percentages = [
            (writer / original - 1) * 100 for original, writer in pairs if original
        ]
        output[metric] = {
            "paired_task_count": len(pairs),
            "original_total": round(original_total, 6) if pairs else None,
            "writer_total": round(writer_total, 6) if pairs else None,
            "delta": (round(writer_total - original_total, 6) if pairs else None),
            "delta_percent": (
                round((writer_total / original_total - 1) * 100, 6)
                if pairs and original_total
                else None
            ),
            "per_task_delta_percent_mean": (
                round(mean(per_task_percentages), 6) if per_task_percentages else None
            ),
            "per_task_delta_percent_median": (
                round(median(per_task_percentages), 6) if per_task_percentages else None
            ),
        }
    return output


def summarize_paired_smoke(
    *,
    mcp_original: Mapping[str, Any],
    mcp_writer: Mapping[str, Any],
    harnessbench_original: Mapping[str, Any],
    harnessbench_writer: Mapping[str, Any],
) -> dict[str, Any]:
    """Combine outcome deltas with the three required Writer mechanism gates."""

    comparisons = _comparisons(
        dataset="mcp",
        task_ids=MCP_TARGET_TASK_IDS + MCP_CONTROL_TASK_IDS,
        target_ids=set(MCP_TARGET_TASK_IDS),
        original=mcp_original["metrics"],
        writer=mcp_writer["metrics"],
    )
    comparisons.extend(
        _comparisons(
            dataset="hb",
            task_ids=(HARNESSBENCH_TARGET_TASK_IDS + HARNESSBENCH_CONTROL_TASK_IDS),
            target_ids=set(HARNESSBENCH_TARGET_TASK_IDS),
            original=harnessbench_original["metrics"],
            writer=harnessbench_writer["metrics"],
        )
    )
    target_deltas = [
        float(value["delta"])
        for value in comparisons
        if value["role"] == "target" and isinstance(value["delta"], (int, float))
    ]
    control_deltas = [
        float(value["delta"])
        for value in comparisons
        if value["role"] == "control" and isinstance(value["delta"], (int, float))
    ]
    mcp_step_comparisons = [
        value
        for value in comparisons
        if value["dataset"] == "mcp"
        and isinstance(value.get("mcp_frozen_rehearsal_step"), Mapping)
        and isinstance(
            value["mcp_frozen_rehearsal_step"].get("delta"),
            (int, float),
        )
    ]
    mcp_step_target_deltas = [
        float(value["mcp_frozen_rehearsal_step"]["delta"])
        for value in mcp_step_comparisons
        if value["role"] == "target"
    ]
    mcp_step_control_deltas = [
        float(value["mcp_frozen_rehearsal_step"]["delta"])
        for value in mcp_step_comparisons
        if value["role"] == "control"
    ]
    mcp_gate = mcp_writer.get("writer_gate")
    hb_gate = harnessbench_writer.get("writer_gate")
    mechanisms_passed = (
        isinstance(mcp_gate, Mapping)
        and mcp_gate.get("passed") is True
        and isinstance(hb_gate, Mapping)
        and hb_gate.get("passed") is True
    )
    all_arms_complete = all(
        bool(value.get("complete"))
        for value in (
            mcp_original,
            mcp_writer,
            harnessbench_original,
            harnessbench_writer,
        )
    )
    quality_gains = [
        value
        for value in comparisons
        if value["role"] == "target"
        and isinstance(value["delta"], (int, float))
        and float(value["delta"]) >= MATERIAL_SCORE_DELTA
    ]
    marginal_positive = [
        value
        for value in comparisons
        if value["role"] == "target"
        and isinstance(value["delta"], (int, float))
        and 0 < float(value["delta"]) < MATERIAL_SCORE_DELTA
    ]
    material_regressions = [
        value
        for value in comparisons
        if isinstance(value["delta"], (int, float))
        and float(value["delta"]) <= -MATERIAL_SCORE_DELTA
    ]
    strict_advantages = [
        value
        for value in quality_gains
        if isinstance(value.get("resources"), Mapping)
        and isinstance(
            value["resources"].get("total_tokens"),
            Mapping,
        )
        and isinstance(
            value["resources"]["total_tokens"].get("delta_percent"),
            (int, float),
        )
        and float(value["resources"]["total_tokens"]["delta_percent"]) <= 0
    ]
    efficiency_candidates = [
        value
        for value in comparisons
        if value["role"] == "target"
        and isinstance(value["delta"], (int, float))
        and float(value["delta"]) >= 0
        and isinstance(value.get("resources"), Mapping)
        and isinstance(
            value["resources"].get("total_tokens"),
            Mapping,
        )
        and isinstance(
            value["resources"]["total_tokens"].get("delta_percent"),
            (int, float),
        )
        and float(value["resources"]["total_tokens"]["delta_percent"]) < 0
    ]
    mcp_step_gains = [
        value
        for value in mcp_step_comparisons
        if value["role"] == "target"
        and float(value["mcp_frozen_rehearsal_step"]["delta"]) >= MATERIAL_SCORE_DELTA
    ]
    mcp_step_regressions = [
        value
        for value in mcp_step_comparisons
        if float(value["mcp_frozen_rehearsal_step"]["delta"]) <= -MATERIAL_SCORE_DELTA
    ]
    mcp_step_and_cost_candidates = [
        value
        for value in mcp_step_gains
        if isinstance(value.get("resources"), Mapping)
        and isinstance(value["resources"].get("total_tokens"), Mapping)
        and isinstance(
            value["resources"]["total_tokens"].get("delta_percent"),
            (int, float),
        )
        and float(value["resources"]["total_tokens"]["delta_percent"]) <= 0
    ]
    mcp_outcome_and_step_candidates = [
        value
        for value in mcp_step_gains
        if isinstance(value.get("delta"), (int, float))
        and float(value["delta"]) >= MATERIAL_SCORE_DELTA
    ]
    score_view_conflicts = [
        value
        for value in mcp_step_comparisons
        if isinstance(value.get("delta"), (int, float))
        and (
            (
                float(value["delta"]) >= MATERIAL_SCORE_DELTA
                and float(value["mcp_frozen_rehearsal_step"]["delta"]) <= -MATERIAL_SCORE_DELTA
            )
            or (
                float(value["delta"]) <= -MATERIAL_SCORE_DELTA
                and float(value["mcp_frozen_rehearsal_step"]["delta"]) >= MATERIAL_SCORE_DELTA
            )
        )
    ]
    return {
        "schema_version": 3,
        "result_scope": ("small paired smoke only; not a full exploration experiment"),
        "all_arms_complete": all_arms_complete,
        "writer_mechanism_gates_passed": mechanisms_passed,
        "stage2_gate_passed": all_arms_complete and mechanisms_passed,
        "writer_gates": {
            "mcp_persona": mcp_gate,
            "harnessbench": hb_gate,
        },
        "outcome_delta": {
            "target_mean": _ratio(target_deltas),
            "control_mean": _ratio(control_deltas),
            "target_count": len(target_deltas),
            "control_count": len(control_deltas),
        },
        "mcp_frozen_rehearsal_step_delta": {
            "target_mean": _ratio(mcp_step_target_deltas),
            "control_mean": _ratio(mcp_step_control_deltas),
            "target_count": len(mcp_step_target_deltas),
            "control_count": len(mcp_step_control_deltas),
            "scope": (
                "10x1 paired-smoke deterministic step score; semantic checkpoint score not included"
            ),
        },
        "resource_delta": {
            "all_targets": _aggregate_resources(
                comparisons,
                role="target",
            ),
            "all_controls": _aggregate_resources(
                comparisons,
                role="control",
            ),
            "mcp_targets": _aggregate_resources(
                comparisons,
                role="target",
                dataset="mcp",
            ),
            "mcp_controls": _aggregate_resources(
                comparisons,
                role="control",
                dataset="mcp",
            ),
            "harnessbench_targets": _aggregate_resources(
                comparisons,
                role="target",
                dataset="hb",
            ),
            "harnessbench_controls": _aggregate_resources(
                comparisons,
                role="control",
                dataset="hb",
            ),
        },
        "task_comparisons": comparisons,
        "advantage_candidates": quality_gains,
        "strict_quality_and_cost_advantage_candidates": strict_advantages,
        "efficiency_candidates": efficiency_candidates,
        "mcp_rehearsal_step_advantage_candidates": mcp_step_gains,
        "mcp_rehearsal_step_and_cost_candidates": (mcp_step_and_cost_candidates),
        "mcp_outcome_and_rehearsal_step_advantage_candidates": (mcp_outcome_and_step_candidates),
        "mcp_rehearsal_step_regressions": mcp_step_regressions,
        "mcp_score_view_conflicts": score_view_conflicts,
        "marginal_positive_observations": marginal_positive,
        "material_regressions": material_regressions,
        "regressions": material_regressions,
        "effect_claim_ready": False,
        "effect_limitations": [
            (
                "Each arm has one observation per task; task deltas are "
                "screening signals, not reproducible effect estimates."
            ),
            (
                "MCP execution_score and HarnessBench outcome_score are "
                "local deterministic metrics with different semantics."
            ),
            (
                "The MCP frozen rehearsal step score is a local deterministic "
                "trajectory score without semantic checkpoint review."
            ),
            (
                "Writer v1 provides a global report and tool recommendations; "
                "it has no native per-step enforcement interface."
            ),
        ],
        "full_exploration_started": False,
        "full_exploration_allowed": False,
        "next_gate": (
            "Run only a targeted confirmatory paired smoke for material gains "
            "and regressions before authorizing any full exploration."
        ),
    }
