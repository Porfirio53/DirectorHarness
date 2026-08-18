from __future__ import annotations

import argparse
import json
from pathlib import Path

import pytest

from scripts.run_mcp_persona_week1 import (
    _build_run_config,
    _classify_trial,
    _director_event_validation,
    _fatal_account_failure,
    _load_existing_results,
    _prepare_output_dir,
    _redact_secret,
    _run_status,
    _summary_for_results,
    _validate_result_slots,
    _write_results_jsonl,
    async_main,
    summarize_existing_output,
)
from openharness.rehearsal.mcp_persona_rehearsal import (
    VERIFIED52_PROTOCOL_ID,
    VERIFIED_TASK_IDS,
)


def _result(
    task_id: int,
    trial: int,
    *,
    errors: list[str] | None = None,
    traceback_value: str | None = None,
    exact_reset: bool = True,
    tool_error: bool = False,
    local_pass: bool = True,
) -> dict[str, object]:
    return {
        "task_id": task_id,
        "trial": trial,
        "errors": errors or [],
        "exception_traceback": traceback_value,
        "state": {"exact_reset": exact_reset},
        "tool_calls": [{"is_error": tool_error}],
        "usage": {"input_tokens": 10, "output_tokens": 2},
        "duration_seconds": 1.5,
        "local_scores": {
            "local_pass": local_pass,
            "sequence_score": 1.0 if local_pass else 0.0,
            "execution_score": 1.0 if local_pass else 0.0,
        },
    }


def _run_config(task_ids: list[int], repeats: int = 2) -> dict[str, object]:
    formal = tuple(task_ids) == VERIFIED_TASK_IDS and repeats == 2
    return {
        "schema_version": 1,
        "dataset_id": (
            "mcp-persona-verified52"
            if formal
            else "mcp-persona-development-run"
        ),
        "formal_verified52": formal,
        "tasks": task_ids,
        "repeats": repeats,
        "language": "en",
        "tool_scope": "server",
        "model": "test-model",
        "chain_guidance": False,
        "experiment_stage": "baseline",
        "openharness_mode": "original",
    }


def test_load_existing_results_groups_resumable_trials(tmp_path: Path) -> None:
    path = tmp_path / "results.jsonl"
    rows = [
        {"task_id": 53, "trial": 1},
        {"task_id": 70, "trial": 1},
        {"task_id": 53, "trial": 2},
    ]
    path.write_text(
        "".join(json.dumps(value) + "\n" for value in rows),
        encoding="utf-8",
    )

    grouped = _load_existing_results(path)

    assert [value["trial"] for value in grouped[53]] == [1, 2]
    assert [value["trial"] for value in grouped[70]] == [1]


def test_load_existing_results_rejects_duplicate_and_invalid_slots(
    tmp_path: Path,
) -> None:
    duplicate = tmp_path / "duplicate.jsonl"
    duplicate.write_text(
        '{"task_id": 53, "trial": 1}\n{"task_id": 53, "trial": 1}\n',
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="Duplicate resumable result"):
        _load_existing_results(duplicate)

    invalid = tmp_path / "invalid.jsonl"
    invalid.write_text('{"task_id": 53, "trial": 0}\n', encoding="utf-8")
    with pytest.raises(ValueError, match="Invalid resumable result"):
        _load_existing_results(invalid)


@pytest.mark.parametrize(
    ("row", "expected"),
    [
        (_result(1, 1), "completed"),
        (_result(1, 1, local_pass=False), "completed"),
        (_result(1, 1, tool_error=True), "completed_with_tool_errors"),
        (
            _result(1, 1, errors=["Exceeded maximum turn limit (30)"]),
            "agent_task_failed",
        ),
        (
            _result(
                1,
                1,
                errors=["Model returned an empty assistant message"],
            ),
            "agent_task_failed",
        ),
        (
            _result(
                1,
                1,
                errors=["Exceeded trial wall-clock limit (300 seconds)"],
            ),
            "agent_task_failed",
        ),
        (
            _result(1, 1, errors=["API error: code=Arrearage; overdue-payment"]),
            "infrastructure_failed",
        ),
        (
            _result(1, 1, errors=["MCP connection failed: refused"]),
            "infrastructure_failed",
        ),
        (
            _result(1, 1, errors=["TimeoutError: timed out"]),
            "infrastructure_failed",
        ),
        (
            _result(1, 1, traceback_value="Traceback ..."),
            "infrastructure_failed",
        ),
        (_result(1, 1, exact_reset=False), "infrastructure_failed"),
    ],
)
def test_trial_failure_classification(
    row: dict[str, object],
    expected: str,
) -> None:
    assert _classify_trial(row) == expected
    status = _run_status(row)
    assert status["baseline_valid"] is (expected != "infrastructure_failed")
    assert status["retry_eligible"] is (expected == "infrastructure_failed")


