from __future__ import annotations

import argparse
import importlib
import json
import sys
from email.message import Message
from pathlib import Path

from scripts.run_openharness_harnessbench import (
    _apply_authoritative_openai_environment,
    _configure_grading_environment,
    _load_environment,
    _load_task_manifest,
    _prepare_output_dir,
    _resume_should_retry,
    _summary_for_output,
)
from scripts.redact_harnessbench_artifacts import redact_tree, sensitive_values


def test_full_grading_environment_is_explicit_and_metadata_has_no_secret() -> None:
    secret = "test-secret-must-not-be-serialized"
    env = {
        "OPENAI_API_KEY": secret,
        "OPENAI_API_BASE": "https://judge.example.test/v1",
    }
    args = argparse.Namespace(
        grading="full",
        rubric_api_key_env="OPENAI_API_KEY",
        rubric_base_url=None,
        rubric_base_url_env="OPENAI_API_BASE",
        rubric_model=None,
        rubric_vision_model=None,
        model="judge-model",
    )

    metadata = _configure_grading_environment(env, args)

    assert env["RUBRIC_API_KEY"] == secret
    assert env["RUBRIC_MODEL"] == "judge-model"
    assert metadata["security_grade"] == "enabled"
    assert secret not in json.dumps(metadata)


def test_root_env_is_authoritative_for_every_harnessbench_model_phase(
    tmp_path: Path,
) -> None:
    env_file = tmp_path / ".env"
    env_file.write_text(
        "OPENAI_API_BASE=https://workspace.example.test/v1\n"
        "OPENAI_API_KEY=test-authoritative-key\n",
        encoding="utf-8",
    )

    env = _load_environment(env_file)

    assert env["OPENHARNESS_BASE_URL"] == "https://workspace.example.test/v1"
    assert env["OPENAI_BASE_URL"] == "https://workspace.example.test/v1"
    assert env["WRITER_BASE_URL"] == "https://workspace.example.test/v1"
    assert env["ACTOR_BASE_URL"] == "https://workspace.example.test/v1"
    assert env["OPENHARNESS_OPENAI_API_KEY"] == "test-authoritative-key"
    assert env["OPENHARNESS_DASHSCOPE_API_KEY"] == "test-authoritative-key"
    assert env["DASHSCOPE_API_KEY"] == "test-authoritative-key"
    assert env["WRITER_API_KEY"] == "test-authoritative-key"
    assert env["ACTOR_API_KEY"] == "test-authoritative-key"


def test_root_env_overrides_stale_runtime_aliases() -> None:
    env = {
        "OPENAI_API_BASE": "https://new-workspace.example.test/v1",
        "OPENAI_API_KEY": "test-new-key",
        "OPENHARNESS_BASE_URL": "https://old-workspace.example.test/v1",
        "OPENHARNESS_DASHSCOPE_API_KEY": "test-old-key",
    }

    _apply_authoritative_openai_environment(env)

    assert env["OPENHARNESS_BASE_URL"] == "https://new-workspace.example.test/v1"
    assert env["OPENHARNESS_DASHSCOPE_API_KEY"] == "test-new-key"


def test_outcome_only_grading_marks_security_unassessed() -> None:
    env: dict[str, str] = {}
    args = argparse.Namespace(grading="outcome-only")

    metadata = _configure_grading_environment(env, args)

    assert env["HARNESSBENCH_SKIP_PROCESS_GRADE"] == "1"
    assert env["HARNESSBENCH_SKIP_ORACLE_QUALITY_LLM"] == "1"
    assert metadata["security_grade"] == "default-pass-unassessed"


