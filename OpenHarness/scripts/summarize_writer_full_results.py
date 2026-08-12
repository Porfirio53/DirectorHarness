#!/usr/bin/env python3
"""Preflight and summarize the configured Writer full runs on both benchmarks."""

from __future__ import annotations

import argparse
import json
import subprocess
from pathlib import Path
from statistics import mean, median
from typing import Any, Mapping, Sequence

from openharness.rehearsal.mcp_persona_rehearsal import VERIFIED_TASK_IDS
from openharness.rehearsal.writer_handoff import verify_writer_deployment


MODEL = "qwen3.6-plus"
MCP_OUTPUT_NAME = "MCP-Persona"
HARNESSBENCH_OUTPUT_NAME = "HarnessBench"
MCP_BASELINE_CONFIG = "results/full/results_without_Writer/MCP-Persona/run-config.json"
HARNESSBENCH_MANIFEST = "results/config/harnessbench-writer-full-v1.tasks.json"


def _read_json(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"{path} is not a JSON object")
    return value


def _read_jsonl(path: Path) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for line_number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        if not line.strip():
            continue
        value = json.loads(line)
        if not isinstance(value, dict):
            raise ValueError(f"{path}:{line_number} is not a JSON object")
        rows.append(value)
    return rows


def _write_json(path: Path, value: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.tmp")
    temporary.write_text(
        json.dumps(value, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    temporary.replace(path)


def _git_commit(root: Path) -> str | None:
    completed = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=root,
        text=True,
        capture_output=True,
        check=False,
    )
    return completed.stdout.strip() if completed.returncode == 0 else None


def _number(value: Any) -> float | None:
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return float(value)
    return None


def _stats(values: Sequence[float]) -> dict[str, Any]:
    return {
        "coverage": len(values),
        "mean": round(mean(values), 6) if values else None,
        "median": round(median(values), 6) if values else None,
        "min": round(min(values), 6) if values else None,
        "max": round(max(values), 6) if values else None,
    }


def _locked_output_status(
    output_dir: Path,
    *,
    expected: Mapping[str, Any],
) -> str:
    if not output_dir.exists() or not any(output_dir.iterdir()):
        return "fresh"
    config_path = output_dir / "run-config.json"
    if not config_path.is_file():
        raise ValueError(f"non-empty output directory has no run-config.json: {output_dir}")
    config = _read_json(config_path)
    mismatches = [key for key, value in expected.items() if config.get(key) != value]
    if mismatches:
        raise ValueError(
            f"{output_dir} belongs to a different run; mismatched fields: " + ", ".join(mismatches)
        )
    return "resumable"


def build_preflight(
    *,
    workspace_root: Path,
    env_file: Path,
    result_root: Path,
    model: str = MODEL,
    repeats: int = 1,
) -> dict[str, Any]:
    workspace_root = workspace_root.resolve()
    mcp_root = workspace_root / "MCP-Persona"
    harnessbench_root = workspace_root / "HarnessBench"
    archive = workspace_root / "writer_harness_demo.zip"
    env_file = env_file.resolve()
    result_root = result_root.resolve()

    if model != MODEL:
        raise ValueError(f"full experiment is locked to {MODEL}, got {model}")
    if repeats not in {1, 2}:
        raise ValueError(f"Writer full experiment supports one or two repeats, got {repeats}")
    if not env_file.is_file():
        raise ValueError(f"env file not found: {env_file}")
    if not (mcp_root / "data/tasks/en_release_data.json").is_file():
        raise ValueError(f"invalid MCP-Persona root: {mcp_root}")
    if not (harnessbench_root / "src/harnessbench/cli.py").is_file():
        raise ValueError(f"invalid HarnessBench root: {harnessbench_root}")

    deployment = verify_writer_deployment(
        workspace_root,
        archive,
        include_support_files=False,
    )
    current_hb_commit = _git_commit(harnessbench_root)

    mcp_source = _read_json(workspace_root / MCP_BASELINE_CONFIG)
    mcp_tasks = tuple(int(value) for value in mcp_source.get("tasks", []))
    if mcp_tasks != VERIFIED_TASK_IDS:
        raise ValueError("frozen MCP source is not the canonical Verified52 list")

    hb_manifest_path = workspace_root / HARNESSBENCH_MANIFEST
    hb_manifest = _read_json(hb_manifest_path)
    hb_tasks = tuple(str(value) for value in hb_manifest.get("tasks", []))
    if (
        len(hb_tasks) != 106
        or len(set(hb_tasks)) != 106
        or hb_manifest.get("task_count") != 106
        or hb_manifest.get("excluded_tasks") != []
    ):
        raise ValueError("frozen HarnessBench manifest is not the complete 106-task set")
    missing_hb_tasks = [
        task_id for task_id in hb_tasks if not (harnessbench_root / "tasks" / task_id).is_dir()
    ]
    if missing_hb_tasks:
        raise ValueError(
            "HarnessBench task directories are missing: " + ", ".join(missing_hb_tasks[:10])
        )
    mcp_output = result_root / MCP_OUTPUT_NAME
    hb_output = result_root / HARNESSBENCH_OUTPUT_NAME
    mcp_output_status = _locked_output_status(
        mcp_output,
        expected={
            "tasks": list(VERIFIED_TASK_IDS),
            "repeats": repeats,
            "model": model,
            "writer_model": model,
            "language": "en",
            "tool_scope": "server",
            "chain_guidance": False,
            "experiment_stage": "writer-full",
            "openharness_mode": "writer_harness",
        },
    )
    hb_output_status = _locked_output_status(
        hb_output,
        expected={
            "tasks": list(hb_tasks),
            "repeats": repeats,
            "model": model,
            "writer_model": model,
            "openharness_mode": "writer_harness",
            "public_url_mode": "loopback",
        },
    )
    if hb_output_status == "resumable":
        hb_config = _read_json(hb_output / "run-config.json")
        grading = hb_config.get("grading")
        if not isinstance(grading, Mapping) or grading.get("mode") != "full":
            raise ValueError(f"{hb_output} is not locked to full HarnessBench grading")

    return {
        "schema_version": 1,
        "ready": True,
        "external_model_called": False,
        "model": model,
        "writer_model": model,
        "writer_deployment": {
            "ok": deployment.ok,
            "checked_files": deployment.checked_files,
            "archive_sha256": deployment.archive_sha256,
            "package_root": deployment.package_root,
            "source_lock_enforced": False,
        },
        "mcp_persona": {
            "dataset": "Verified52",
            "task_count": len(mcp_tasks),
            "repeats": repeats,
            "output_dir": str(mcp_output),
            "output_status": mcp_output_status,
        },
        "harnessbench": {
            "dataset": hb_manifest.get("dataset_id"),
            "commit": current_hb_commit,
            "task_count": len(hb_tasks),
            "excluded_task_count": 0,
            "repeats": repeats,
            "grading": "full",
            "output_dir": str(hb_output),
            "output_status": hb_output_status,
        },
        "resume_policy": (
            "A matching run-config.json is resumable; a different or untracked "
            "non-empty output directory is rejected."
        ),
        "env_file": str(env_file),
    }


def _writer_usage_from_adapter(payload: Mapping[str, Any]) -> tuple[int, int]:
    adapters = payload.get("adapter_results")
    if not isinstance(adapters, list):
        adapter = payload.get("adapter_result")
        adapters = [adapter] if isinstance(adapter, Mapping) else []
    input_tokens = 0
    output_tokens = 0
    for adapter in adapters:
        if not isinstance(adapter, Mapping):
            continue
        stdout = adapter.get("stdout")
        if not isinstance(stdout, str) or not stdout.strip():
            continue
        try:
            public_result = json.loads(stdout)
        except json.JSONDecodeError:
            continue
        if not isinstance(public_result, Mapping):
            continue
        usage = public_result.get("writer_usage")
        if isinstance(usage, Mapping):
            input_tokens += int(usage.get("input_tokens", 0) or 0)
            output_tokens += int(usage.get("output_tokens", 0) or 0)
    return input_tokens, output_tokens


def _harnessbench_metrics(
    summary: Mapping[str, Any],
) -> dict[str, Any]:
    score_values: dict[str, list[float]] = {
        "outcome": [],
        "process": [],
        "security": [],
        "quality": [],
        "combined": [],
    }
    actor_input = 0
    actor_output = 0
    writer_input = 0
    writer_output = 0
    durations: list[float] = []
    task_rows: list[Mapping[str, Any]] = []
    for repeat in summary.get("repeats", []):
        if isinstance(repeat, Mapping) and isinstance(repeat.get("tasks"), list):
            task_rows.extend(row for row in repeat["tasks"] if isinstance(row, Mapping))
    for row in task_rows:
        result_file = row.get("result_file")
        if not isinstance(result_file, str):
            continue
        payload = _read_json(Path(result_file))
        oracle = payload.get("oracle_result")
        scoring = payload.get("scoring")
        if isinstance(oracle, Mapping):
            value = _number(oracle.get("outcome_score"))
            if value is not None:
                score_values["outcome"].append(value)
            value = _number(oracle.get("quality"))
            if value is not None:
                score_values["quality"].append(value)
        if isinstance(scoring, Mapping):
            for source, target in (
                ("process_score", "process"),
                ("security_score", "security"),
                ("combined_score", "combined"),
            ):
                value = _number(scoring.get(source))
                if value is not None:
                    score_values[target].append(value)
        usage = payload.get("usage_summary")
        if isinstance(usage, Mapping):
            actor_input += int(usage.get("input_tokens", 0) or 0)
            actor_output += int(usage.get("output_tokens", 0) or 0)
        current_writer_input, current_writer_output = _writer_usage_from_adapter(payload)
        writer_input += current_writer_input
        writer_output += current_writer_output
        duration = _number(payload.get("elapsed_sec"))
        if duration is not None:
            durations.append(duration)
    actor_total = actor_input + actor_output
    writer_total = writer_input + writer_output
    return {
        "scores": {name: _stats(values) for name, values in score_values.items()},
        "tokens": {
            "actor": {
                "input": actor_input,
                "output": actor_output,
                "total": actor_total,
            },
            "writer": {
                "input": writer_input,
                "output": writer_output,
                "total": writer_total,
            },
            "combined_total": actor_total + writer_total,
        },
        "latency_seconds": {
            **_stats(durations),
            "total": round(sum(durations), 6),
        },
    }


def _mcp_metrics(
    rows: Sequence[Mapping[str, Any]],
) -> dict[str, Any]:
    actor_input = 0
    actor_output = 0
    writer_input = 0
    writer_output = 0
    durations: list[float] = []
    for row in rows:
        usage = row.get("usage")
        if isinstance(usage, Mapping):
            actor_input += int(usage.get("input_tokens", 0) or 0)
            actor_output += int(usage.get("output_tokens", 0) or 0)
        writer = row.get("writer")
        if isinstance(writer, Mapping) and isinstance(writer.get("usage"), Mapping):
            writer_usage = writer["usage"]
            writer_input += int(writer_usage.get("input_tokens", 0) or 0)
            writer_output += int(writer_usage.get("output_tokens", 0) or 0)
        duration = _number(row.get("duration_seconds"))
        if duration is not None:
            durations.append(duration)
    actor_total = actor_input + actor_output
    writer_total = writer_input + writer_output
    return {
        "tokens": {
            "actor": {
                "input": actor_input,
                "output": actor_output,
                "total": actor_total,
            },
            "writer": {
                "input": writer_input,
                "output": writer_output,
                "total": writer_total,
            },
            "combined_total": actor_total + writer_total,
        },
        "latency_seconds": {
            **_stats(durations),
            "total": round(sum(durations), 6),
        },
    }


def build_summary(
    *,
    mcp_output: Path,
    harnessbench_output: Path,
) -> dict[str, Any]:
    mcp_output = mcp_output.resolve()
    harnessbench_output = harnessbench_output.resolve()
    mcp_config = _read_json(mcp_output / "run-config.json")
    mcp_run = _read_json(mcp_output / "baseline-summary.json")
    mcp_semantic = _read_json(mcp_output / "semantic-summary.json")
    mcp_step = _read_json(mcp_output / "rehearsal-step-summary.json")
    mcp_rows = _read_jsonl(mcp_output / "results.jsonl")
    hb_config = _read_json(harnessbench_output / "run-config.json")
    hb_run = _read_json(harnessbench_output / "baseline-summary.json")

    mcp_repeats = int(mcp_config.get("repeats", 0) or 0)
    hb_repeats = int(hb_config.get("repeats", 0) or 0)
    if mcp_repeats not in {1, 2} or hb_repeats != mcp_repeats:
        raise ValueError(
            "Writer full summary requires matching one- or two-repeat MCP and "
            "HarnessBench result sets"
        )
    expected_trial_count = 52 * mcp_repeats
    expected_hb_results = 106 * hb_repeats
    mcp_keys = {(int(row["task_id"]), int(row["trial"])) for row in mcp_rows}
    expected_mcp_keys = {
        (task_id, trial)
        for task_id in VERIFIED_TASK_IDS
        for trial in range(1, mcp_repeats + 1)
    }
    mcp_writer_gate = mcp_run.get("writer_smoke_gate")
    mcp_run_ready = (
        mcp_config.get("writer_full_verified52") is True
        and mcp_config.get("repeats") == mcp_repeats
        and mcp_config.get("model") == MODEL
        and mcp_config.get("writer_model") == MODEL
        and mcp_config.get("openharness_mode") == "writer_harness"
        and mcp_keys == expected_mcp_keys
        and mcp_run.get("run_complete") is True
        and isinstance(mcp_writer_gate, Mapping)
        and mcp_writer_gate.get("passed") is True
    )
    semantic_ready = (
        mcp_semantic.get("expected_task_count") == 52
        and mcp_semantic.get("expected_trial_count") == expected_trial_count
        and mcp_semantic.get("semantic_review_ready") is True
    )
    process_ready = (
        mcp_step.get("expected_task_count") == 52
        and mcp_step.get("expected_trial_count") == expected_trial_count
        and mcp_step.get("structurally_complete") is True
        and mcp_step.get("semantic_review_complete") is True
    )

    hb_writer_gate = hb_run.get("writer_smoke_gate")
    hb_grading = hb_config.get("grading")
    hb_scoring_ready = (
        hb_config.get("dataset_id") == "harnessbench-writer-full-v1"
        and len(hb_config.get("tasks", [])) == 106
        and len(set(hb_config.get("tasks", []))) == 106
        and hb_config.get("repeats") == hb_repeats
        and hb_config.get("model") == MODEL
        and hb_config.get("writer_model") == MODEL
        and hb_config.get("openharness_mode") == "writer_harness"
        and isinstance(hb_grading, Mapping)
        and hb_grading.get("mode") == "full"
        and hb_run.get("expected_results") == expected_hb_results
        and hb_run.get("result_count") == expected_hb_results
        and hb_run.get("full_grading_complete") is True
    )
    hb_writer_gate_passed = (
        isinstance(hb_writer_gate, Mapping)
        and hb_writer_gate.get("passed") is True
    )
    hb_state_counts = hb_run.get("state_counts")
    if not isinstance(hb_state_counts, Mapping):
        hb_state_counts = {}
    hb_execution_failure_count = sum(
        int(count)
        for state, count in hb_state_counts.items()
        if state not in {"completed", "pending"}
        and isinstance(count, int)
        and not isinstance(count, bool)
    )

    return {
        "schema_version": 1,
        "result_scope": (
            f"Writer-enabled {mcp_repeats}-repeat full runs; local OpenHarness-compatible "
            "scores, not official remote benchmark submissions"
        ),
        "model": MODEL,
        "writer_model": MODEL,
        "repeats": mcp_repeats,
        "all_scoring_complete": (
            mcp_run_ready and semantic_ready and process_ready and hb_scoring_ready
        ),
        "mcp_persona": {
            "dataset": "Verified52",
            "task_count": 52,
            "repeat_count": mcp_repeats,
            "trial_count": expected_trial_count,
            "run_complete": mcp_run_ready,
            "process_score_complete": process_ready,
            "semantic_score_complete": semantic_ready,
            "final_score": {
                "name": "local_execution_score",
                "mean": mcp_run.get("local_score_means", {}).get("execution_score"),
            },
            "process_score": mcp_step.get("step_score"),
            "semantic_score": mcp_semantic.get("semantic_score_means"),
            "rehearsal_aware_combined": mcp_step.get("rehearsal_aware_combined"),
            "all_local_score_means": mcp_run.get("local_score_means"),
            "resources": _mcp_metrics(mcp_rows),
            "source_dir": str(mcp_output),
        },
        "harnessbench": {
            "dataset": hb_config.get("dataset_id"),
            "task_count": 106,
            "repeat_count": hb_repeats,
            "trajectory_count": expected_hb_results,
            "run_and_full_grading_complete": hb_scoring_ready,
            "execution_gate_passed": hb_run.get("baseline_ready") is True,
            "writer_gate_passed": hb_writer_gate_passed,
            "execution_failure_count": hb_execution_failure_count,
            "state_counts": dict(hb_state_counts),
            "benchmark_scores_and_resources": _harnessbench_metrics(hb_run),
            "source_dir": str(harnessbench_output),
        },
    }


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)
    preflight = subparsers.add_parser(
        "preflight",
        help="Validate the 52/106 task scopes, inspect Writer provenance, and check outputs",
    )
    preflight.add_argument("--workspace-root", type=Path, required=True)
    preflight.add_argument("--env-file", type=Path, required=True)
    preflight.add_argument("--result-root", type=Path, required=True)
    preflight.add_argument("--model", default=MODEL)
    preflight.add_argument("--repeats", type=int, choices=(1, 2), default=1)
    preflight.add_argument("--output", type=Path)

    summarize = subparsers.add_parser(
        "summarize",
        help="Combine already-produced scores, token usage, and latency without an actor call",
    )
    summarize.add_argument("--mcp-output", type=Path, required=True)
    summarize.add_argument("--harnessbench-output", type=Path, required=True)
    summarize.add_argument("--output", type=Path, required=True)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.command == "preflight":
        payload = build_preflight(
            workspace_root=args.workspace_root,
            env_file=args.env_file,
            result_root=args.result_root,
            model=args.model,
            repeats=args.repeats,
        )
        if args.output is not None:
            _write_json(args.output, payload)
    else:
        payload = build_summary(
            mcp_output=args.mcp_output,
            harnessbench_output=args.harnessbench_output,
        )
        _write_json(args.output, payload)
    print(json.dumps(payload, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