def test_trial_wall_clock_timeout_has_distinct_agent_status() -> None:
    row = _result(
        1,
        1,
        errors=["Exceeded trial wall-clock limit (300 seconds)"],
    )

    assert _run_status(row) == {
        "classification": "agent_task_failed",
        "detail": "agent_trial_timeout",
        "baseline_valid": True,
        "retry_eligible": False,
    }


def test_director_validation_excludes_preflight_input_rejections() -> None:
    valid_call = {
        "tool_name": "mcp__server__valid",
        "output": "ok",
        "is_error": False,
    }
    invalid_call = {
        "tool_name": "mcp__server__invalid",
        "output": (
            "Invalid input for mcp__server__invalid: required field missing"
        ),
        "is_error": True,
    }
    validation = _director_event_validation(
        enabled=True,
        tool_calls=[valid_call, invalid_call],
        trajectory_events=[
            {"type": "tool_started", **valid_call},
            {
                "type": "director_event",
                "event": "tool_check",
                "tool_use_id": "call-valid",
            },
            {"type": "tool_completed", **valid_call},
            {"type": "tool_started", **invalid_call},
            {"type": "tool_completed", **invalid_call},
        ],
    )

    assert validation is not None
    assert validation["tool_call_count"] == 2
    assert validation["checked_tool_use_count"] == 1
    assert validation["all_tool_calls_checked"] is True
    assert validation["director_before_tool_completion"] is True


def test_director_validation_does_not_hide_executed_tool_errors() -> None:
    failed_call = {
        "tool_name": "mcp__server__failed",
        "output": "remote tool returned an error",
        "is_error": True,
    }
    validation = _director_event_validation(
        enabled=True,
        tool_calls=[failed_call],
        trajectory_events=[
            {"type": "tool_started", **failed_call},
            {"type": "tool_completed", **failed_call},
        ],
    )

    assert validation is not None
    assert validation["tool_call_count"] == 1
    assert validation["checked_tool_use_count"] == 0
    assert validation["all_tool_calls_checked"] is False
    assert validation["director_before_tool_completion"] is False


def test_locked_output_requires_resume_and_exact_configuration(
    tmp_path: Path,
) -> None:
    output_dir = tmp_path / "baseline"
    requested = _run_config([1, 2])

    locked = _prepare_output_dir(output_dir, dict(requested), resume=False)
    resumed = _prepare_output_dir(output_dir, dict(requested), resume=True)

    assert resumed == locked
    assert "created_at" in locked
    with pytest.raises(ValueError, match="pass --resume"):
        _prepare_output_dir(output_dir, dict(requested), resume=False)
    changed = dict(requested)
    changed["model"] = "different-model"
    with pytest.raises(ValueError, match="mismatched fields: model"):
        _prepare_output_dir(output_dir, changed, resume=True)


def test_locked_output_refuses_untracked_nonempty_directory(
    tmp_path: Path,
) -> None:
    output_dir = tmp_path / "baseline"
    output_dir.mkdir()
    (output_dir / "unrelated.txt").write_text("keep", encoding="utf-8")

    with pytest.raises(ValueError, match="refusing to mix"):
        _prepare_output_dir(output_dir, _run_config([1]), resume=True)