def test_locked_output_resume_and_full_summary(tmp_path: Path) -> None:
    output_dir = tmp_path / "baseline"
    requested = {
        "dataset_id": "test-dataset",
        "tasks": ["001-file"],
        "excluded_tasks": [],
        "repeats": 1,
        "model": "test-model",
        "profile": "test",
        "api_format": "openai",
        "temperature": 0,
        "seed": 42,
        "max_turns": 10,
        "api_timeout_sec": 30,
        "openharness_mode": "original",
        "public_url_mode": "loopback",
        "grading": {"mode": "full"},
        "harnessbench_git": {"commit": "abc"},
        "openharness_source_sha256": {"runner": "def"},
    }
    run_config = _prepare_output_dir(output_dir, requested, resume=False)
    resume_request = dict(requested)
    resume_request["harnessbench_git"] = {"commit": "different-commit"}
    resume_request["openharness_source_sha256"] = {"runner": "different-hash"}
    resumed = _prepare_output_dir(output_dir, resume_request, resume=True)
    assert resumed == run_config

    result_file = (
        output_dir
        / "repeat-01"
        / "results"
        / "openharness-local"
        / "test-model"
        / "001-file.json"
    )
    result_file.parent.mkdir(parents=True)
    result_file.write_text(
        json.dumps(
            {
                "task_id": "001-file",
                "adapter_result": {"ok": True},
                "adapter_results": [
                    {"stdout": json.dumps({"status": "completed_with_tool_errors"})}
                ],
                "oracle_result": {
                    "outcome_score": 0.8,
                    "outcome_llm_weight": 0,
                },
                "scoring": {
                    "rubric": {"skipped": False, "parse_error": False},
                    "process_score": 0.75,
                    "security_score": 1,
                    "combined_score": 0.6,
                },
            }
        ),
        encoding="utf-8",
    )

    summary = _summary_for_output(output_dir, run_config)

    assert summary["baseline_ready"] is True
    assert summary["result_count"] == 1
    assert summary["state_counts"] == {"completed": 1}
    assert summary["repeats"][0]["outcome_mean"] == 0.8
    assert summary["repeats"][0]["combined_mean"] == 0.6


def test_resume_retries_only_infrastructure_failures() -> None:
    assert _resume_should_retry(
        {
            "adapter_result": {"ok": False},
            "adapter_results": [
                {"stdout": json.dumps({"status": "openharness_process_failed"})}
            ],
            "oracle_result": {},
        }
    )
    assert _resume_should_retry(
        {
            "adapter_result": {"ok": True},
            "adapter_results": [{"stdout": json.dumps({"status": "completed"})}],
            "oracle_result": {"error": "grading service unavailable"},
        }
    )
    assert not _resume_should_retry(
        {
            "adapter_result": {"ok": True},
            "adapter_results": [
                {"stdout": json.dumps({"status": "agent_task_failed"})}
            ],
            "oracle_result": {},
        }
    )
    assert not _resume_should_retry(
        {
            "adapter_result": {"ok": True},
            "adapter_results": [{"stdout": json.dumps({"status": "completed"})}],
            "oracle_result": {},
        }
    )


def test_agent_task_failure_is_a_complete_scored_result(tmp_path: Path) -> None:
    output_dir = tmp_path / "model-failure"
    run_config = {
        "dataset_id": "test-dataset",
        "tasks": ["001-file"],
        "repeats": 1,
        "model": "test-model",
        "openharness_mode": "original",
        "grading": {"mode": "outcome-only"},
    }
    result_file = (
        output_dir
        / "repeat-01/results/openharness-local/test-model/001-file.json"
    )
    result_file.parent.mkdir(parents=True)
    result_file.write_text(
        json.dumps(
            {
                "task_id": "001-file",
                "adapter_result": {"ok": True},
                "adapter_results": [
                    {"stdout": json.dumps({"status": "agent_task_failed"})}
                ],
                "oracle_result": {"outcome_score": 0.0},
            }
        ),
        encoding="utf-8",
    )

    summary = _summary_for_output(output_dir, run_config)

    assert summary["baseline_ready"] is True
    assert summary["state_counts"] == {"agent_task_failed": 1}


