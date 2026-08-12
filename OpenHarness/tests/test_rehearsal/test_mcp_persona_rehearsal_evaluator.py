from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

import pytest

from openharness.rehearsal.mcp_persona_rehearsal import (
    MECHANISM_METRICS,
    NO_PUBLIC_CHECKPOINT_TASK_IDS,
    STEP_WEIGHTS,
    VERIFIED_TASK_IDS,
    build_rehearsal_spec,
    score_rehearsal_trial,
    summarize_rehearsal_scores,
    validate_rehearsal_spec,
    write_json,
    write_jsonl,
)
from scripts import evaluate_mcp_persona_rehearsal as evaluator_cli


def _mcp_root() -> Path:
    configured = os.environ.get("MCP_PERSONA_ROOT")
    root = Path(configured) if configured else Path(__file__).resolve().parents[3] / "MCP-Persona"
    if not (root / "data/tasks/en_release_data.json").is_file():
        pytest.skip("MCP-Persona checkout is unavailable")
    return root.resolve()


def _milestone(
    milestone_id: str,
    tool: str,
    *,
    effect: str = "read",
    completion_mode: str = "success",
    activation: str = "active",
    required: bool = True,
    rules: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    return {
        "id": milestone_id,
        "required": required,
        "requirement": "required" if required else "conditional",
        "acceptable_actions": [{"tool": tool, "input_rules": rules or []}],
        "effect": effect,
        "completion_mode": completion_mode,
        "activation": activation,
        "postcondition_verifiers": [],
    }


def _task_spec(
    milestones: list[dict[str, Any]],
    *,
    edges: list[dict[str, Any]] | None = None,
    active_limits: dict[str, int] | None = None,
    has_checkpoint: bool = True,
) -> dict[str, Any]:
    return {
        "task_id": 1,
        "milestones": milestones,
        "edges": edges or [],
        "active_tool_call_limits": active_limits
        or {
            str(action["tool"]): 1
            for milestone in milestones
            for action in milestone["acceptable_actions"]
        },
        "has_public_checkpoint": has_checkpoint,
    }


def _result(
    calls: list[dict[str, Any]],
    *,
    final_answer: str = "done",
    state_changed: bool = True,
    events: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    return {
        "task_id": 1,
        "trial": 1,
        "tool_calls": calls,
        "final_answer": final_answer,
        "state": {"state_changed": state_changed},
        "events": events or [],
    }


def _call(
    tool: str,
    *,
    arguments: dict[str, Any] | None = None,
    output: str = '{"success": true}',
    is_error: bool = False,
) -> dict[str, Any]:
    return {
        "tool_name": tool,
        "input": arguments or {},
        "output": output,
        "is_error": is_error,
    }


def _score(
    task_spec: dict[str, Any],
    result: dict[str, Any],
    *,
    semantic: float | None = 1.0,
) -> dict[str, Any]:
    review = {"semantic_checkpoint_score": semantic} if semantic is not None else None
    return score_rehearsal_trial(
        task_spec,
        result,
        semantic_review=review,
        spec_sha256="spec",
        evaluator_sha256="evaluator",
    )


def test_generated_spec_is_deterministic_complete_and_reviewed(
    tmp_path: Path,
) -> None:
    first = build_rehearsal_spec(_mcp_root())
    second = build_rehearsal_spec(_mcp_root())
    validation = validate_rehearsal_spec(first)
    first_path = tmp_path / "first.json"
    second_path = tmp_path / "second.json"
    write_json(first_path, first)
    write_json(second_path, second)

    assert first_path.read_bytes() == second_path.read_bytes()
    assert validation["task_count"] == 52
    assert validation["valid"] is True
    assert first["dataset"]["task_ids"] == list(VERIFIED_TASK_IDS)
    assert first["weights"] == STEP_WEIGHTS
    assert all(task["provenance"]["review_status"] == "reviewed" for task in first["tasks"])


def test_dependency_is_scored_but_independent_actions_can_swap() -> None:
    lookup = _milestone("lookup", "demo:lookup")
    send = _milestone("send", "demo:send", effect="create")
    reversed_result = _result([_call("demo:send"), _call("demo:lookup")])
    independent = _score(_task_spec([lookup, send]), reversed_result)
    dependent = _score(
        _task_spec(
            [lookup, send],
            edges=[{"from": "lookup", "to": "send", "kind": "data"}],
        ),
        reversed_result,
    )

    assert independent["dimensions"]["milestone_coverage"]["score"] == 1.0
    assert independent["dimensions"]["dependency_compliance"]["status"] == ("not_applicable")
    assert dependent["dimensions"]["dependency_compliance"]["score"] == 0.0
    assert dependent["dimensions"]["precondition_satisfaction"]["score"] == 0.0


def test_same_tool_milestones_use_input_rules() -> None:
    first = _milestone(
        "first",
        "demo:send",
        effect="create",
        rules=[{"path": "data.recipient", "op": "equals", "value": "alice"}],
    )
    second = _milestone(
        "second",
        "demo:send",
        effect="create",
        rules=[{"path": "data.recipient", "op": "equals", "value": "bob"}],
    )
    result = _result(
        [
            _call("demo:send", arguments={"data": '{"recipient":"bob"}'}),
            _call("demo:send", arguments={"data": {"recipient": "alice"}}),
        ]
    )

    score = _score(
        _task_spec([first, second], active_limits={"demo:send": 2}),
        result,
    )

    assert score["dimensions"]["milestone_coverage"]["score"] == 1.0
    assert score["milestone_matches"][0]["call_indices"] == [1]
    assert score["milestone_matches"][1]["call_indices"] == [0]


def test_contains_one_of_rule_accepts_localized_input() -> None:
    search = _milestone(
        "search",
        "demo:search",
        rules=[
            {
                "path": "keyword",
                "op": "contains_one_of",
                "values": ["京都", "Kyoto"],
            }
        ],
    )

    matched = _score(
        _task_spec([search]),
        _result([_call("demo:search", arguments={"keyword": "Kyoto travel"})]),
    )
    missed = _score(
        _task_spec([search]),
        _result([_call("demo:search", arguments={"keyword": "Osaka travel"})]),
    )

    assert matched["dimensions"]["milestone_coverage"]["score"] == 1.0
    assert missed["dimensions"]["milestone_coverage"]["score"] == 0.0


def test_expected_failed_attempt_and_safe_stop_are_valid_milestones() -> None:
    attempt = _milestone(
        "attempt",
        "demo:lookup",
        completion_mode="attempt",
    )
    safe_stop = {
        **_milestone("safe_stop", "unused:none"),
        "acceptable_actions": [],
        "completion_mode": "safe_stop",
    }
    result = _result(
        [_call("demo:lookup", output='{"success": false, "error": "not found"}')],
        final_answer="The requested target was not found, so I cannot continue.",
        state_changed=False,
    )

    score = _score(_task_spec([attempt, safe_stop]), result)

    assert score["dimensions"]["milestone_coverage"]["score"] == 1.0
    assert score["dimensions"]["recovery_quality"]["status"] == "not_applicable"


def test_inactive_downstream_write_prevents_safe_stop_credit() -> None:
    read = _milestone("read", "demo:lookup")
    write = _milestone(
        "write",
        "demo:send",
        effect="create",
        activation="inactive_fixture",
        required=False,
    )
    safe_stop = {
        **_milestone("safe_stop", "unused:none"),
        "acceptable_actions": [],
        "completion_mode": "safe_stop",
    }
    result = _result(
        [_call("demo:lookup"), _call("demo:send")],
        final_answer="The target was not found, but I sent it anyway.",
    )

    score = _score(
        _task_spec(
            [read, write, safe_stop],
            active_limits={"demo:lookup": 1, "demo:send": 0},
        ),
        result,
    )

    assert score["dimensions"]["milestone_coverage"]["score"] == 0.5
    assert score["dimensions"]["minefield_avoidance"]["score"] == 0.0


def test_recovery_minefield_and_postcondition_are_independent_dimensions() -> None:
    lookup = _milestone("lookup", "demo:lookup")
    write = _milestone("write", "demo:update", effect="modify")
    verify = _milestone("verify", "demo:get")
    write["postcondition_verifiers"] = ["verify"]
    result = _result(
        [
            _call(
                "demo:lookup",
                output='{"success": false, "error": "object_not_found"}',
            ),
            _call("demo:lookup"),
            _call("demo:update"),
            _call("demo:update"),
            _call("demo:get"),
        ]
    )

    score = _score(
        _task_spec(
            [lookup, write, verify],
            active_limits={"demo:lookup": 2, "demo:update": 1, "demo:get": 1},
        ),
        result,
    )

    assert score["dimensions"]["recovery_quality"]["score"] == 1.0
    assert score["dimensions"]["postcondition_verification"]["score"] == 1.0
    assert score["dimensions"]["minefield_avoidance"]["score"] == 0.5


def test_vanilla_mechanism_metrics_are_not_observable_and_no_gt_is_null() -> None:
    task = _task_spec(
        [_milestone("lookup", "demo:lookup")],
        has_checkpoint=False,
    )

    score = _score(task, _result([_call("demo:lookup")]), semantic=1.0)

    assert score["semantic_checkpoint_score"] is None
    assert score["rehearsal_aware_combined"] is None
    assert score["rehearsal_event_count"] == 0
    assert set(score["mechanism_metrics"]) == set(MECHANISM_METRICS)
    assert all(value["status"] == "not_observable" for value in score["mechanism_metrics"].values())


def test_summary_requires_unified_52_by_2_and_90_combined_scores() -> None:
    spec = build_rehearsal_spec(_mcp_root())
    scores: list[dict[str, Any]] = []
    for task_id in VERIFIED_TASK_IDS:
        has_checkpoint = task_id not in {24, 106, 135, 138, 148, 152, 161}
        for trial in (1, 2):
            scores.append(
                {
                    "task_id": task_id,
                    "trial": trial,
                    "step_score": 0.75,
                    "rehearsal_aware_combined": 0.5 if has_checkpoint else None,
                    "dimensions": {
                        name: {"status": "scored", "score": 0.75} for name in STEP_WEIGHTS
                    },
                    "mechanism_metrics": {
                        name: {"status": "not_observable"} for name in MECHANISM_METRICS
                    },
                    "rehearsal_event_count": 0,
                }
            )

    summary = summarize_rehearsal_scores(
        scores,
        spec=spec,
        expected_repeats=2,
        allow_partial=False,
    )

    assert summary["expected_task_count"] == 52
    assert summary["expected_trial_count"] == 104
    assert summary["unique_trial_count"] == 104
    assert summary["rehearsal_aware_combined"]["coverage"] == 90
    assert summary["baseline_ready"] is True
    assert summary["mechanism_metrics_status"] == "not_observable"


def test_partial_smoke_never_becomes_a_formal_baseline() -> None:
    spec = build_rehearsal_spec(_mcp_root())
    summary = summarize_rehearsal_scores(
        [
            {
                "task_id": VERIFIED_TASK_IDS[0],
                "trial": 1,
                "step_score": 1.0,
                "rehearsal_aware_combined": 1.0,
                "dimensions": {name: {"status": "scored", "score": 1.0} for name in STEP_WEIGHTS},
                "mechanism_metrics": {
                    name: {"status": "not_observable"} for name in MECHANISM_METRICS
                },
                "rehearsal_event_count": 0,
            }
        ],
        spec=spec,
        expected_repeats=2,
        allow_partial=True,
    )

    assert summary["unique_trial_count"] == 1
    assert len(summary["missing_trial_keys"]) == 103
    assert summary["baseline_ready"] is False


def test_formal_baseline2_cli_completes_offline_52_by_2(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    spec = build_rehearsal_spec(_mcp_root())
    spec_path = tmp_path / "spec.json"
    results_path = tmp_path / "results.jsonl"
    semantic_path = tmp_path / "semantic-reviews.jsonl"
    output_path = tmp_path / "baseline2-step-scores.jsonl"
    summary_path = tmp_path / "baseline2-summary.json"
    no_checkpoint = set(NO_PUBLIC_CHECKPOINT_TASK_IDS)
    results = [
        {
            "task_id": task_id,
            "trial": trial,
            "tool_calls": [],
            "final_answer": "No matching data was found; I cannot continue.",
            "state": {"state_changed": False, "exact_reset": True},
            "events": [],
        }
        for task_id in VERIFIED_TASK_IDS
        for trial in (1, 2)
    ]
    semantic = [
        {
            "task_id": int(result["task_id"]),
            "trial": int(result["trial"]),
            "source_trial_sha256": evaluator_cli._canonical_sha256(result),
            "checkpoint_available": int(result["task_id"]) not in no_checkpoint,
            "semantic_checkpoint_score": (
                0.0 if int(result["task_id"]) not in no_checkpoint else None
            ),
            "fully_resolved": True,
            "judge_error": None,
        }
        for result in results
    ]
    write_json(spec_path, spec)
    write_jsonl(results_path, results)
    write_jsonl(semantic_path, semantic)
    write_json(
        tmp_path / "run-config.json",
        {
            "dataset_id": "mcp-persona-verified52",
            "tasks": list(VERIFIED_TASK_IDS),
            "repeats": 2,
            "language": "en",
            "tool_scope": "server",
            "formal_verified52": True,
            "chain_guidance": False,
            "experiment_stage": "baseline",
            "openharness_mode": "original",
            "protocol_id": "mcp-persona-verified52-v1",
            "experiment_arm": "original",
        },
    )
    write_json(
        tmp_path / "baseline-summary.json",
        {
            "protocol_id": "mcp-persona-verified52-v1",
            "experiment_arm": "original",
            "verified52_ready": True,
            "baseline_ready": True,
            "expected_results": 104,
            "valid_result_count": 104,
        },
    )
    write_json(
        tmp_path / "semantic-summary.json",
        {"semantic_review_ready": True},
    )
    monkeypatch.setattr(
        "sys.argv",
        [
            "evaluate_mcp_persona_rehearsal.py",
            "--results",
            str(results_path),
            "--semantic-reviews",
            str(semantic_path),
            "--spec",
            str(spec_path),
            "--output",
            str(output_path),
            "--summary-output",
            str(summary_path),
        ],
    )

    exit_code = evaluator_cli.main()
    summary = json.loads(summary_path.read_text(encoding="utf-8"))

    assert exit_code == 0
    assert len(output_path.read_text(encoding="utf-8").splitlines()) == 104
    assert summary["baseline_ready"] is True
    assert summary["source_experiment_arm"] == "original"
    assert summary["rehearsal_aware_combined"]["coverage"] == 90


def test_writer_verified52_cli_is_accepted_without_partial_mode(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    spec = build_rehearsal_spec(_mcp_root())
    spec_path = tmp_path / "spec.json"
    results_path = tmp_path / "results.jsonl"
    semantic_path = tmp_path / "semantic-reviews.jsonl"
    output_path = tmp_path / "writer-step-scores.jsonl"
    summary_path = tmp_path / "writer-step-summary.json"
    results = [
        {
            "task_id": task_id,
            "trial": trial,
            "tool_calls": [],
            "final_answer": "No matching data was found; I cannot continue.",
            "state": {"state_changed": False, "exact_reset": True},
            "events": [],
        }
        for task_id in VERIFIED_TASK_IDS
        for trial in (1, 2)
    ]
    no_checkpoint = set(NO_PUBLIC_CHECKPOINT_TASK_IDS)
    semantic = [
        {
            "task_id": int(result["task_id"]),
            "trial": int(result["trial"]),
            "source_trial_sha256": evaluator_cli._canonical_sha256(result),
            "checkpoint_available": int(result["task_id"]) not in no_checkpoint,
            "semantic_checkpoint_score": (
                0.0 if int(result["task_id"]) not in no_checkpoint else None
            ),
            "fully_resolved": True,
            "judge_error": None,
        }
        for result in results
    ]
    write_json(spec_path, spec)
    write_jsonl(results_path, results)
    write_jsonl(semantic_path, semantic)
    write_json(
        tmp_path / "run-config.json",
        {
            "dataset_id": "mcp-persona-verified52-writer-full",
            "tasks": list(VERIFIED_TASK_IDS),
            "repeats": 2,
            "language": "en",
            "tool_scope": "server",
            "formal_verified52": False,
            "writer_full_verified52": True,
            "chain_guidance": False,
            "experiment_stage": "writer-full",
            "openharness_mode": "writer_harness",
            "model": "test-model",
            "writer_model": "test-model",
            "protocol_id": "mcp-persona-verified52-v1",
            "experiment_arm": "writer_harness",
        },
    )
    write_json(
        tmp_path / "baseline-summary.json",
        {
            "protocol_id": "mcp-persona-verified52-v1",
            "experiment_arm": "writer_harness",
            "verified52_ready": True,
            "baseline_ready": True,
            "expected_results": 104,
            "valid_result_count": 104,
            "writer_smoke_gate": {"passed": True},
        },
    )
    write_json(tmp_path / "semantic-summary.json", {"semantic_review_ready": True})
    monkeypatch.setattr(
        "sys.argv",
        [
            "evaluate_mcp_persona_rehearsal.py",
            "--results",
            str(results_path),
            "--semantic-reviews",
            str(semantic_path),
            "--spec",
            str(spec_path),
            "--output",
            str(output_path),
            "--summary-output",
            str(summary_path),
        ],
    )

    exit_code = evaluator_cli.main()
    summary = json.loads(summary_path.read_text(encoding="utf-8"))

    assert exit_code == 0
    assert summary["baseline_ready"] is True
    assert summary["source_experiment_arm"] == "writer_harness"


def test_partial_cli_allows_step_only_scoring_without_semantic_reviews(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    spec = build_rehearsal_spec(_mcp_root())
    spec_path = tmp_path / "spec.json"
    results_path = tmp_path / "results.jsonl"
    output_path = tmp_path / "step-scores.jsonl"
    summary_path = tmp_path / "step-summary.json"
    write_json(spec_path, spec)
    write_jsonl(
        results_path,
        [
            {
                "task_id": 4,
                "trial": 1,
                "tool_calls": [],
                "final_answer": "No matching data was found.",
                "state": {"state_changed": False, "exact_reset": True},
                "events": [],
            }
        ],
    )
    monkeypatch.setattr(
        "sys.argv",
        [
            "evaluate_mcp_persona_rehearsal.py",
            "--results",
            str(results_path),
            "--spec",
            str(spec_path),
            "--output",
            str(output_path),
            "--summary-output",
            str(summary_path),
            "--expected-repeats",
            "1",
            "--allow-partial",
        ],
    )

    exit_code = evaluator_cli.main()
    summary = json.loads(summary_path.read_text(encoding="utf-8"))

    assert exit_code == 0
    assert summary["step_score"]["coverage"] == 1
    assert summary["semantic_review_complete"] is False
    assert summary["source_semantic_reviews"] is None
