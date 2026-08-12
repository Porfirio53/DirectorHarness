#!/usr/bin/env python3
"""Build the unified MCP-Persona Verified52 Baseline1 summary offline."""

from __future__ import annotations

import argparse
import json
import statistics
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any, Mapping, Sequence

from openharness.rehearsal.mcp_persona_rehearsal import (
    NO_PUBLIC_CHECKPOINT_TASK_IDS,
    VERIFIED_TASK_IDS,
    read_jsonl,
    sha256_file,
    validate_rehearsal_spec,
    write_json,
)


RESULT_LABEL = (
    "OpenHarness-compatible local MCP-Persona Verified52 baseline; "
    "not an official MCP-Persona score"
)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--results", type=Path, required=True)
    parser.add_argument("--semantic-reviews", type=Path, required=True)
    parser.add_argument("--spec", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    return parser.parse_args()


def _read_json(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"{path} is not a JSON object")
    return value


def _mean(values: Sequence[float]) -> float | None:
    return round(statistics.fmean(values), 6) if values else None


def _median(values: Sequence[float]) -> float | None:
    return round(statistics.median(values), 6) if values else None


def _index_exact(
    rows: Sequence[Mapping[str, Any]],
    *,
    label: str,
) -> dict[tuple[int, int], Mapping[str, Any]]:
    expected = {
        (task_id, trial)
        for task_id in VERIFIED_TASK_IDS
        for trial in (1, 2)
    }
    indexed: dict[tuple[int, int], Mapping[str, Any]] = {}
    for row in rows:
        if not isinstance(row.get("task_id"), int) or not isinstance(
            row.get("trial"), int
        ):
            raise ValueError(f"{label} has an invalid task/trial key")
        key = (int(row["task_id"]), int(row["trial"]))
        if key in indexed:
            raise ValueError(f"{label} contains duplicate key {key}")
        indexed[key] = row
    missing = sorted(expected - set(indexed))
    unexpected = sorted(set(indexed) - expected)
    if missing or unexpected:
        raise ValueError(
            f"{label} does not cover exact Verified52x2 slots; "
            f"missing={len(missing)}, unexpected={unexpected}"
        )
    return indexed


def _numeric_local_scores(
    rows: Sequence[Mapping[str, Any]],
) -> dict[str, list[float]]:
    values: dict[str, list[float]] = defaultdict(list)
    for row in rows:
        local_scores = row.get("local_scores")
        if not isinstance(local_scores, Mapping):
            continue
        for name, value in local_scores.items():
            if isinstance(value, (int, float)) and not isinstance(value, bool):
                values[str(name)].append(float(value))
    return values


def _score_breakdown(
    rows: Sequence[Mapping[str, Any]],
    reviews: Mapping[tuple[int, int], Mapping[str, Any]],
) -> dict[str, Any]:
    local = _numeric_local_scores(rows)
    semantic = [
        float(reviews[(int(row["task_id"]), int(row["trial"]))][
            "semantic_checkpoint_score"
        ])
        for row in rows
        if isinstance(
            reviews[(int(row["task_id"]), int(row["trial"]))].get(
                "semantic_checkpoint_score"
            ),
            (int, float),
        )
        and not isinstance(
            reviews[(int(row["task_id"]), int(row["trial"]))].get(
                "semantic_checkpoint_score"
            ),
            bool,
        )
    ]
    joint_pass = [
        row
        for row in rows
        if reviews[(int(row["task_id"]), int(row["trial"]))].get(
            "checkpoint_available"
        )
        and row.get("local_scores", {}).get("local_pass") is True
        and reviews[(int(row["task_id"]), int(row["trial"]))].get(
            "semantic_task_complete"
        )
        is True
    ]
    return {
        "trial_count": len(rows),
        "local_score_means": {
            name: _mean(values) for name, values in sorted(local.items())
        },
        "semantic_checkpoint": {
            "coverage": len(semantic),
            "mean": _mean(semantic),
            "median": _median(semantic),
            "strict_pass_count": sum(value == 1.0 for value in semantic),
            "strict_pass_rate": (
                round(sum(value == 1.0 for value in semantic) / len(semantic), 6)
                if semantic
                else None
            ),
        },
        "joint_chain_and_semantic_strict_pass": {
            "count": len(joint_pass),
            "denominator": len(semantic),
            "rate": (
                round(len(joint_pass) / len(semantic), 6)
                if semantic
                else None
            ),
        },
    }


def build_baseline1_summary(
    results: Sequence[Mapping[str, Any]],
    semantic_reviews: Sequence[Mapping[str, Any]],
    *,
    run_config: Mapping[str, Any],
    spec: Mapping[str, Any],
) -> dict[str, Any]:
    """Validate and summarize the one fixed 52-task, two-trial baseline."""

    result_index = _index_exact(results, label="results")
    review_index = _index_exact(semantic_reviews, label="semantic reviews")
    if (
        run_config.get("tasks") != list(VERIFIED_TASK_IDS)
        or run_config.get("repeats") != 2
        or run_config.get("language") != "en"
        or run_config.get("tool_scope") != "server"
        or run_config.get("chain_guidance") is not False
        or run_config.get("formal_verified52") is not True
        or run_config.get("dataset_id") != "mcp-persona-verified52"
    ):
        raise ValueError("run-config.json is not the formal blind Verified52x2 run")
    validate_rehearsal_spec(spec)
    task_specs = {
        int(value["task_id"]): value
        for value in spec.get("tasks", [])
        if isinstance(value, Mapping)
    }

    ordered_results = [
        result_index[(task_id, trial)]
        for task_id in VERIFIED_TASK_IDS
        for trial in (1, 2)
    ]
    for key, row in result_index.items():
        run_status = row.get("run_status")
        state = row.get("state")
        if (
            not isinstance(run_status, Mapping)
            or run_status.get("baseline_valid") is not True
            or not isinstance(state, Mapping)
            or state.get("exact_reset") is not True
        ):
            raise ValueError(f"Result {key} is not a valid reset baseline outcome")
        review = review_index[key]
        if review.get("fully_resolved") is not True or review.get("judge_error"):
            raise ValueError(f"Semantic review {key} is unresolved")
        has_checkpoint = task_specs[key[0]].get("has_public_checkpoint") is True
        if bool(review.get("checkpoint_available")) is not has_checkpoint:
            raise ValueError(f"Semantic checkpoint availability disagrees for {key}")

    outcome_counts = Counter(
        str(row.get("run_status", {}).get("detail", "unknown"))
        for row in ordered_results
    )
    total_tool_calls = sum(
        len(row.get("tool_calls", []))
        for row in ordered_results
        if isinstance(row.get("tool_calls"), list)
    )
    tool_error_count = sum(
        call.get("is_error") is True or call.get("simulator_error") is True
        for row in ordered_results
        for call in row.get("tool_calls", [])
        if isinstance(call, Mapping)
    )
    durations = [
        float(row["duration_seconds"])
        for row in ordered_results
        if isinstance(row.get("duration_seconds"), (int, float))
        and not isinstance(row.get("duration_seconds"), bool)
    ]
    input_tokens = sum(
        int(row.get("usage", {}).get("input_tokens", 0))
        for row in ordered_results
        if isinstance(row.get("usage"), Mapping)
    )
    output_tokens = sum(
        int(row.get("usage", {}).get("output_tokens", 0))
        for row in ordered_results
        if isinstance(row.get("usage"), Mapping)
    )

    grouped: dict[int, list[Mapping[str, Any]]] = defaultdict(list)
    for row in ordered_results:
        grouped[int(row["task_id"])].append(row)
    repeat_deltas: dict[str, list[float]] = defaultdict(list)
    for task_id, rows in grouped.items():
        rows = sorted(rows, key=lambda value: int(value["trial"]))
        for name in ("execution_score", "sequence_score", "expected_tool_recall"):
            values = [
                row.get("local_scores", {}).get(name)
                for row in rows
                if isinstance(row.get("local_scores"), Mapping)
            ]
            if len(values) == 2 and all(
                isinstance(value, (int, float)) and not isinstance(value, bool)
                for value in values
            ):
                repeat_deltas[name].append(abs(float(values[0]) - float(values[1])))
        semantic_values = [
            review_index[(task_id, int(row["trial"]))].get(
                "semantic_checkpoint_score"
            )
            for row in rows
        ]
        if len(semantic_values) == 2 and all(
            isinstance(value, (int, float)) and not isinstance(value, bool)
            for value in semantic_values
        ):
            first_semantic = semantic_values[0]
            second_semantic = semantic_values[1]
            assert isinstance(first_semantic, (int, float))
            assert isinstance(second_semantic, (int, float))
            repeat_deltas["semantic_checkpoint_score"].append(
                abs(float(first_semantic) - float(second_semantic))
            )

    diagnostic_breakdowns: dict[str, dict[str, Any]] = {}
    for dimension in ("chain_length", "server_scope", "state_modification"):
        buckets: dict[str, list[Mapping[str, Any]]] = defaultdict(list)
        for row in ordered_results:
            strata = task_specs[int(row["task_id"])].get("strata")
            if isinstance(strata, Mapping):
                buckets[str(strata[dimension])].append(row)
        diagnostic_breakdowns[dimension] = {
            name: _score_breakdown(values, review_index)
            for name, values in sorted(buckets.items())
        }

    overall = _score_breakdown(ordered_results, review_index)
    semantic_coverage = overall["semantic_checkpoint"]["coverage"]
    expected_semantic_coverage = (
        len(VERIFIED_TASK_IDS) - len(NO_PUBLIC_CHECKPOINT_TASK_IDS)
    ) * 2
    baseline_ready = semantic_coverage == expected_semantic_coverage
    return {
        "schema_version": 1,
        "result_scope": RESULT_LABEL,
        "dataset_id": "mcp-persona-verified52",
        "task_ids": list(VERIFIED_TASK_IDS),
        "task_count": len(VERIFIED_TASK_IDS),
        "repeats": 2,
        "expected_trial_count": 104,
        "unique_trial_count": len(result_index),
        "configuration": {
            name: run_config.get(name)
            for name in (
                "model",
                "temperature",
                "seed",
                "max_turns",
                "max_tokens",
                "tool_scope",
                "chain_guidance",
                "runtime_fingerprint_sha256",
            )
        },
        "model_outcome_counts": dict(sorted(outcome_counts.items())),
        "overall": overall,
        "cost_and_runtime": {
            "total_tool_calls": total_tool_calls,
            "tool_error_count": tool_error_count,
            "input_tokens": input_tokens,
            "output_tokens": output_tokens,
            "total_tokens": input_tokens + output_tokens,
            "duration_seconds_total": round(sum(durations), 3),
            "duration_seconds_mean": _mean(durations),
        },
        "repeat_stability": {
            name: {
                "pair_count": len(values),
                "mean_absolute_delta": _mean(values),
            }
            for name, values in sorted(repeat_deltas.items())
        },
        "no_public_checkpoint_task_ids": list(NO_PUBLIC_CHECKPOINT_TASK_IDS),
        "diagnostic_breakdowns": diagnostic_breakdowns,
        "membership_policy": (
            "All 52 tasks remain in the dataset regardless of model outcome. "
            "Breakdowns are diagnostics, not subsets."
        ),
        "baseline_ready": baseline_ready,
    }


def main() -> int:
    args = parse_args()
    run_config = _read_json(args.results.with_name("run-config.json"))
    baseline_summary = _read_json(args.results.with_name("baseline-summary.json"))
    semantic_summary = _read_json(
        args.semantic_reviews.with_name("semantic-summary.json")
    )
    semantic_config = _read_json(
        args.semantic_reviews.with_name("semantic-run-config.json")
    )
    if baseline_summary.get("baseline_ready") is not True:
        raise ValueError("Agent baseline is incomplete")
    if semantic_summary.get("semantic_review_ready") is not True:
        raise ValueError("Semantic checkpoint review is incomplete")
    if semantic_config.get("results_sha256") != sha256_file(args.results):
        raise ValueError("Semantic review was produced from different results")
    spec_value = _read_json(args.spec)
    summary = build_baseline1_summary(
        read_jsonl(args.results),
        read_jsonl(args.semantic_reviews),
        run_config=run_config,
        spec=spec_value,
    )
    summary["source_sha256"] = {
        "results": sha256_file(args.results),
        "semantic_reviews": sha256_file(args.semantic_reviews),
        "spec": sha256_file(args.spec),
    }
    write_json(args.output, summary)
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return 0 if summary["baseline_ready"] else 2


if __name__ == "__main__":
    raise SystemExit(main())
