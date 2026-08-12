from __future__ import annotations

import argparse
import json
import os
from pathlib import Path

import pytest

from scripts import review_mcp_persona_semantics as semantics
from openharness.rehearsal.mcp_persona_rehearsal import VERIFIED_TASK_IDS


def _mcp_root() -> Path:
    configured = os.environ.get("MCP_PERSONA_ROOT")
    root = (
        Path(configured)
        if configured
        else Path(__file__).resolve().parents[3] / "MCP-Persona"
    )
    if not (root / "data/tasks/en_release_data.json").is_file():
        pytest.skip("MCP-Persona checkout is unavailable")
    return root.resolve()


def _args(tmp_path: Path, *, resume: bool) -> argparse.Namespace:
    task_file = tmp_path / "data/tasks/en_release_data.json"
    task_file.parent.mkdir(parents=True, exist_ok=True)
    if not task_file.exists():
        task_file.write_text("[]\n", encoding="utf-8")
    results = tmp_path / "results.jsonl"
    if not results.exists():
        results.write_text(
            "".join(
                json.dumps(
                    {
                        "task_id": 1,
                        "trial": trial,
                        "tool_calls": [],
                        "final_answer": "done",
                        "state": {"state_changed": False},
                    }
                )
                + "\n"
                for trial in (1, 2)
            ),
            encoding="utf-8",
        )
    return argparse.Namespace(
        mcp_persona_root=tmp_path,
        results=results,
        summary=None,
        output=tmp_path / "reviews.jsonl",
        summary_output=tmp_path / "summary.json",
        env_file=None,
        language="en",
        model="unused",
        task_ids=[1],
        repeats=2,
        require_verified52=False,
        api_timeout=1.0,
        judge_attempts=3,
        judge_retry_delay=0.0,
        max_evidence_chars=1_000,
        resume=resume,
        reuse_judge_from=None,
        rules_only=True,
    )


