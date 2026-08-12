#!/usr/bin/env python3
"""Offline-score OpenHarness MCP-Persona traces with the frozen rehearsal spec."""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
from typing import Any, Mapping

from openharness.rehearsal import mcp_persona_rehearsal
from openharness.rehearsal.mcp_persona_rehearsal import (
    VERIFIED52_PROTOCOL_ID,
    VERIFIED52_WRITER_ARM,
    VERIFIED_TASK_IDS,
    read_jsonl,
    score_rehearsal_trial,
    sha256_file,
    summarize_rehearsal_scores,
    validate_rehearsal_spec,
    verified52_experiment_arm,
    write_json,
    write_jsonl,
)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--results", type=Path, required=True)
    parser.add_argument(
        "--semantic-reviews",
        type=Path,
        help=(
            "Semantic checkpoint reviews. Required for a complete Verified52 arm; "
            "optional for --allow-partial step-only smoke scoring."
        ),
    )
    parser.add_argument("--spec", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--summary-output", type=Path, required=True)
    parser.add_argument("--expected-repeats", type=int, default=2)
    parser.add_argument(
        "--allow-partial",
        action="store_true",
        help="Allow a development smoke over a strict subset; never use for the final baseline",
    )
    return parser.parse_args()


def _unique_by_key(
    values: list[dict[str, Any]],
    *,
    label: str,
) -> dict[tuple[int, int], dict[str, Any]]:
    result: dict[tuple[int, int], dict[str, Any]] = {}
    for value in values:
        if not isinstance(value.get("task_id"), int) or not isinstance(value.get("trial"), int):
            raise ValueError(f"{label} contains an invalid task/trial key")
        key = (int(value["task_id"]), int(value["trial"]))
        if key in result:
            raise ValueError(f"{label} contains duplicate key {key}")
        result[key] = value
    return result


def _evaluator_sha256() -> str:
    module_path = Path(mcp_persona_rehearsal.__file__).resolve()
    script_path = Path(__file__).resolve()
    digest = hashlib.sha256()
    for path in (module_path, script_path):
        digest.update(str(path.name).encode("utf-8"))
        digest.update(b"\0")
        digest.update(path.read_bytes())
        digest.update(b"\0")
    return digest.hexdigest()


def _canonical_sha256(value: Any) -> str:
    return hashlib.sha256(
        json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            default=str,
        ).encode("utf-8")
    ).hexdigest()


def _read_object(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"{path} is not a JSON object")
    return value


def _validate_verified52_inputs(
    args: argparse.Namespace,
    results: Mapping[tuple[int, int], Mapping[str, Any]],
    semantic: Mapping[tuple[int, int], Mapping[str, Any]],
) -> str:
    expected = {(task_id, trial) for task_id in VERIFIED_TASK_IDS for trial in (1, 2)}
    if set(results) != expected or set(semantic) != expected:
        raise ValueError(
            "Verified52 scoring requires the same exact 52x2 result and semantic slots"
        )
    run_config = _read_object(args.results.with_name("run-config.json"))
    baseline_summary = _read_object(args.results.with_name("baseline-summary.json"))
    semantic_summary = _read_object(args.semantic_reviews.with_name("semantic-summary.json"))
    arm = verified52_experiment_arm(run_config)
    if arm is None:
        raise ValueError(
            "Source Agent run is not an exact Original or Writer Verified52 arm"
        )
    if (
        run_config.get("protocol_id") != VERIFIED52_PROTOCOL_ID
        or run_config.get("experiment_arm") != arm
        or baseline_summary.get("verified52_ready") is not True
        or baseline_summary.get("baseline_ready") is not True
        or baseline_summary.get("protocol_id") != VERIFIED52_PROTOCOL_ID
        or baseline_summary.get("experiment_arm") != arm
        or baseline_summary.get("expected_results") != 104
        or baseline_summary.get("valid_result_count") != 104
    ):
        raise ValueError("Source Agent run is not a ready Verified52 protocol arm")
    if arm == VERIFIED52_WRITER_ARM:
        writer_gate = baseline_summary.get("writer_smoke_gate")
        if not isinstance(writer_gate, Mapping) or writer_gate.get("passed") is not True:
            raise ValueError("Writer Verified52 source did not pass the Writer gate")
    if semantic_summary.get("semantic_review_ready") is not True:
        raise ValueError("Semantic checkpoint review is incomplete")
    for key, result in results.items():
        review = semantic[key]
        if (
            review.get("fully_resolved") is not True
            or review.get("judge_error")
            or review.get("source_trial_sha256") != _canonical_sha256(result)
        ):
            raise ValueError(f"Semantic review {key} is unresolved or belongs to another result")
    return str(arm)


def main() -> int:
    args = parse_args()
    if args.expected_repeats < 1:
        raise ValueError("--expected-repeats must be positive")
    if args.semantic_reviews is None and not args.allow_partial:
        raise ValueError("--semantic-reviews is required unless --allow-partial is set")
    spec_value = json.loads(args.spec.read_text(encoding="utf-8"))
    if not isinstance(spec_value, dict):
        raise ValueError("Spec is not a JSON object")
    spec: Mapping[str, Any] = spec_value
    validation = validate_rehearsal_spec(spec)
    task_specs = {
        int(value["task_id"]): value for value in spec["tasks"] if isinstance(value, Mapping)
    }
    results = _unique_by_key(read_jsonl(args.results), label="results")
    semantic = (
        _unique_by_key(
            read_jsonl(args.semantic_reviews),
            label="semantic reviews",
        )
        if args.semantic_reviews is not None
        else {}
    )
    source_arm: str | None = None
    if not args.allow_partial:
        if args.expected_repeats != 2:
            raise ValueError("Verified52 scoring requires --expected-repeats 2")
        source_arm = _validate_verified52_inputs(args, results, semantic)
    spec_digest = sha256_file(args.spec)
    evaluator_digest = _evaluator_sha256()
    scores: list[dict[str, Any]] = []
    for key in sorted(results):
        task_id, _trial = key
        task_spec = task_specs.get(task_id)
        if task_spec is None:
            raise ValueError(f"Result task {task_id} is outside the frozen spec")
        scores.append(
            score_rehearsal_trial(
                task_spec,
                results[key],
                semantic_review=semantic.get(key),
                spec_sha256=spec_digest,
                evaluator_sha256=evaluator_digest,
            )
        )
    summary = summarize_rehearsal_scores(
        scores,
        spec=spec,
        expected_repeats=args.expected_repeats,
        allow_partial=bool(args.allow_partial),
    )
    summary["spec_sha256"] = spec_digest
    summary["evaluator_sha256"] = evaluator_digest
    summary["spec_validation"] = validation
    summary["source_results"] = str(args.results.resolve())
    summary["source_semantic_reviews"] = (
        str(args.semantic_reviews.resolve()) if args.semantic_reviews is not None else None
    )
    summary["source_sha256"] = {
        "results": sha256_file(args.results),
        **(
            {"semantic_reviews": sha256_file(args.semantic_reviews)}
            if args.semantic_reviews is not None
            else {}
        ),
    }
    summary["source_protocol_id"] = (
        VERIFIED52_PROTOCOL_ID if source_arm is not None else None
    )
    summary["source_experiment_arm"] = source_arm
    summary["strict_paired_causal_claim_ready"] = False
    write_jsonl(args.output, scores)
    write_json(args.summary_output, summary)
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return 0 if summary["baseline_ready"] or args.allow_partial else 2


if __name__ == "__main__":
    raise SystemExit(main())