def test_canonical_results_are_unique_and_validate_locked_slots(
    tmp_path: Path,
) -> None:
    path = tmp_path / "results.jsonl"
    grouped = {
        2: [_result(2, 2), _result(2, 1)],
        1: [_result(1, 1), _result(1, 2, local_pass=False)],
    }

    _write_results_jsonl(path, grouped)
    loaded = _load_existing_results(path)
    _validate_result_slots(loaded, [1, 2], 2)

    keys = [
        (row["task_id"], row["trial"])
        for values in loaded.values()
        for row in values
    ]
    assert keys == [(1, 1), (1, 2), (2, 1), (2, 2)]

    loaded[3] = [_result(3, 1)]
    with pytest.raises(ValueError, match="not in the locked run"):
        _validate_result_slots(loaded, [1, 2], 2)


def test_canonical_results_reject_infrastructure_failure() -> None:
    grouped = {
        1: [_result(1, 1, errors=["API error: Arrearage"])],
    }

    with pytest.raises(ValueError, match="retryable infrastructure failure"):
        _validate_result_slots(grouped, [1], 2)


def test_baseline_summary_accepts_valid_agent_failures_and_low_scores() -> None:
    task_ids = list(VERIFIED_TASK_IDS)
    grouped: dict[int, list[dict[str, object]]] = {}
    for task_id in task_ids:
        grouped[task_id] = [
            _result(task_id, 1, local_pass=task_id % 2 == 0),
            _result(
                task_id,
                2,
                errors=["Exceeded maximum turn limit (30)"]
                if task_id == VERIFIED_TASK_IDS[-1]
                else None,
            ),
        ]

    summary = _summary_for_results(grouped, _run_config(task_ids))

    assert summary["expected_results"] == 104
    assert summary["result_count"] == 104
    assert summary["valid_result_count"] == 104
    assert summary["state_counts"]["agent_task_failed"] == 1
    assert summary["exact_reset_count"] == 104
    assert summary["missing_results"] == []
    assert summary["analysis_ready"] is True
    assert summary["verified52_ready"] is True
    assert summary["formal_original_baseline"] is True
    assert summary["baseline_ready"] is True


def test_only_exact_blind_verified52_can_be_baseline_ready() -> None:
    wrong_task_ids = list(range(1, 53))
    wrong_grouped = {
        task_id: [_result(task_id, 1), _result(task_id, 2)]
        for task_id in wrong_task_ids
    }
    wrong = _summary_for_results(wrong_grouped, _run_config(wrong_task_ids))

    exact_task_ids = list(VERIFIED_TASK_IDS)
    exact_grouped = {
        task_id: [_result(task_id, 1), _result(task_id, 2)]
        for task_id in exact_task_ids
    }
    guided_config = _run_config(exact_task_ids)
    guided_config["chain_guidance"] = True
    guided = _summary_for_results(exact_grouped, guided_config)

    assert wrong["run_complete"] is True
    assert wrong["baseline_ready"] is False
    assert guided["run_complete"] is True
    assert guided["baseline_ready"] is False


def test_baseline_summary_reports_missing_and_infrastructure_slots() -> None:
    grouped = {
        1: [_result(1, 1)],
        2: [_result(2, 1, errors=["TimeoutError: timed out"])],
    }

    summary = _summary_for_results(grouped, _run_config([1, 2]))

    assert summary["result_count"] == 2
    assert summary["valid_result_count"] == 1
    assert summary["state_counts"] == {
        "completed": 1,
        "infrastructure_failed": 1,
        "pending": 2,
    }
    assert summary["missing_results"] == [
        {"task_id": 1, "trial": 2},
        {"task_id": 2, "trial": 2},
    ]
    assert summary["exact_reset_count"] == 2
    assert summary["baseline_ready"] is False


