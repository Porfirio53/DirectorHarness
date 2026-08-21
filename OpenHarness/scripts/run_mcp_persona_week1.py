#!/usr/bin/env python3
"""Run the complete MCP-Persona week-one local acceptance workflow."""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import os
import shutil
import sys
import time
import traceback
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping, Sequence
from urllib.parse import urlparse

from dotenv import load_dotenv

from openharness.api.openai_client import OpenAICompatibleClient
from openharness.config.settings import PermissionSettings
from openharness.engine.messages import ConversationMessage
from openharness.engine.query import MaxTurnsExceeded, QueryContext, run_query
from openharness.engine.stream_events import (
    AssistantTurnComplete,
    DirectorEventEmitted,
    ErrorEvent,
    StatusEvent,
    ToolExecutionCompleted,
    ToolExecutionStarted,
)
from openharness.mcp.client import McpClientManager
from openharness.mcp.types import McpStdioServerConfig
from openharness.permissions.checker import PermissionChecker
from openharness.permissions.modes import PermissionMode
from openharness.rehearsal.mcp_persona import (
    build_audit_report,
    write_audit_json,
    write_task_csv,
)
from openharness.rehearsal.mcp_persona_rehearsal import (
    VERIFIED52_PROTOCOL_ID,
    VERIFIED52_WRITER_DIRECTOR_ARM,
    VERIFIED_TASK_IDS,
    is_verified52_original_config,
    is_verified52_writer_config,
    verified52_experiment_arm,
)
from openharness.rehearsal.mcp_persona_runtime import (
    build_version_manifest,
    directory_diff_summary,
    directory_hash,
    evaluate_local_trial,
    execution_runtime_fingerprint,
    exact_copy,
    prepare_task_state,
    probe_official_evaluators,
    qualified_tool_name,
    released_tool_schemas,
    scores_to_dict,
    simulator_source,
    task_by_id,
)
from openharness.rehearsal.writer_handoff import (
    WriterEventRecorder,
    generate_writer_handoff,
    verify_writer_deployment,
)
from openharness.tools.base import ToolRegistry
from openharness.tools.mcp_tool import McpToolAdapter


REPRESENTATIVE_TASKS = (53, 70, 131)
REPRESENTATIVE_ROLES = {
    53: "single_server_short_dependency_chain",
    70: "single_server_long_state_mutation",
    131: "cross_server_public_release_probe",
}
LOCAL_STATE_SERVERS = {
    "instagram",
    "lark_mcp",
    "notion",
    "obsidian",
    "reddit",
    "slack",
    "universal_email",
    "wecome",
    "xiaohongshu",
}
RESULT_LABEL = "OpenHarness-compatible local result; not an official MCP-Persona score"
VALID_TRIAL_STATES = {
    "completed",
    "completed_with_tool_errors",
    "agent_task_failed",
}
AGENT_FAILURE_MARKERS = (
    "exceeded maximum turn limit",
    "exceeded trial wall-clock limit",
    "model returned an empty assistant message",
)
FATAL_ACCOUNT_FAILURE_MARKERS = (
    "arrearage",
    "authentication",
    "insufficient balance",
    "overdue-payment",
    "unauthorized",
)
INFRASTRUCTURE_FAILURE_MARKERS = (
    "api connection",
    "api timeout",
    "apiconnectionerror",
    "apitimeouterror",
    "arrearage",
    "authentication",
    "connection error",
    "connection failed",
    "connection refused",
    "expected tools were not registered",
    "http error",
    "insufficient balance",
    "internal server error",
    "mcp connection failed",
    "overdue-payment",
    "rate limit",
    "ratelimit",
    "service unavailable",
    "status code 4",
    "status code 5",
    "timed out",
    "timeout",
    "too many requests",
    "unauthorized",
)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--mcp-persona-root", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--language", choices=("en", "zh"), default="en")
    parser.add_argument("--mode", choices=("representative", "full"), default="full")
    parser.add_argument("--task-ids", type=int, nargs="*")
    parser.add_argument("--repeats", type=int, default=2)
    parser.add_argument("--model", default="qwen3.6-plus")
    parser.add_argument("--env-file", type=Path)
    parser.add_argument("--mcp-persona-python", type=Path)
    parser.add_argument("--tool-scope", choices=("task", "server"), default="task")
    parser.add_argument("--max-turns", type=int, default=30)
    parser.add_argument("--max-tokens", type=int, default=4096)
    parser.add_argument("--temperature", type=float, default=0.0)
    parser.add_argument("--seed", type=int, default=42)
    parser.add_argument("--api-timeout", type=float, default=180.0)
    parser.add_argument(
        "--trial-timeout",
        type=float,
        default=300.0,
        help="Hard wall-clock limit for one complete task trial",
    )
    parser.add_argument(
        "--enable-thinking",
        action="store_true",
        help="Keep Qwen thinking mode enabled (disabled by default for bounded runs)",
    )
    parser.add_argument(
        "--tool-output-inline-chars",
        type=int,
        default=200_000,
        help="Keep benchmark tool results available to the model instead of artifact-only previews",
    )
    parser.add_argument(
        "--chain-guidance",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="Expose the released tool order for compatibility testing (not an official blind score)",
    )
    parser.add_argument(
        "--experiment-stage",
        choices=(
            "baseline",
            "paired-smoke",
            "writer-full",
            "writer-director-full",
        ),
        default="baseline",
        help=(
            "Paired smoke permits one repeat over a subset; writer-full locks one "
            "Writer-enabled repeat over all Verified52 tasks. Neither is the formal "
            "two-repeat Original baseline."
        ),
    )
    parser.add_argument(
        "--openharness-mode",
        choices=("original", "writer_harness"),
        default="original",
        help="Run the actor directly or require the external Writer handoff first",
    )
    parser.add_argument(
        "--writer-workspace-root",
        type=Path,
        help="Workspace root containing the immutable writer_harness package",
    )
    parser.add_argument(
        "--writer-archive",
        type=Path,
        help="Team writer_director_0812.zip used for byte-level verification",
    )
    parser.add_argument(
        "--writer-model",
        help="Writer report model; defaults to --model",
    )
    parser.add_argument("--writer-max-tokens", type=int, default=4096)
    parser.add_argument(
        "--director-harness-enabled",
        action="store_true",
        help="Enable the team Director preflight before each tool execution",
    )
    parser.add_argument(
        "--director-mcp-catalog",
        type=Path,
        help="Team-approved MCP catalog used only when Director is enabled",
    )
    parser.add_argument("--max-tasks", type=int, default=0)
    parser.add_argument(
        "--resume",
        action="store_true",
        help="Keep completed task/trial rows in the output directory and continue missing trials",
    )
    parser.add_argument(
        "--summarize-only",
        action="store_true",
        help="Refresh baseline-summary.json from existing files without loading credentials or calling a model",
    )
    return parser.parse_args()


def _write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.tmp")
    temporary.write_text(
        json.dumps(value, ensure_ascii=False, indent=2, default=str) + "\n",
        encoding="utf-8",
    )
    temporary.replace(path)


def _write_results_jsonl(
    path: Path,
    grouped: Mapping[int, Sequence[Mapping[str, Any]]],
) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    rows = sorted(
        (row for values in grouped.values() for row in values),
        key=lambda row: (int(row["task_id"]), int(row["trial"])),
    )
    temporary = path.with_name(f".{path.name}.tmp")
    with temporary.open("w", encoding="utf-8") as stream:
        for row in rows:
            stream.write(json.dumps(row, ensure_ascii=False, default=str) + "\n")
    temporary.replace(path)