@pytest.mark.asyncio
async def test_semantic_review_persists_each_trial_for_resume(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    args = _args(tmp_path, resume=False)
    monkeypatch.setattr(
        semantics,
        "task_by_id",
        lambda *_args, **_kwargs: {
            "gt": [{"checkpoint_type": "personalized_search", "GT_value": "done"}]
        },
    )
    calls = 0

    def interrupt_second(*_args: object, **_kwargs: object) -> tuple[dict[str, object], ...]:
        nonlocal calls
        calls += 1
        if calls == 2:
            raise KeyboardInterrupt
        return (
            {
                "index": 0,
                "checkpoint_type": "personalized_search",
                "status": "pass",
                "score": 1.0,
                "reason": "matched",
            },
        )

    monkeypatch.setattr(semantics, "evaluate_checkpoint_rules", interrupt_second)

    with pytest.raises(KeyboardInterrupt):
        await semantics.async_main(args)

    persisted = semantics._load_rows(args.output)
    assert [(row["task_id"], row["trial"]) for row in persisted] == [(1, 1)]
    assert not args.output.with_suffix(".jsonl.tmp").exists()

    monkeypatch.setattr(
        semantics,
        "evaluate_checkpoint_rules",
        lambda *_args, **_kwargs: (
            {
                "index": 0,
                "checkpoint_type": "personalized_search",
                "status": "pass",
                "score": 1.0,
                "reason": "matched",
            },
        ),
    )

    exit_code = await semantics.async_main(_args(tmp_path, resume=True))

    assert exit_code == 0
    resumed = semantics._load_rows(args.output)
    assert [(row["task_id"], row["trial"]) for row in resumed] == [(1, 1), (1, 2)]

    resumed[0].update(fully_resolved=False, judge_error="Timeout")
    semantics._write_jsonl(args.output, resumed)
    evaluations = 0

    def count_evaluations(
        *_args: object, **_kwargs: object
    ) -> tuple[dict[str, object], ...]:
        nonlocal evaluations
        evaluations += 1
        return (
            {
                "index": 0,
                "checkpoint_type": "personalized_search",
                "status": "pass",
                "score": 1.0,
                "reason": "matched",
            },
        )

    monkeypatch.setattr(semantics, "evaluate_checkpoint_rules", count_evaluations)

    retry_exit_code = await semantics.async_main(_args(tmp_path, resume=True))

    assert retry_exit_code == 0
    assert evaluations == 1
    retried = semantics._load_rows(args.output)
    assert [(row["task_id"], row["trial"]) for row in retried] == [(1, 1), (1, 2)]
    assert all(row["fully_resolved"] for row in retried)


def test_exact_slot_index_rejects_duplicate_missing_and_extra_trial(
    tmp_path: Path,
) -> None:
    source = tmp_path / "results.jsonl"
    complete = [
        {"task_id": task_id, "trial": trial}
        for task_id in VERIFIED_TASK_IDS
        for trial in (1, 2)
    ]

    indexed = semantics._index_rows(
        complete,
        task_ids=VERIFIED_TASK_IDS,
        repeats=2,
        source=source,
        require_complete=True,
    )

    assert len(indexed) == 104
    with pytest.raises(ValueError, match="duplicate"):
        semantics._index_rows(
            [*complete, complete[0]],
            task_ids=VERIFIED_TASK_IDS,
            repeats=2,
            source=source,
            require_complete=True,
        )
    with pytest.raises(ValueError, match="missing 1 slots"):
        semantics._index_rows(
            complete[:-1],
            task_ids=VERIFIED_TASK_IDS,
            repeats=2,
            source=source,
            require_complete=True,
        )
    with pytest.raises(ValueError, match="outside repeats"):
        semantics._index_rows(
            [{"task_id": VERIFIED_TASK_IDS[0], "trial": 3}],
            task_ids=VERIFIED_TASK_IDS,
            repeats=2,
            source=source,
            require_complete=False,
        )


def test_semantic_config_lock_refuses_overwrite_and_mismatch(
    tmp_path: Path,
) -> None:
    output = tmp_path / "semantic-reviews.jsonl"
    first = {"schema_version": 1, "model": "judge-a", "results_sha256": "one"}

    locked = semantics._prepare_review_config(
        output,
        dict(first),
        resume=False,
    )

    assert locked["model"] == "judge-a"
    with pytest.raises(ValueError, match="pass --resume"):
        semantics._prepare_review_config(output, dict(first), resume=False)
    changed = dict(first)
    changed["model"] = "judge-b"
    with pytest.raises(ValueError, match="mismatched fields: model"):
        semantics._prepare_review_config(output, changed, resume=True)


def test_judge_indices_accept_exact_checkpoint_numbers() -> None:
    decisions = {
        1: {"pass": True, "confidence": 0.8, "reason": "first"},
        3: {"pass": False, "confidence": 0.9, "reason": "second"},
    }

    normalized = semantics._normalize_judge_decision_indices(
        decisions,
        [1, 3],
    )

    assert normalized == decisions


def test_judge_indices_normalize_unambiguous_zero_based_positions() -> None:
    decisions = {
        0: {"pass": True, "confidence": 0.8, "reason": "first"},
        1: {"pass": False, "confidence": 0.9, "reason": "second"},
    }

    normalized = semantics._normalize_judge_decision_indices(
        decisions,
        [1, 3],
    )

    assert set(normalized) == {1, 3}
    assert normalized[1]["reason"] == "first"
    assert normalized[3]["reason"] == "second"
    assert all(
        value["index_normalization"]
        == "zero_based_response_position_to_checkpoint_index"
        for value in normalized.values()
    )


def test_judge_indices_reject_ambiguous_mixed_numbering() -> None:
    decisions = {
        0: {"pass": True, "confidence": 0.8, "reason": "first"},
        3: {"pass": False, "confidence": 0.9, "reason": "second"},
    }

    with pytest.raises(
        ValueError,
        match=r"missing=\[1\], unexpected=\[0\]",
    ):
        semantics._normalize_judge_decision_indices(decisions, [1, 3])


@pytest.mark.asyncio
async def test_judge_retries_are_bounded(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    attempts = 0

    async def flaky(*_args: object, **_kwargs: object) -> dict[int, dict[str, object]]:
        nonlocal attempts
        attempts += 1
        if attempts < 3:
            raise TimeoutError("temporary")
        return {0: {"pass": True, "confidence": 1.0, "reason": "proved"}}

    monkeypatch.setattr(semantics, "_judge_unresolved", flaky)

    decisions, error, used = await semantics._judge_with_retries(
        object(),  # type: ignore[arg-type]
        attempts=3,
        retry_delay=0,
        model="judge",
        task={},
        row={},
        unresolved=[{"index": 0}],
        state_evidence="",
        max_evidence_chars=100,
    )

    assert attempts == 3
    assert used == 3
    assert error is None
    assert decisions[0]["pass"] is True


@pytest.mark.asyncio
async def test_rules_only_formal_verified52_preflight_never_calls_model(
    tmp_path: Path,
) -> None:
    results = tmp_path / "results.jsonl"
    semantics._write_jsonl(
        results,
        [
            {
                "task_id": task_id,
                "trial": trial,
                "tool_calls": [],
                "final_answer": "No matching data was found; I cannot continue.",
                "state": {
                    "state_changed": False,
                    "exact_reset": True,
                },
                "run_status": {"baseline_valid": True},
            }
            for task_id in VERIFIED_TASK_IDS
            for trial in (1, 2)
        ],
    )
    semantics._write_json(
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
    semantics._write_json(
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
    args = argparse.Namespace(
        mcp_persona_root=_mcp_root(),
        results=results,
        summary=None,
        output=tmp_path / "semantic-reviews.jsonl",
        summary_output=tmp_path / "semantic-summary.json",
        env_file=None,
        language="en",
        model="must-not-be-called",
        task_ids=list(VERIFIED_TASK_IDS),
        repeats=2,
        require_verified52=True,
        api_timeout=1.0,
        judge_attempts=3,
        judge_retry_delay=0.0,
        max_evidence_chars=1_000,
        resume=False,
        reuse_judge_from=None,
        rules_only=True,
    )

    exit_code = await semantics.async_main(args)
    summary = json.loads(args.summary_output.read_text(encoding="utf-8"))

    assert exit_code == 0
    assert len(semantics._load_rows(args.output)) == 104
    assert summary["dataset_id"] == "mcp-persona-verified52"
    assert summary["experiment_arm"] == "original"
    assert summary["expected_trial_count"] == 104
    assert summary["rules_only"] is True
    assert summary["semantic_review_ready"] is False


def test_verified52_source_validation_accepts_writer_arm(tmp_path: Path) -> None:
    results = tmp_path / "results.jsonl"
    results.write_text("", encoding="utf-8")
    semantics._write_json(
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
    semantics._write_json(
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
    args = argparse.Namespace(results=results, require_verified52=True)

    arm = semantics._validate_verified52_source(args, VERIFIED_TASK_IDS)

    assert arm == "writer_harness"