def test_run_config_is_secret_free() -> None:
    secret = "test-api-key-must-not-appear"
    args = argparse.Namespace(
        repeats=2,
        language="en",
        mode="full",
        model="qwen-test",
        tool_scope="server",
        max_turns=30,
        max_tokens=4096,
        temperature=0.0,
        seed=42,
        api_timeout=180.0,
        trial_timeout=300.0,
        enable_thinking=False,
        tool_output_inline_chars=200_000,
        chain_guidance=False,
    )
    manifest = {
        "model": {"api_key_present": True, "api_key_value_recorded": False},
        "openharness": {"root": "/repo/openharness", "commit": "abc"},
        "mcp_persona": {
            "repository": "https://example.test/repo",
            "root": "/repo/mcp-persona",
            "commit": "def",
            "external_environment": {"details": secret},
        },
        "file_sha256": {"runner.py": "123"},
    }

    config = _build_run_config(
        args=args,
        task_ids=[1],
        root=Path("/repo/mcp-persona"),
        openharness_root=Path("/repo/openharness"),
        env_file=Path("/repo/.env"),
        api_base="https://api.example.test/v1",
        version_manifest=manifest,
    )

    serialized = json.dumps(config)
    assert secret not in serialized
    assert config["model_endpoint"] == {
        "scheme": "https",
        "api_base_host": "api.example.test",
        "api_base_path": "/v1",
    }


def test_run_config_marks_only_the_exact_verified52_command_as_formal() -> None:
    args = argparse.Namespace(
        repeats=2,
        language="en",
        mode="full",
        model="qwen-test",
        tool_scope="server",
        max_turns=30,
        max_tokens=4096,
        temperature=0.0,
        seed=42,
        api_timeout=180.0,
        trial_timeout=300.0,
        enable_thinking=False,
        tool_output_inline_chars=200_000,
        chain_guidance=False,
    )
    manifest = {
        "openharness": {"root": "/repo/openharness", "commit": "abc"},
        "mcp_persona": {
            "root": "/repo/mcp-persona",
            "commit": "def",
            "external_environment": {},
        },
        "runtime": {"python": "3.11", "packages": {}},
        "file_sha256": {},
    }

    formal = _build_run_config(
        args=args,
        task_ids=list(VERIFIED_TASK_IDS),
        root=Path("/repo/mcp-persona"),
        openharness_root=Path("/repo/openharness"),
        env_file=Path("/repo/.env"),
        api_base="https://api.example.test/v1",
        version_manifest=manifest,
    )
    reordered_ids = list(VERIFIED_TASK_IDS)
    reordered_ids[0], reordered_ids[1] = reordered_ids[1], reordered_ids[0]
    development = _build_run_config(
        args=args,
        task_ids=reordered_ids,
        root=Path("/repo/mcp-persona"),
        openharness_root=Path("/repo/openharness"),
        env_file=Path("/repo/.env"),
        api_base="https://api.example.test/v1",
        version_manifest=manifest,
    )

    assert formal["formal_verified52"] is True
    assert formal["dataset_id"] == "mcp-persona-verified52"
    assert "writer_full_verified52" not in formal
    assert development["formal_verified52"] is False
    assert development["dataset_id"] == "mcp-persona-development-run"
    assert "writer_full_verified52" not in development


def test_run_config_marks_one_repeat_writer_verified52_as_full_writer() -> None:
    args = argparse.Namespace(
        repeats=1,
        language="en",
        mode="full",
        model="qwen3.6-plus",
        tool_scope="server",
        max_turns=30,
        max_tokens=4096,
        temperature=0.0,
        seed=42,
        api_timeout=180.0,
        trial_timeout=300.0,
        enable_thinking=False,
        tool_output_inline_chars=200_000,
        chain_guidance=False,
        experiment_stage="writer-full",
        openharness_mode="writer_harness",
        writer_model="qwen3.6-plus",
        writer_max_tokens=4096,
    )
    manifest = {
        "openharness": {"root": "/repo/openharness", "commit": "abc"},
        "mcp_persona": {
            "root": "/repo/mcp-persona",
            "commit": "def",
            "external_environment": {},
        },
        "runtime": {"python": "3.11", "packages": {}},
        "file_sha256": {},
    }

    config = _build_run_config(
        args=args,
        task_ids=list(VERIFIED_TASK_IDS),
        root=Path("/repo/mcp-persona"),
        openharness_root=Path("/repo/openharness"),
        env_file=Path("/repo/.env"),
        api_base="https://api.example.test/v1",
        version_manifest=manifest,
    )

    assert config["formal_verified52"] is False
    assert config["writer_full_selection"] is True
    assert "writer_full_verified52" not in config
    assert config["protocol_id"] is None
    assert config["dataset_id"] == "mcp-persona-verified52-writer-full"
    assert config["openharness_mode"] == "writer_harness"
    assert config["writer_model"] == config["model"] == "qwen3.6-plus"