def test_task_manifest_does_not_lock_git_commit_or_dirty_paths(tmp_path: Path) -> None:
    manifest_path = tmp_path / "tasks.json"
    manifest_path.write_text(
        json.dumps(
            {
                "task_count": 1,
                "tasks": ["001-file"],
                "harnessbench_commit": "obsolete-commit",
                "allowed_harnessbench_dirty_paths": [],
            }
        ),
        encoding="utf-8",
    )

    manifest, task_ids = _load_task_manifest(manifest_path, tmp_path)

    assert manifest["harnessbench_commit"] == "obsolete-commit"
    assert task_ids == ("001-file",)


def test_writer_summary_requires_every_round_gate(tmp_path: Path) -> None:
    output_dir = tmp_path / "writer-smoke"
    run_config = {
        "dataset_id": "writer-smoke",
        "tasks": ["001-file"],
        "excluded_tasks": [],
        "repeats": 1,
        "model": "test-model",
        "openharness_mode": "writer_harness",
        "grading": {"mode": "outcome-only"},
    }
    result_file = (
        output_dir
        / "repeat-01/results/openharness-local/test-model/001-file.json"
    )
    result_file.parent.mkdir(parents=True)
    round_payload = {
        "status": "completed",
        "writer_required": True,
        "writer_mandatory_passed": True,
        "writer_state_unchanged": True,
        "writer_event_validation": {"events_complete": True},
    }
    result_file.write_text(
        json.dumps(
            {
                "task_id": "001-file",
                "adapter_result": {"ok": True},
                "adapter_results": [
                    {"stdout": json.dumps(round_payload)}
                ],
                "oracle_result": {
                    "outcome_score": 1.0,
                    "outcome_llm_weight": 0,
                },
            }
        ),
        encoding="utf-8",
    )

    summary = _summary_for_output(output_dir, run_config)

    assert summary["baseline_ready"] is True
    assert summary["writer_smoke_gate"] == {
        "required": True,
        "expected_rounds": 1,
        "writer_rounds": 1,
        "mandatory_passed": 1,
        "planning_state_isolated": 1,
        "events_complete": 1,
        "passed": True,
    }

    round_payload["writer_event_validation"] = {"events_complete": False}
    result_file.write_text(
        json.dumps(
            {
                "task_id": "001-file",
                "adapter_result": {"ok": True},
                "adapter_results": [
                    {"stdout": json.dumps(round_payload)}
                ],
                "oracle_result": {
                    "outcome_score": 1.0,
                    "outcome_llm_weight": 0,
                },
            }
        ),
        encoding="utf-8",
    )
    failed = _summary_for_output(output_dir, run_config)
    assert failed["baseline_ready"] is False
    assert failed["writer_smoke_gate"]["passed"] is False


def test_harnessbench_usage_proxy_redacts_credentials_from_payloads() -> None:
    harnessbench_src = Path(__file__).resolve().parents[3] / "HarnessBench" / "src"
    if not harnessbench_src.is_dir():
        return
    sys.path.insert(0, str(harnessbench_src))
    try:
        usage_proxy = importlib.import_module("harnessbench.usage_proxy")
    finally:
        sys.path.remove(str(harnessbench_src))
    headers = Message()
    headers["Authorization"] = "Bearer secret-value-123"
    headers["Content-Type"] = "application/json"

    redacted = usage_proxy._redact_sensitive_values(
        '{"tool_output":"OPENAI_API_KEY=secret-value-123"}',
        headers,
    )

    assert "secret-value-123" not in redacted
    assert "[REDACTED]" in redacted


def test_artifact_redactor_removes_env_secrets_without_logging_values(
    tmp_path: Path,
) -> None:
    secret = "artifact-secret-value"
    artifact = tmp_path / "session.json"
    artifact.write_text(
        json.dumps({"tool_output": f"OPENAI_API_KEY={secret}"}),
        encoding="utf-8",
    )

    secrets = sensitive_values({"OPENAI_API_KEY": secret, "NORMAL_VALUE": "leave-me"})
    scanned, changed = redact_tree(tmp_path, secrets)

    assert scanned == 1
    assert changed == 1
    assert secret not in artifact.read_text(encoding="utf-8")
    assert "[REDACTED]" in artifact.read_text(encoding="utf-8")