def _append_infrastructure_failure(path: Path, result: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    record = dict(result)
    record["recorded_at"] = datetime.now(timezone.utc).isoformat()
    with path.open("a", encoding="utf-8") as stream:
        stream.write(json.dumps(record, ensure_ascii=False, default=str) + "\n")


def _redact_secret(value: Any, secret: str) -> Any:
    if not secret:
        return value
    if isinstance(value, str):
        return value.replace(secret, "[REDACTED_API_KEY]")
    if isinstance(value, Mapping):
        return {
            str(key): _redact_secret(child, secret)
            for key, child in value.items()
        }
    if isinstance(value, list):
        return [_redact_secret(child, secret) for child in value]
    if isinstance(value, tuple):
        return tuple(_redact_secret(child, secret) for child in value)
    return value


def _fatal_account_failure(result: Mapping[str, Any]) -> bool:
    errors = "\n".join(str(value) for value in result.get("errors", [])).casefold()
    return any(marker in errors for marker in FATAL_ACCOUNT_FAILURE_MARKERS)


def _load_existing_results(path: Path) -> dict[int, list[dict[str, Any]]]:
    grouped: dict[int, list[dict[str, Any]]] = {}
    keys: set[tuple[int, int]] = set()
    if not path.is_file():
        return grouped
    for line_number, line in enumerate(
        path.read_text(encoding="utf-8").splitlines(),
        1,
    ):
        if not line.strip():
            continue
        value = json.loads(line)
        if (
            not isinstance(value, dict)
            or not isinstance(value.get("task_id"), int)
            or not isinstance(value.get("trial"), int)
            or int(value["trial"]) < 1
        ):
            raise ValueError(
                f"Invalid resumable result row at {path}:{line_number}"
            )
        key = (int(value["task_id"]), int(value["trial"]))
        if key in keys:
            raise ValueError(
                f"Duplicate resumable result for task {key[0]} trial {key[1]} in {path}"
            )
        keys.add(key)
        grouped.setdefault(key[0], []).append(value)
    for values in grouped.values():
        values.sort(key=lambda row: int(row["trial"]))
    return grouped


def _validate_result_slots(
    grouped: Mapping[int, Sequence[Mapping[str, Any]]],
    task_ids: Sequence[int],
    repeats: int,
) -> None:
    allowed_tasks = set(task_ids)
    for task_id, values in grouped.items():
        if task_id not in allowed_tasks:
            raise ValueError(
                f"results.jsonl contains task {task_id}, which is not in the locked run"
            )
        for value in values:
            trial = int(value["trial"])
            if trial > repeats:
                raise ValueError(
                    f"results.jsonl contains task {task_id} trial {trial}, "
                    f"but repeats={repeats}"
                )
            if _classify_trial(value) not in VALID_TRIAL_STATES:
                raise ValueError(
                    f"canonical results.jsonl contains retryable infrastructure failure "
                    f"for task {task_id} trial {trial}"
                )


def _classify_trial(result: Mapping[str, Any]) -> str:
    """Separate valid model outcomes from runner/API/environment failures."""

    if result.get("exception_traceback"):
        return "infrastructure_failed"
    state = result.get("state")
    if isinstance(state, Mapping) and state.get("exact_reset") is False:
        return "infrastructure_failed"
    errors = [
        str(value).strip()
        for value in result.get("errors", [])
        if str(value).strip()
    ]
    if errors:
        combined = "\n".join(errors).casefold()
        if all(
            any(marker in error.casefold() for marker in AGENT_FAILURE_MARKERS)
            for error in errors
        ):
            return "agent_task_failed"
        if any(marker in combined for marker in INFRASTRUCTURE_FAILURE_MARKERS):
            return "infrastructure_failed"
        # Unknown runner error text is not a fair model outcome, so make it resumable.
        return "infrastructure_failed"
    tool_calls = result.get("tool_calls")
    if isinstance(tool_calls, list) and any(
        isinstance(call, Mapping) and call.get("is_error") is True
        for call in tool_calls
    ):
        return "completed_with_tool_errors"
    return "completed"


def _run_status(result: Mapping[str, Any]) -> dict[str, Any]:
    classification_detail = _classify_trial(result)
    if classification_detail == "infrastructure_failed":
        classification = "infrastructure_failed"
    elif classification_detail == "completed":
        classification = "completed"
    else:
        classification = "agent_task_failed"
    errors = "\n".join(
        str(value) for value in result.get("errors", [])
    ).casefold()
    detail = (
        "agent_trial_timeout"
        if classification == "agent_task_failed"
        and "exceeded trial wall-clock limit" in errors
        else classification_detail
    )
    return {
        "classification": classification,
        "detail": detail,
        "baseline_valid": classification_detail in VALID_TRIAL_STATES,
        "retry_eligible": classification_detail == "infrastructure_failed",
    }


def _runner_failure_result(
    *,
    task: Mapping[str, Any],
    trial: int,
    exc: Exception,
) -> dict[str, Any]:
    return {
        "schema_version": 1,
        "result_scope": RESULT_LABEL,
        "task_id": int(task["id"]),
        "trial": trial,
        "query": {
            "instruction": task.get("instruction"),
            "context": task.get("context"),
        },
        "query_type": task.get("query_type"),
        "servers": sorted(_task_servers(task)),
        "expected_tools": task.get("chains", []),
        "mcp_statuses": [],
        "registered_tools": [],
        "tool_calls": [],
        "raw_server_trace": [],
        "final_answer": "",
        "messages": [],
        "events": [],
        "errors": [f"{type(exc).__name__}: {str(exc) or 'no exception message'}"],
        "exception_traceback": traceback.format_exc(),
        "usage": {"input_tokens": 0, "output_tokens": 0},
        "state": None,
        "local_scores": {},
        "duration_seconds": None,
        "trial_state": "infrastructure_failed",
        "run_status": {
            "classification": "infrastructure_failed",
            "detail": "infrastructure_failed",
            "baseline_valid": False,
            "retry_eligible": True,
        },
    }


def _prepare_output_dir(
    output_dir: Path,
    requested_config: dict[str, Any],
    *,
    resume: bool,
) -> dict[str, Any]:
    config_path = output_dir / "run-config.json"
    if config_path.is_file():
        existing = json.loads(config_path.read_text(encoding="utf-8"))
        if not isinstance(existing, dict):
            raise ValueError(f"Invalid run config: {config_path}")
        provenance_only_fields = {
            "created_at",
            "dataset_id",
            "env_file",
            "execution_core_source_sha256",
            "experiment_arm",
            "formal_verified52",
            "legacy_runtime_fingerprint_sha256",
            "mcp_persona_git",
            "mcp_persona_root",
            "openharness_git",
            "openharness_root",
            "protocol_id",
            "result_label",
            "runtime_fingerprint_schema",
            "runtime_fingerprint_scope",
            "runtime_fingerprint_sha256",
            "schema_version",
            "source_lock_enforced",
            "source_provenance_history",
            "source_sha256",
            "writer_deployment",
            "writer_full_selection",
            "writer_full_verified52",
        }
        comparable_keys = sorted(
            (set(existing) | set(requested_config)) - provenance_only_fields
        )
        mismatches = [
            key
            for key in comparable_keys
            if existing.get(key) != requested_config.get(key)
        ]
        if mismatches:
            raise ValueError(
                "output directory belongs to a different experiment protocol; "
                "mismatched fields: "
                + ", ".join(mismatches)
            )
        if not resume:
            raise ValueError(
                f"output directory already contains run-config.json; pass --resume: {output_dir}"
            )
        return existing
    if output_dir.exists() and any(output_dir.iterdir()):
        raise ValueError(
            f"refusing to mix a baseline with an existing untracked output directory: {output_dir}"
        )
    output_dir.mkdir(parents=True, exist_ok=True)
    requested_config["created_at"] = datetime.now(timezone.utc).isoformat()
    _write_json(config_path, requested_config)
    return requested_config


def _build_run_config(
    *,
    args: argparse.Namespace,
    task_ids: Sequence[int],
    root: Path,
    openharness_root: Path,
    env_file: Path | None,
    api_base: str,
    version_manifest: Mapping[str, Any],
) -> dict[str, Any]:
    endpoint = urlparse(api_base)
    openharness = version_manifest.get("openharness", {})
    mcp_persona = version_manifest.get("mcp_persona", {})
    formal_verified52 = (
        tuple(task_ids) == VERIFIED_TASK_IDS
        and args.repeats == 2
        and args.language == "en"
        and args.tool_scope == "server"
        and args.chain_guidance is False
        and getattr(args, "experiment_stage", "baseline") == "baseline"
        and getattr(args, "openharness_mode", "original") == "original"
    )
    writer_full_selection = (
        tuple(task_ids) == VERIFIED_TASK_IDS
        and args.repeats in {1, 2}
        and args.language == "en"
        and args.tool_scope == "server"
        and args.chain_guidance is False
        and getattr(args, "experiment_stage", "baseline") == "writer-full"
        and getattr(args, "openharness_mode", "original") == "writer_harness"
        and (getattr(args, "writer_model", None) or args.model) == args.model
    )
    writer_director_full_selection = (
        tuple(task_ids) == VERIFIED_TASK_IDS
        and args.repeats == 2
        and args.language == "en"
        and args.tool_scope == "server"
        and args.chain_guidance is False
        and getattr(args, "experiment_stage", "baseline")
        == "writer-director-full"
        and getattr(args, "openharness_mode", "original") == "writer_harness"
        and getattr(args, "director_harness_enabled", False) is True
        and (getattr(args, "writer_model", None) or args.model) == args.model
    )
    writer_full_verified52 = writer_full_selection and args.repeats == 2
    runtime_fingerprint = execution_runtime_fingerprint(version_manifest)
    experiment_arm = (
        VERIFIED52_WRITER_DIRECTOR_ARM
        if getattr(args, "director_harness_enabled", False)
        else "writer_harness"
        if getattr(args, "openharness_mode", "original") == "writer_harness"
        else "original"
    )
    return {
        "schema_version": 1,
        "result_label": RESULT_LABEL,
        "dataset_id": (
            "mcp-persona-verified52"
            if formal_verified52
            else (
                "mcp-persona-verified52-writer-director-full"
                if writer_director_full_selection
                else (
                    "mcp-persona-verified52-writer-full"
                    if writer_full_selection
                    else "mcp-persona-development-run"
                )
            )
        ),
        "formal_verified52": formal_verified52,
        **(
            {"writer_full_verified52": True}
            if writer_full_verified52
            else {}
        ),
        **(
            {"writer_full_selection": True}
            if writer_full_selection
            else {}
        ),
        **(
            {"writer_director_full_verified52": True}
            if writer_director_full_selection
            else {}
        ),
        "protocol_id": (
            VERIFIED52_PROTOCOL_ID
            if formal_verified52
            or writer_full_verified52
            or writer_director_full_selection
            else None
        ),
        "experiment_arm": experiment_arm,
        "source_lock_enforced": False,
        "tasks": list(task_ids),
        "repeats": args.repeats,
        "language": args.language,
        "mode": args.mode,
        "model": args.model,
        "tool_scope": args.tool_scope,
        "max_turns": args.max_turns,
        "max_tokens": args.max_tokens,
        "temperature": args.temperature,
        "seed": args.seed,
        "api_timeout_seconds": args.api_timeout,
        "trial_timeout_seconds": args.trial_timeout,
        "enable_thinking": args.enable_thinking,
        "tool_output_inline_chars": args.tool_output_inline_chars,
        "chain_guidance": args.chain_guidance,
        "experiment_stage": getattr(args, "experiment_stage", "baseline"),
        "openharness_mode": getattr(args, "openharness_mode", "original"),
        "writer_model": (
            getattr(args, "writer_model", None) or args.model
            if getattr(args, "openharness_mode", "original") == "writer_harness"
            else None
        ),
        "writer_max_tokens": (
            getattr(args, "writer_max_tokens", 4096)
            if getattr(args, "openharness_mode", "original") == "writer_harness"
            else None
        ),
        "director_harness_enabled": bool(
            getattr(args, "director_harness_enabled", False)
        ),
        "director_mcp_catalog": (
            str(args.director_mcp_catalog.resolve())
            if getattr(args, "director_harness_enabled", False)
            and args.director_mcp_catalog is not None
            else None
        ),
        "mcp_persona_root": str(root),
        "openharness_root": str(openharness_root),
        "env_file": str(env_file) if env_file else None,
        "model_endpoint": {
            "scheme": endpoint.scheme,
            "api_base_host": endpoint.hostname,
            "api_base_path": endpoint.path,
        },
        "openharness_git": {
            "root": openharness.get("root"),
            "commit": openharness.get("commit"),
        }
        if isinstance(openharness, Mapping)
        else None,
        "mcp_persona_git": {
            "repository": mcp_persona.get("repository"),
            "root": mcp_persona.get("root"),
            "commit": mcp_persona.get("commit"),
        }
        if isinstance(mcp_persona, Mapping)
        else None,
        "source_sha256": version_manifest.get("file_sha256"),
        "runtime_fingerprint_schema": runtime_fingerprint["schema_version"],
        "runtime_fingerprint_scope": runtime_fingerprint["scope"],
        "runtime_fingerprint_sha256": runtime_fingerprint["sha256"],
        "execution_core_source_sha256": runtime_fingerprint[
            "execution_core_source_sha256"
        ],
        "writer_deployment": (
            version_manifest.get("writer_harness")
            if getattr(args, "openharness_mode", "original")
            == "writer_harness"
            else None
        ),
    }


def _numeric_mean(values: Sequence[float]) -> float | None:
    return round(sum(values) / len(values), 6) if values else None


def _summary_for_results(
    grouped: Mapping[int, Sequence[Mapping[str, Any]]],
    run_config: Mapping[str, Any],
) -> dict[str, Any]:
    task_ids = [int(value) for value in run_config.get("tasks", [])]
    repeats = int(run_config.get("repeats", 0))
    expected_slots = [
        (task_id, trial)
        for task_id in task_ids
        for trial in range(1, repeats + 1)
    ]
    by_key = {
        (int(row["task_id"]), int(row["trial"])): row
        for values in grouped.values()
        for row in values
    }
    state_counts: dict[str, int] = {}
    missing: list[dict[str, int]] = []
    infrastructure_failures: list[dict[str, Any]] = []
    reset_failures: list[dict[str, int]] = []
    score_values: dict[str, list[float]] = {}
    total_tool_calls = 0
    total_input_tokens = 0
    total_output_tokens = 0
    durations: list[float] = []
    valid_result_count = 0
    exact_reset_count = 0
    writer_required = run_config.get("openharness_mode") == "writer_harness"
    writer_mandatory_count = 0
    writer_state_isolated_count = 0
    writer_events_complete_count = 0
    writer_input_tokens = 0
    writer_output_tokens = 0
    director_required = run_config.get("director_harness_enabled") is True
    director_enabled_count = 0
    director_checked_count = 0
    director_ordered_count = 0
    director_event_count = 0

    for task_id, trial in expected_slots:
        row = by_key.get((task_id, trial))
        if row is None:
            state_counts["pending"] = state_counts.get("pending", 0) + 1
            missing.append({"task_id": task_id, "trial": trial})
            continue
        trial_state = _classify_trial(row)
        state_counts[trial_state] = state_counts.get(trial_state, 0) + 1
        if trial_state in VALID_TRIAL_STATES:
            valid_result_count += 1
        else:
            infrastructure_failures.append(
                {
                    "task_id": task_id,
                    "trial": trial,
                    "errors": list(row.get("errors", [])),
                }
            )
        state = row.get("state")
        if not isinstance(state, Mapping) or state.get("exact_reset") is not True:
            reset_failures.append({"task_id": task_id, "trial": trial})
        else:
            exact_reset_count += 1
        scores = row.get("local_scores")
        if isinstance(scores, Mapping):
            for name, value in scores.items():
                if isinstance(value, (int, float)) and not isinstance(value, bool):
                    score_values.setdefault(str(name), []).append(float(value))
        tool_calls = row.get("tool_calls")
        if isinstance(tool_calls, list):
            total_tool_calls += len(tool_calls)
        usage = row.get("usage")
        if isinstance(usage, Mapping):
            if isinstance(usage.get("input_tokens"), int):
                total_input_tokens += int(usage["input_tokens"])
            if isinstance(usage.get("output_tokens"), int):
                total_output_tokens += int(usage["output_tokens"])
        duration = row.get("duration_seconds")
        if isinstance(duration, (int, float)):
            durations.append(float(duration))
        writer = row.get("writer")
        if writer_required and isinstance(writer, Mapping):
            writer_mandatory_count += (
                writer.get("mandatory_passed") is True
            )
            writer_state_isolated_count += (
                writer.get("planning_state_unchanged") is True
            )
            validation = writer.get("event_validation")
            writer_events_complete_count += (
                isinstance(validation, Mapping)
                and validation.get("events_complete") is True
            )
            writer_usage = writer.get("usage")
            if isinstance(writer_usage, Mapping):
                writer_input_tokens += int(
                    writer_usage.get("input_tokens", 0) or 0
                )
                writer_output_tokens += int(
                    writer_usage.get("output_tokens", 0) or 0
                )
        director = row.get("director")
        if director_required and isinstance(director, Mapping):
            director_enabled_count += director.get("enabled") is True
            validation = director.get("event_validation")
            if isinstance(validation, Mapping):
                director_checked_count += (
                    validation.get("all_tool_calls_checked") is True
                )
                director_ordered_count += (
                    validation.get("director_before_tool_completion") is True
                )
                director_event_count += int(
                    validation.get("event_count", 0) or 0
                )

    result_count = sum(key in by_key for key in expected_slots)
    expected_results = len(expected_slots)
    run_complete = (
        expected_results > 0
        and result_count == expected_results
        and valid_result_count == expected_results
        and not reset_failures
    )
    formal_verified52 = is_verified52_original_config(run_config)
    writer_gate_passed = (
        writer_required
        and run_complete
        and writer_mandatory_count == expected_results
        and writer_state_isolated_count == expected_results
        and writer_events_complete_count == expected_results
    )
    director_gate_passed = (
        director_required
        and run_complete
        and director_enabled_count == expected_results
        and director_checked_count == expected_results
        and director_ordered_count == expected_results
    )
    analysis_ready = run_complete and (
        not writer_required or writer_gate_passed
    ) and (not director_required or director_gate_passed)
    verified52_arm = verified52_experiment_arm(run_config)
    verified52_ready = analysis_ready and verified52_arm is not None
    baseline_ready = verified52_ready
    locked_config = {
        key: value for key, value in run_config.items() if key != "created_at"
    }
    config_sha256 = hashlib.sha256(
        json.dumps(
            locked_config,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()
    return {
        "schema_version": 1,
        "result_label": RESULT_LABEL,
        "dataset_id": run_config.get("dataset_id"),
        "protocol_id": (
            VERIFIED52_PROTOCOL_ID if verified52_arm is not None else None
        ),
        "experiment_arm": (
            verified52_arm
            or run_config.get("experiment_arm")
            or run_config.get("openharness_mode", "original")
        ),
        "formal_verified52": formal_verified52,
        "writer_full_verified52": is_verified52_writer_config(run_config),
        "writer_director_full_verified52": (
            run_config.get("writer_director_full_verified52") is True
        ),
        "model": run_config.get("model"),
        "config_sha256": config_sha256,
        "chain_guidance_enabled": run_config.get("chain_guidance"),
        "task_count": len(task_ids),
        "repeats": repeats,
        "expected_results": expected_results,
        "result_count": result_count,
        "valid_result_count": valid_result_count,
        "state_counts": dict(sorted(state_counts.items())),
        "missing_results": missing,
        "infrastructure_failures": infrastructure_failures,
        "exact_reset_count": exact_reset_count,
        "reset_failures": reset_failures,
        "local_score_means": {
            name: _numeric_mean(values)
            for name, values in sorted(score_values.items())
        },
        "total_tool_calls": total_tool_calls,
        "usage": {
            "input_tokens": total_input_tokens,
            "output_tokens": total_output_tokens,
        },
        "writer_smoke_gate": {
            "required": writer_required,
            "expected_trials": expected_results if writer_required else 0,
            "mandatory_passed": writer_mandatory_count,
            "planning_state_isolated": writer_state_isolated_count,
            "events_complete": writer_events_complete_count,
            "passed": writer_gate_passed,
            "usage": {
                "input_tokens": writer_input_tokens,
                "output_tokens": writer_output_tokens,
            },
        },
        "director_smoke_gate": {
            "required": director_required,
            "expected_trials": expected_results if director_required else 0,
            "enabled_trials": director_enabled_count,
            "all_tool_calls_checked": director_checked_count,
            "preflight_order_passed": director_ordered_count,
            "event_count": director_event_count,
            "passed": director_gate_passed,
        },
        "mean_duration_seconds": _numeric_mean(durations),
        "run_complete": run_complete,
        "analysis_ready": analysis_ready,
        "verified52_ready": verified52_ready,
        "formal_original_baseline": (
            verified52_ready and verified52_arm == "original"
        ),
        "baseline_ready": baseline_ready,
        "updated_at": datetime.now(timezone.utc).isoformat(),
    }


def _write_incremental_summary(
    output_dir: Path,
    grouped: Mapping[int, Sequence[Mapping[str, Any]]],
    run_config: Mapping[str, Any],
) -> dict[str, Any]:
    summary = _summary_for_results(grouped, run_config)
    _write_json(output_dir / "baseline-summary.json", summary)
    return summary


def summarize_existing_output(output_dir: Path) -> dict[str, Any]:
    """Refresh a completed run summary without initializing MCP or model clients."""

    output_dir = output_dir.resolve()
    config_path = output_dir / "run-config.json"
    results_path = output_dir / "results.jsonl"
    if not config_path.is_file() or not results_path.is_file():
        raise ValueError(
            f"offline summary requires run-config.json and results.jsonl: {output_dir}"
        )
    run_config = json.loads(config_path.read_text(encoding="utf-8"))
    if not isinstance(run_config, dict):
        raise ValueError(f"invalid run config: {config_path}")
    grouped = _load_existing_results(results_path)
    task_ids = [int(value) for value in run_config.get("tasks", [])]
    repeats = int(run_config.get("repeats", 0))
    _validate_result_slots(grouped, task_ids, repeats)
    return _write_incremental_summary(output_dir, grouped, run_config)


def _discover_env_file(args: argparse.Namespace, openharness_root: Path) -> Path | None:
    candidates = [
        args.env_file,
        openharness_root.parent / ".env",
    ]
    return next((value.resolve() for value in candidates if value and value.is_file()), None)


def _task_servers(task: Mapping[str, Any]) -> set[str]:
    return {str(name).split(":", 1)[0] for name in task.get("chains", []) if ":" in str(name)}


def _runtime_candidate_ids(report: Mapping[str, Any], root: Path, language: str) -> list[int]:
    candidates: list[int] = []
    for audit in report.get("tasks", []):
        if not isinstance(audit, Mapping) or not audit.get("coverage_candidate"):
            continue
        task_id = int(audit["task_id"])
        task = task_by_id(root, task_id, language)
        servers = _task_servers(task)
        if not servers or not servers.issubset(LOCAL_STATE_SERVERS):
            continue
        schemas_ok = True
        for qualified in task.get("chains", []):
            server, name = str(qualified).split(":", 1)
            if name not in released_tool_schemas(root, server):
                schemas_ok = False
                break
            try:
                simulator_source(root, server, name)
            except FileNotFoundError:
                schemas_ok = False
                break
        if schemas_ok:
            candidates.append(task_id)
    return candidates


def _task_prompt(task: Mapping[str, Any], *, chain_guidance: bool) -> str:
    context = task.get("context") if isinstance(task.get("context"), Mapping) else {}
    necessary = context.get("necessary_context", {}) if isinstance(context, Mapping) else {}
    unnecessary = context.get("unnecessary_context", []) if isinstance(context, Mapping) else []
    guidance = ""
    if chain_guidance:
        guidance = (
            "\n\nCompatibility-probe execution order (released annotation; not an answer):\n"
            f"{json.dumps(task.get('chains', []), ensure_ascii=False, indent=2)}\n"
            "Execute every listed step once in exactly this order. Repeated tool names are separate "
            "required steps. Derive every parameter from the user context or prior tool results; "
            "do not invent missing values."
        )
    return (
        f"Task instruction:\n{task.get('instruction', '')}\n\n"
        "Available personal context (it may contain both relevant and irrelevant facts):\n"
        f"{json.dumps({'necessary_context': necessary, 'other_context': unnecessary}, ensure_ascii=False, indent=2)}"
        f"{guidance}"
    )


def _system_prompt() -> str:
    return (
        "You are executing an MCP-Persona task inside an isolated OpenHarness sandbox. "
        "Use the available MCP tools to complete every requested action. Do not invent object IDs, "
        "user IDs, document IDs, channel IDs, feed IDs, tokens, or tool results. When a later call "
        "depends on an earlier result, copy the returned identifier exactly. Continue calling tools "
        "until the task is complete, then summarize what was actually verified. Follow the action order "
        "in the user's instruction exactly, even when later actions appear independent. If a tool fails "
        "or the released context lacks required data, report the blocker precisely and do not claim "
        "success. Never guess alternate IDs or repeatedly retry the same call after a required ID is "
        "reported missing."
    )


def _load_server_trace(state_dir: Path) -> list[dict[str, Any]]:
    values: list[dict[str, Any]] = []
    for path in state_dir.glob("_openharness-*-calls.jsonl"):
        for line in path.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            value = json.loads(line)
            if isinstance(value, dict):
                values.append(value)
    return sorted(values, key=lambda value: int(value.get("timestamp_ns", 0)))


def _merge_raw_outputs(
    event_calls: list[dict[str, Any]], server_calls: Sequence[Mapping[str, Any]]
) -> None:
    unused = list(range(len(server_calls)))
    for call in event_calls:
        qualified = qualified_tool_name(str(call.get("tool_name", "")))
        server, tool = qualified.split(":", 1) if ":" in qualified else ("", qualified)
        for index in list(unused):
            candidate = server_calls[index]
            if candidate.get("server") == server and candidate.get("tool_name") == tool:
                call["raw_output"] = candidate.get("output")
                call["simulator_input"] = candidate.get("simulator_input")
                call["simulator_error"] = bool(candidate.get("is_error"))
                unused.remove(index)
                break


def _pre_director_input_rejection(call: Mapping[str, Any]) -> bool:
    """Return whether OpenHarness rejected a call before Director preflight."""

    if call.get("is_error") is not True:
        return False
    tool_name = str(call.get("tool_name") or "")
    output = str(call.get("output") or "").lstrip()
    return bool(tool_name) and output.startswith(f"Invalid input for {tool_name}:")


def _director_event_validation(
    *,
    enabled: bool,
    tool_calls: Sequence[Mapping[str, Any]],
    trajectory_events: Sequence[Mapping[str, Any]],
) -> dict[str, Any] | None:
    if not enabled:
        return None
    director_events = [
        event
        for event in trajectory_events
        if event.get("type") == "director_event"
    ]
    rejected_calls = [
        call for call in tool_calls if _pre_director_input_rejection(call)
    ]
    eligible_tool_call_count = len(tool_calls) - len(rejected_calls)
    tool_use_ids = {
        str(event.get("tool_use_id") or "")
        for event in director_events
        if str(event.get("tool_use_id") or "")
    }
    director_seen = 0
    completed_seen = 0
    director_before_completion = True
    for event in trajectory_events:
        event_type = event.get("type")
        if event_type == "director_event":
            director_seen += 1
        elif event_type == "tool_completed":
            if _pre_director_input_rejection(event):
                continue
            completed_seen += 1
            if director_seen < completed_seen:
                director_before_completion = False
    return {
        "enabled": True,
        "event_count": len(director_events),
        "checked_tool_use_count": len(tool_use_ids),
        "tool_call_count": len(tool_calls),
        "all_tool_calls_checked": len(tool_use_ids) == eligible_tool_call_count,
        "director_before_tool_completion": director_before_completion,
        "event_types": sorted(
            {
                str(event.get("event") or "")
                for event in director_events
                if event.get("event")
            }
        ),
        "statuses": sorted(
            {
                str(event.get("status") or "")
                for event in director_events
                if event.get("status")
            }
        ),
    }


async def _run_trial(
    *,
    task: Mapping[str, Any],
    trial: int,
    args: argparse.Namespace,
    root: Path,
    openharness_root: Path,
    api_client: OpenAICompatibleClient,
    task_dir: Path,
) -> dict[str, Any]:
    task_id = int(task["id"])
    trial_dir = task_dir / f"trial-{trial}"
    initial_dir = trial_dir / "initial_state"
    live_dir = trial_dir / "live_state"
    final_dir = trial_dir / "final_state"
    initial_dir.mkdir(parents=True, exist_ok=True)
    prepare_task_state(task, initial_dir)
    exact_copy(initial_dir, live_dir)
    initial_hash = directory_hash(initial_dir)

    server_script = openharness_root / "scripts" / "mcp_persona_stdio_server.py"
    configs: dict[str, object] = {}
    for server in sorted(_task_servers(task)):
        configs[server] = McpStdioServerConfig(
            command=sys.executable,
            args=[
                str(server_script),
                "--mcp-persona-root",
                str(root),
                "--task-id",
                str(task_id),
                "--language",
                args.language,
                "--server",
                server,
                "--state-dir",
                str(live_dir),
                "--tool-scope",
                args.tool_scope,
            ],
            cwd=str(openharness_root),
        )

    manager = McpClientManager(configs)
    director = None
    director_log_file: Path | None = None
    if args.director_harness_enabled:
        from director_harness import DirectorHarness
        from director_harness.catalog import McpCatalog
        from director_harness.events import DirectorEventLog

        director_log_file = trial_dir / "director-events.jsonl"
        director = DirectorHarness(
            catalog=McpCatalog.from_file(args.director_mcp_catalog),
            event_log=DirectorEventLog(director_log_file),
        )
    tool_calls: list[dict[str, Any]] = []
    messages = [
        ConversationMessage.from_user_text(
            _task_prompt(task, chain_guidance=args.chain_guidance)
        )
    ]
    trajectory_events: list[dict[str, Any]] = []
    errors: list[str] = []
    exception_traceback: str | None = None
    final_answer = ""
    usage = {"input_tokens": 0, "output_tokens": 0}
    statuses: list[dict[str, Any]] = []
    discovered: list[str] = []
    writer_handoff = None
    writer_recorder: WriterEventRecorder | None = None
    writer_state_before: str | None = None
    writer_state_after: str | None = None
    started = time.time()
    try:
        await manager.connect_all()
        statuses = [asdict(value) for value in manager.list_statuses()]
        failed = [value for value in statuses if value["state"] != "connected"]
        if failed:
            errors.append(f"MCP connection failed: {failed}")
        registry = ToolRegistry()
        for tool_info in manager.list_tools():
            registry.register(McpToolAdapter(manager, tool_info))
        discovered = [tool.name for tool in registry.list_tools()]
        expected_adapters = {
            "mcp__" + str(name).replace(":", "__", 1) for name in task.get("chains", [])
        }
        missing_registered = sorted(expected_adapters - set(discovered))
        if missing_registered:
            errors.append(f"Expected tools were not registered: {missing_registered}")
        system_prompt = _system_prompt()
        if args.openharness_mode == "writer_harness" and not errors:
            writer_state_before = directory_hash(live_dir)
            # The protocol annotation incorrectly models this async generator as
            # a coroutine returning an iterator; OpenAICompatibleClient is the
            # runtime implementation used by both Writer planning and run_query.
            writer_handoff = await generate_writer_handoff(
                api_client=api_client,  # type: ignore[arg-type]
                model=args.writer_model or args.model,
                actor_model=args.model,
                workspace_root=args.writer_workspace_root,
                query=_task_prompt(task, chain_guidance=False),
                live_tool_schemas=registry.to_api_schema(),
                max_tokens=args.writer_max_tokens,
            )
            writer_state_after = directory_hash(live_dir)
            if writer_state_after != writer_state_before:
                raise RuntimeError(
                    "Writer planning changed the MCP state before actor execution"
                )
            if not writer_handoff.execution_decision.get("should_execute"):
                raise RuntimeError(
                    "Writer did not produce a content-complete final_scripts "
                    "execution input"
                )
            writer_recorder = WriterEventRecorder(
                writer_handoff,
                events=trajectory_events,
            )
            system_prompt += writer_handoff.prompt_appendix()
        context = QueryContext(
            api_client=api_client,  # type: ignore[arg-type]
            tool_registry=registry,
            permission_checker=PermissionChecker(PermissionSettings(mode=PermissionMode.FULL_AUTO)),
            cwd=trial_dir,
            model=args.model,
            system_prompt=system_prompt,
            max_tokens=args.max_tokens,
            max_turns=args.max_turns,
            tool_metadata={
                "session_id": f"mcp-persona-{task_id}-trial-{trial}",
                "mcp_manager": manager,
                "writer_execution_contract": (
                    writer_handoff.execution_contract
                    if writer_handoff is not None
                    else None
                ),
            },
            director=director,
        )
        if not errors:
            trial_timeout = asyncio.timeout(args.trial_timeout)
            try:
                async with trial_timeout:
                    async for event, event_usage in run_query(context, messages):
                        if event_usage is not None:
                            usage["input_tokens"] += event_usage.input_tokens
                            usage["output_tokens"] += event_usage.output_tokens
                        if isinstance(event, ToolExecutionStarted):
                            if writer_recorder is not None:
                                writer_recorder.action_proposed(
                                    event.tool_name,
                                    event.tool_input,
                                )
                            call = {
                                "tool_name": event.tool_name,
                                "input": event.tool_input,
                                "output": "",
                                "is_error": False,
                            }
                            tool_calls.append(call)
                            trajectory_events.append({"type": "tool_started", **call})
                        elif isinstance(event, DirectorEventEmitted):
                            trajectory_events.append(
                                {
                                    "type": "director_event",
                                    "event": event.event,
                                    "tool_name": event.tool_name,
                                    "requested_tool_name": event.requested_tool_name,
                                    "status": event.status,
                                    "detail": event.detail,
                                    "session_id": event.session_id,
                                    "tool_use_id": event.tool_use_id,
                                    "timestamp": event.timestamp,
                                    "data": event.data or {},
                                }
                            )
                        elif isinstance(event, ToolExecutionCompleted):
                            if writer_recorder is not None:
                                writer_recorder.action_completed(
                                    event.tool_name,
                                    is_error=event.is_error,
                                )
                            completed_call: dict[str, Any] | None = None
                            for value in tool_calls:
                                if value["tool_name"] == event.tool_name and not value["output"]:
                                    completed_call = value
                                    break
                            if completed_call is not None:
                                completed_call["output"] = event.output
                                completed_call["is_error"] = event.is_error
                            trajectory_events.append(
                                {
                                    "type": "tool_completed",
                                    "tool_name": event.tool_name,
                                    "output": event.output,
                                    "is_error": event.is_error,
                                }
                            )
                        elif isinstance(event, AssistantTurnComplete):
                            trajectory_events.append(
                                {
                                    "type": "assistant_turn",
                                    "message": event.message.model_dump(mode="json"),
                                }
                            )
                            if not event.message.tool_uses:
                                final_answer = event.message.text
                        elif isinstance(event, ErrorEvent):
                            errors.append(event.message)
                            trajectory_events.append({"type": "error", "message": event.message})
                        elif isinstance(event, StatusEvent):
                            trajectory_events.append({"type": "status", "message": event.message})
            except MaxTurnsExceeded as exc:
                errors.append(str(exc))
            except TimeoutError:
                if not trial_timeout.expired():
                    raise
                errors.append(
                    "Exceeded trial wall-clock limit "
                    f"({args.trial_timeout:g} seconds)"
                )
                trajectory_events.append(
                    {
                        "type": "agent_terminated",
                        "reason": "trial_timeout",
                        "limit_seconds": args.trial_timeout,
                    }
                )
    except Exception as exc:
        statuses = [asdict(value) for value in manager.list_statuses()]
        detail = str(exc) or "no exception message"
        errors.append(f"{type(exc).__name__}: {detail}")
        exception_traceback = traceback.format_exc()
    finally:
        await manager.close()

    server_trace = _load_server_trace(live_dir)
    _merge_raw_outputs(tool_calls, server_trace)
    mutated_hash = directory_hash(live_dir)
    state_changed = mutated_hash != initial_hash
    if final_dir.exists():
        shutil.rmtree(final_dir)
    shutil.copytree(live_dir, final_dir)
    state_diff = directory_diff_summary(initial_dir, final_dir)
    exact_copy(initial_dir, live_dir)
    reset_hash = directory_hash(live_dir)
    exact_reset = reset_hash == initial_hash
    scores = evaluate_local_trial(
        task,
        tool_calls,
        final_answer,
        state_changed=state_changed,
        exact_reset=exact_reset,
        state_evidence=state_diff,
    )
    writer_validation = (
        writer_recorder.validation()
        if writer_recorder is not None
        else None
    )
    director_validation = _director_event_validation(
        enabled=args.director_harness_enabled,
        tool_calls=tool_calls,
        trajectory_events=trajectory_events,
    )
    result = {
        "schema_version": 1,
        "result_scope": RESULT_LABEL,
        "task_id": task_id,
        "trial": trial,
        "query": {"instruction": task.get("instruction"), "context": task.get("context")},
        "query_type": task.get("query_type"),
        "servers": sorted(_task_servers(task)),
        "expected_tools": task.get("chains", []),
        "mcp_statuses": statuses,
        "registered_tools": discovered,
        "tool_calls": tool_calls,
        "raw_server_trace": server_trace,
        "final_answer": final_answer,
        "messages": [message.model_dump(mode="json") for message in messages],
        "events": trajectory_events,
        "errors": errors,
        "exception_traceback": exception_traceback,
        "usage": usage,
        "writer": {
            "enabled": args.openharness_mode == "writer_harness",
            "mandatory_passed": writer_handoff is not None,
            "policy": "team_writer_v1_scored_final_scripts_observe_only",
            "planning_state_before": writer_state_before,
            "planning_state_after": writer_state_after,
            "planning_state_unchanged": (
                writer_state_before is not None
                and writer_state_before == writer_state_after
            ),
            "event_validation": writer_validation,
            "usage": (
                dict(writer_handoff.usage)
                if writer_handoff is not None
                else {"input_tokens": 0, "output_tokens": 0}
            ),
            "handoff": (
                writer_handoff.to_dict()
                if writer_handoff is not None
                else None
            ),
        },
        "director": {
            "enabled": args.director_harness_enabled,
            "policy": "team_director_pre_tool_execution_v1",
            "event_validation": director_validation,
            "event_count": (
                int(director_validation.get("event_count", 0))
                if isinstance(director_validation, Mapping)
                else 0
            ),
            "log_file": (
                str(director_log_file)
                if director_log_file is not None
                else None
            ),
        },
        "state": {
            "initial_hash": initial_hash,
            "mutated_hash": mutated_hash,
            "reset_hash": reset_hash,
            "state_changed": state_changed,
            "exact_reset": exact_reset,
            "diff_summary": state_diff,
            "initial_dir": str(initial_dir),
            "final_dir": str(final_dir),
        },
        "local_scores": scores_to_dict(scores),
        "duration_seconds": round(time.time() - started, 3),
    }
    result["trial_state"] = _classify_trial(result)
    result["run_status"] = _run_status(result)
    return result


async def async_main(args: argparse.Namespace) -> int:
    output_dir = args.output_dir.resolve()
    if args.summarize_only:
        summary = summarize_existing_output(output_dir)
        print(json.dumps(summary, ensure_ascii=False, indent=2))
        return 0 if summary["analysis_ready"] else 2
    if args.repeats < 1:
        raise SystemExit("--repeats must be at least 1")
    if args.repeats < 2 and args.experiment_stage not in {
        "paired-smoke",
        "writer-full",
        "writer-director-full",
    }:
        raise SystemExit("--repeats must be at least 2 for the week-one reset gate")
    if (
        args.experiment_stage
        in {"paired-smoke", "writer-full", "writer-director-full"}
        and args.chain_guidance
    ):
        raise SystemExit(f"{args.experiment_stage} requires --no-chain-guidance")
    openharness_root = Path(__file__).resolve().parents[1]
    root = args.mcp_persona_root.resolve()
    workspace_root = (
        args.writer_workspace_root.resolve()
        if args.writer_workspace_root is not None
        else openharness_root.parent.resolve()
    )
    writer_archive = (
        args.writer_archive.resolve()
        if args.writer_archive is not None
        else workspace_root / "docs" / "writer_director_0812.zip"
    )
    args.writer_workspace_root = workspace_root
    args.writer_archive = writer_archive
    if args.director_harness_enabled:
        if str(workspace_root) not in sys.path:
            sys.path.insert(0, str(workspace_root))
        if args.director_mcp_catalog is None:
            raise SystemExit(
                "--director-mcp-catalog is required when Director is enabled"
            )
        args.director_mcp_catalog = args.director_mcp_catalog.resolve()
        if not args.director_mcp_catalog.is_file():
            raise SystemExit(
                f"Director MCP catalog not found: {args.director_mcp_catalog}"
            )

    env_file = _discover_env_file(args, openharness_root)
    if env_file is not None:
        load_dotenv(dotenv_path=env_file, override=args.env_file is not None)
    api_key = os.environ.get("OPENAI_API_KEY")
    api_base = os.environ.get("OPENAI_API_BASE")
    if not api_key or not api_base:
        raise SystemExit("OPENAI_API_KEY and OPENAI_API_BASE must be configured")
    os.environ["OPENHARNESS_TOOL_OUTPUT_INLINE_CHARS"] = str(
        args.tool_output_inline_chars
    )

    audit = build_audit_report(root, args.language)
    candidates = _runtime_candidate_ids(audit, root, args.language)
    if args.task_ids:
        task_ids = list(dict.fromkeys(args.task_ids))
    elif args.mode == "representative":
        task_ids = list(REPRESENTATIVE_TASKS)
    else:
        task_ids = list(REPRESENTATIVE_TASKS) + [
            value for value in candidates if value not in REPRESENTATIVE_TASKS
        ]
    if args.max_tasks > 0:
        task_ids = task_ids[: args.max_tasks]
    if not task_ids:
        raise SystemExit("No MCP-Persona tasks were selected")
    if args.experiment_stage in {"writer-full", "writer-director-full"}:
        if tuple(task_ids) != VERIFIED_TASK_IDS:
            raise SystemExit(
                f"{args.experiment_stage} requires the exact canonical Verified52 task list"
            )
        if args.experiment_stage == "writer-full" and args.repeats != 1:
            raise SystemExit("writer-full is locked to exactly one repeat")
        if args.experiment_stage == "writer-director-full" and args.repeats != 2:
            raise SystemExit("writer-director-full is locked to exactly two repeats")
        if args.language != "en" or args.tool_scope != "server":
            raise SystemExit(
                f"{args.experiment_stage} requires --language en and --tool-scope server"
            )
        if args.openharness_mode != "writer_harness":
            raise SystemExit(
                f"{args.experiment_stage} requires --openharness-mode writer_harness"
            )
        if (args.writer_model or args.model) != args.model:
            raise SystemExit(
                f"{args.experiment_stage} requires Writer and actor to use the same model"
            )
        if (
            args.experiment_stage == "writer-director-full"
            and not args.director_harness_enabled
        ):
            raise SystemExit(
                "writer-director-full requires --director-harness-enabled"
            )

    version_manifest = build_version_manifest(
        openharness_root,
        root,
        model=args.model,
        api_base=api_base,
        mcp_persona_python=args.mcp_persona_python,
    )
    if args.openharness_mode == "writer_harness":
        writer_deployment = verify_writer_deployment(
            workspace_root,
            writer_archive,
            include_support_files=False,
        )
        if not writer_deployment.ok:
            raise SystemExit(
                "deployed Writer does not exactly match the team archive: "
                f"missing={list(writer_deployment.missing_files)}, "
                f"mismatched={list(writer_deployment.mismatched_files)}"
            )
        version_manifest["writer_harness"] = writer_deployment.to_dict()
        version_manifest["writer_harness"]["source_lock_enforced"] = True
        tracked = version_manifest.get("file_sha256")
        if isinstance(tracked, dict):
            for relative in (
                "src/openharness/rehearsal/writer_handoff.py",
                "scripts/run_mcp_persona_week1.py",
            ):
                path = openharness_root / relative
                tracked[str(path.resolve())] = hashlib.sha256(
                    path.read_bytes()
                ).hexdigest()
    if args.director_harness_enabled:
        tracked = version_manifest.get("file_sha256")
        if isinstance(tracked, dict):
            for path in sorted((workspace_root / "director_harness").glob("*.py")):
                tracked[str(path.resolve())] = hashlib.sha256(
                    path.read_bytes()
                ).hexdigest()
            for relative in (
                "src/openharness/engine/query.py",
                "src/openharness/engine/query_engine.py",
                "src/openharness/engine/stream_events.py",
            ):
                path = openharness_root / relative
                tracked[str(path.resolve())] = hashlib.sha256(
                    path.read_bytes()
                ).hexdigest()
    version_manifest["experiment"] = {
        "language": args.language,
        "mode": args.mode,
        "repeats": args.repeats,
        "tool_scope": args.tool_scope,
        "max_turns": args.max_turns,
        "max_tokens": args.max_tokens,
        "temperature": args.temperature,
        "seed": args.seed,
        "api_timeout_seconds": args.api_timeout,
        "trial_timeout_seconds": args.trial_timeout,
        "enable_thinking": args.enable_thinking,
        "tool_output_inline_chars": args.tool_output_inline_chars,
        "api_client_lifecycle": "one client per trial",
        "chain_guidance": args.chain_guidance,
        "experiment_stage": args.experiment_stage,
        "openharness_mode": args.openharness_mode,
        "writer_model": (
            args.writer_model or args.model
            if args.openharness_mode == "writer_harness"
            else None
        ),
        "director_harness_enabled": bool(args.director_harness_enabled),
        "director_mcp_catalog": (
            str(args.director_mcp_catalog)
            if args.director_harness_enabled
            else None
        ),
    }
    requested_config = _build_run_config(
        args=args,
        task_ids=task_ids,
        root=root,
        openharness_root=openharness_root,
        env_file=env_file,
        api_base=api_base,
        version_manifest=version_manifest,
    )
    run_config = _prepare_output_dir(
        output_dir,
        requested_config,
        resume=bool(args.resume),
    )
    task_ids = [int(value) for value in run_config["tasks"]]
    write_audit_json(audit, output_dir / "static-audit.json")
    write_task_csv(audit, output_dir / "static-audit.csv")
    _write_json(output_dir / "version-manifest.json", version_manifest)
    official_probe = probe_official_evaluators(root, args.mcp_persona_python)
    _write_json(output_dir / "official-evaluator-probe.json", official_probe)

    results_path = output_dir / "results.jsonl"
    infrastructure_failures_path = output_dir / "infrastructure-failures.jsonl"
    qwen_endpoint = "qwen" in args.model.lower() or "dashscope" in api_base.lower()
    extra_body = (
        {"enable_thinking": False}
        if qwen_endpoint and not args.enable_thinking
        else None
    )
    grouped = _load_existing_results(results_path) if args.resume else {}
    _validate_result_slots(grouped, task_ids, args.repeats)
    baseline_summary = _write_incremental_summary(output_dir, grouped, run_config)
    stop_due_to_account_failure = False
    for position, task_id in enumerate(task_ids, 1):
        if stop_due_to_account_failure:
            break
        task = task_by_id(root, task_id, args.language)
        print(f"[{position}/{len(task_ids)}] Task {task_id} ({task.get('query_type')})")
        grouped.setdefault(task_id, [])
        completed_trials = {
            int(value["trial"])
            for value in grouped[task_id]
            if isinstance(value.get("trial"), int)
            and _classify_trial(value) in VALID_TRIAL_STATES
        }
        for trial in range(1, args.repeats + 1):
            if trial in completed_trials:
                print(f"  trial {trial}/{args.repeats} already complete; skipping")
                continue
            print(f"  trial {trial}/{args.repeats} ...", flush=True)
            api_client = OpenAICompatibleClient(
                api_key,
                base_url=api_base,
                timeout=args.api_timeout,
                extra_body=extra_body,
                temperature=args.temperature,
                seed=args.seed,
            )
            try:
                try:
                    result = await _run_trial(
                        task=task,
                        trial=trial,
                        args=args,
                        root=root,
                        openharness_root=openharness_root,
                        api_client=api_client,
                        task_dir=output_dir / "artifacts" / f"task-{task_id}",
                    )
                except Exception as exc:
                    result = _runner_failure_result(task=task, trial=trial, exc=exc)
            finally:
                await api_client.close()
            redacted = _redact_secret(result, api_key)
            if not isinstance(redacted, dict):
                raise TypeError("redacted trial result is not an object")
            result = redacted
            trial_state = _classify_trial(result)
            if trial_state == "infrastructure_failed":
                _append_infrastructure_failure(infrastructure_failures_path, result)
                print(
                    "    infrastructure failure; this slot remains retryable with --resume "
                    f"(errors={len(result.get('errors', []))})"
                )
                if _fatal_account_failure(result):
                    stop_due_to_account_failure = True
                    print(
                        "    fatal account/authentication failure detected; "
                        "stopping early. Fix the account and rerun the same --resume command."
                    )
            else:
                grouped[task_id] = [
                    value
                    for value in grouped[task_id]
                    if int(value["trial"]) != trial
                ]
                grouped[task_id].append(result)
                grouped[task_id].sort(key=lambda value: int(value["trial"]))
                _write_results_jsonl(results_path, grouped)
                scores = result["local_scores"]
                print(
                    "    calls={calls} sequence={sequence:.2f} checkpoint={checkpoint:.2f} "
                    "execution={execution:.2f} reset={reset} state={state}".format(
                        calls=scores["actual_call_count"],
                        sequence=scores["sequence_score"],
                        checkpoint=scores["checkpoint_score"],
                        execution=scores["execution_score"],
                        reset=scores["exact_reset"],
                        state=trial_state,
                    )
                )
            baseline_summary = _write_incremental_summary(
                output_dir,
                grouped,
                run_config,
            )
            if stop_due_to_account_failure:
                break

    baseline_summary = _write_incremental_summary(output_dir, grouped, run_config)
    summary = {
        "schema_version": 1,
        "result_scope": RESULT_LABEL,
        "dataset_id": run_config.get("dataset_id"),
        "mode": args.mode,
        "task_ids": task_ids,
        "task_count": len(task_ids),
        "repeats": args.repeats,
        "model": args.model,
        "chain_guidance_enabled": args.chain_guidance,
        "static_coverage_candidate_count": audit["summary"][
            "coverage_candidate_task_count"
        ],
        "static_strict_candidate_count": audit["summary"]["strict_candidate_task_count"],
        "runtime_candidate_count": len(candidates),
        "result_count": baseline_summary["result_count"],
        "expected_results": baseline_summary["expected_results"],
        "model_outcome_counts": baseline_summary["state_counts"],
        "local_score_means": baseline_summary["local_score_means"],
        "run_complete": baseline_summary["run_complete"],
        "formal_verified52": baseline_summary["formal_verified52"],
        "baseline_ready": baseline_summary["baseline_ready"],
        "writer_smoke_gate": baseline_summary["writer_smoke_gate"],
        "director_smoke_gate": baseline_summary["director_smoke_gate"],
        "official_evaluator_runnable": all(
            value.get("runnable") for value in official_probe.values()
        ),
        "local_checkpoint_and_execution_scores_generated": True,
        "membership_policy": (
            "One fixed task set only. Model score, chain completion, or trial "
            "stability never changes dataset membership."
        ),
        "warning": (
            "Do not compare local scores directly with official full-benchmark scores. "
            "This is an OpenHarness-compatible local result."
        ),
    }
    _write_json(output_dir / "summary.json", summary)
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    print(f"Week-one report: {output_dir / 'summary.json'}")
    print(
        "Baseline completeness: "
        f"{baseline_summary['result_count']}/{baseline_summary['expected_results']} "
        f"(ready={baseline_summary['baseline_ready']})"
    )
    writer_gate = baseline_summary["writer_smoke_gate"]
    director_gate = baseline_summary["director_smoke_gate"]
    complete = baseline_summary["run_complete"] and (
        args.openharness_mode != "writer_harness"
        or writer_gate["passed"] is True
    ) and (
        not args.director_harness_enabled
        or director_gate["passed"] is True
    )
    return 0 if complete else 2


def main() -> int:
    return asyncio.run(async_main(parse_args()))


if __name__ == "__main__":
    raise SystemExit(main())