def test_writer_52x2_is_ready_but_one_repeat_is_analysis_only() -> None:
    task_ids = list(VERIFIED_TASK_IDS)
    grouped = {
        task_id: [_result(task_id, 1), _result(task_id, 2)]
        for task_id in task_ids
    }
    for rows in grouped.values():
        for row in rows:
            row["writer"] = {
                "mandatory_passed": True,
                "planning_state_unchanged": True,
                "event_validation": {"events_complete": True},
                "usage": {"input_tokens": 1, "output_tokens": 1},
            }
    writer_config = {
        **_run_config(task_ids),
        "dataset_id": "mcp-persona-verified52-writer-full",
        "formal_verified52": False,
        "writer_full_verified52": True,
        "experiment_stage": "writer-full",
        "openharness_mode": "writer_harness",
        "writer_model": "test-model",
        "protocol_id": VERIFIED52_PROTOCOL_ID,
        "experiment_arm": "writer_harness",
    }

    full = _summary_for_results(grouped, writer_config)
    one_repeat_config = {**writer_config, "repeats": 1}
    one_repeat = _summary_for_results(
        {task_id: [rows[0]] for task_id, rows in grouped.items()},
        one_repeat_config,
    )

    assert full["writer_smoke_gate"]["passed"] is True
    assert full["analysis_ready"] is True
    assert full["verified52_ready"] is True
    assert full["formal_original_baseline"] is False
    assert full["baseline_ready"] is True
    assert one_repeat["analysis_ready"] is True
    assert one_repeat["verified52_ready"] is False
    assert one_repeat["baseline_ready"] is False


def test_resume_ignores_source_provenance_changes(tmp_path: Path) -> None:
    output_dir = tmp_path / "baseline"
    requested = {
        **_run_config([1, 2]),
        "source_sha256": {"runner": "old"},
        "runtime_fingerprint_sha256": "old-runtime",
        "openharness_git": {"commit": "old"},
        "writer_deployment": {"archive_sha256": "old"},
        "source_lock_enforced": False,
    }
    locked = _prepare_output_dir(output_dir, dict(requested), resume=False)
    resumed_request = {
        **requested,
        "source_sha256": {"runner": "new"},
        "runtime_fingerprint_sha256": "new-runtime",
        "openharness_git": {"commit": "new"},
        "writer_deployment": {"archive_sha256": "new"},
    }
    resumed_request.pop("source_lock_enforced")

    resumed = _prepare_output_dir(output_dir, resumed_request, resume=True)

    assert resumed == locked


@pytest.mark.asyncio
async def test_summarize_only_never_initializes_runtime_or_credentials(
    tmp_path: Path,
) -> None:
    output_dir = tmp_path / "offline"
    output_dir.mkdir()
    (output_dir / "run-config.json").write_text(
        json.dumps(_run_config([1], repeats=1)),
        encoding="utf-8",
    )
    (output_dir / "results.jsonl").write_text(
        json.dumps(_result(1, 1)) + "\n",
        encoding="utf-8",
    )
    expected = summarize_existing_output(output_dir)

    exit_code = await async_main(
        argparse.Namespace(summarize_only=True, output_dir=output_dir)
    )

    assert exit_code == 0
    assert expected["analysis_ready"] is True


def test_persisted_failures_redact_api_key_and_account_errors_fail_fast() -> None:
    secret = "sk-secret-value"
    value = {
        "errors": [f"Authorization: Bearer {secret}; code=Arrearage"],
        "exception_traceback": f"request(api_key={secret})",
        "events": [{"message": secret}],
    }

    redacted = _redact_secret(value, secret)

    assert secret not in json.dumps(redacted)
    assert "[REDACTED_API_KEY]" in json.dumps(redacted)
    assert _fatal_account_failure(redacted) is True
