#!/usr/bin/env python3
"""Recover one completed HarnessBench sandbox without rerunning the agent."""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import Any

from dotenv import load_dotenv


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--workspace-root", type=Path, required=True)
    parser.add_argument("--result-root", type=Path, required=True)
    parser.add_argument("--env-file", type=Path, required=True)
    parser.add_argument("--repeat", type=int, required=True)
    parser.add_argument("--task-id", required=True)
    parser.add_argument("--sandbox", type=Path, required=True)
    parser.add_argument(
        "--check-only",
        action="store_true",
        help="Validate the existing sandbox and deterministic Oracle without API calls",
    )
    return parser


def _read_object(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"JSON file must contain an object: {path}")
    return value


def _read_rounds(path: Path) -> list[dict[str, Any]]:
    rounds: list[dict[str, Any]] = []
    for line_number, line in enumerate(
        path.read_text(encoding="utf-8").splitlines(), start=1
    ):
        if not line.strip():
            continue
        value = json.loads(line)
        if not isinstance(value, dict):
            raise ValueError(f"round {line_number} is not an object: {path}")
        rounds.append(value)
    if not rounds:
        raise ValueError(f"no completed adapter rounds found: {path}")
    return rounds


def _command_for_round(
    harness_config: dict[str, Any],
    *,
    workspace: Path,
    sandbox: Path,
    prompt_file: Path,
    session_id: str,
    task_id: str,
) -> list[str]:
    models = harness_config.get("models")
    model_config = models.get("openharness-local") if isinstance(models, dict) else None
    if not isinstance(model_config, dict):
        raise ValueError("harness.local.json has no openharness-local model")
    command = str(model_config.get("command") or "")
    raw_args = model_config.get("args")
    if not command or not isinstance(raw_args, list):
        raise ValueError("invalid openharness-local command configuration")
    values = {
        "workspace": str(workspace),
        "sandbox": str(sandbox),
        "prompt_file": str(prompt_file),
        "session_id": session_id,
        "task_id": task_id,
        "model_id": "openharness-local",
    }
    return [command, *[str(item).format(**values) for item in raw_args]]


def _actor_elapsed_seconds(sandbox: Path, rounds_file: Path) -> float:
    match = re.search(r"-(\d{8}-\d{6})-[^-]+$", sandbox.name)
    if match is None:
        return 0.0
    started_at = datetime.strptime(match.group(1), "%Y%m%d-%H%M%S").timestamp()
    return round(max(0.0, rounds_file.stat().st_mtime - started_at), 3)


def _configure_rubric_environment(
    env_file: Path, run_config: dict[str, Any], *, check_only: bool
) -> None:
    if not env_file.is_file():
        raise ValueError(f"env file not found: {env_file}")
    load_dotenv(env_file, override=True)
    grading = run_config.get("grading")
    if not isinstance(grading, dict) or grading.get("mode") != "full":
        raise ValueError("result run was not configured for full grading")
    key_source = str(grading.get("rubric_api_key_source") or "OPENAI_API_KEY")
    key = os.environ.get(key_source, "").strip()
    base_url = str(grading.get("rubric_base_url") or "").strip()
    model = str(grading.get("rubric_model") or run_config.get("model") or "").strip()
    if not key or not base_url or not model:
        raise ValueError("full grading credentials or model configuration are incomplete")
    os.environ["RUBRIC_API_KEY"] = key
    os.environ["RUBRIC_BASE_URL"] = base_url.rstrip("/")
    os.environ["RUBRIC_MODEL"] = model
    os.environ["RUBRIC_VISION_MODEL"] = str(
        grading.get("rubric_vision_model") or model
    )
    if check_only:
        os.environ["HARNESSBENCH_SKIP_PROCESS_GRADE"] = "1"
        os.environ["HARNESSBENCH_SKIP_ORACLE_QUALITY_LLM"] = "1"
    else:
        os.environ.pop("HARNESSBENCH_SKIP_PROCESS_GRADE", None)
        os.environ.pop("HARNESSBENCH_SKIP_ORACLE_QUALITY_LLM", None)


def main() -> int:
    args = _parser().parse_args()
    workspace_root = args.workspace_root.resolve()
    result_root = args.result_root.resolve()
    env_file = args.env_file.resolve()
    sandbox = args.sandbox.resolve()
    harnessbench_root = workspace_root / "HarnessBench"
    openharness_root = workspace_root / "OpenHarness"
    output_dir = result_root / "HarnessBench"
    repeat_dir = output_dir / f"repeat-{args.repeat:02d}"
    run_config = _read_object(output_dir / "run-config.json")

    if args.task_id not in run_config.get("tasks", []):
        raise ValueError(f"task is not part of the locked run: {args.task_id}")
    if not sandbox.is_relative_to(repeat_dir / "sandboxes"):
        raise ValueError(f"sandbox is outside repeat {args.repeat}: {sandbox}")

    harnessbench_src = harnessbench_root / "src"
    sys.path.insert(0, str(harnessbench_src))
    sys.path.insert(0, str(openharness_root))
    os.environ["HARNESSBENCH_APP_CONFIG"] = str(repeat_dir / "app.local.json")
    os.environ["HARNESSBENCH_HARNESS_CONFIG"] = str(output_dir / "harness.local.json")

    from harnessbench.extract_proxy_trace import extract_proxy_trace_incremental
    from harnessbench.grading.process_grade import compute_scoring
    from harnessbench.runner import _collect_proxy_usage_summary
    from harnessbench.tasks import load_tasks, run_oracle
    from scripts.run_openharness_harnessbench import (
        RESULT_LABEL,
        _director_gate,
        _full_grading_complete,
        _summary_for_output,
        _writer_gate,
        _write_json,
    )

    tasks = load_tasks(harnessbench_root / "tasks")
    if args.task_id not in tasks:
        raise ValueError(f"HarnessBench task not found: {args.task_id}")

    rounds_file = sandbox / "openharness-harnessbench" / "rounds.jsonl"
    rounds = _read_rounds(rounds_file)
    if any(row.get("task_id") != args.task_id for row in rounds):
        raise ValueError("sandbox rounds do not match the requested task")
    if any(row.get("status") == "openharness_process_failed" for row in rounds):
        raise ValueError("sandbox contains an OpenHarness infrastructure failure")

    session_ids = {str(row.get("session_id") or "") for row in rounds}
    if len(session_ids) != 1 or "" in session_ids:
        raise ValueError("sandbox rounds do not have one stable session ID")
    session_id = next(iter(session_ids))
    workspace = sandbox / "workspace"
    proxy_dir = sandbox / "usage-proxy"
    trace = extract_proxy_trace_incremental(proxy_dir)
    if trace.get("error"):
        raise ValueError(f"proxy trace is incomplete: {trace['error']}")
    usage = _collect_proxy_usage_summary(proxy_dir / "requests.jsonl", session_id)
    if usage.get("available") is not True:
        raise ValueError(f"proxy usage is incomplete: {usage}")

    _configure_rubric_environment(env_file, run_config, check_only=args.check_only)
    oracle_result = run_oracle(tasks[args.task_id], workspace)
    if oracle_result.get("error"):
        raise ValueError(f"Oracle failed: {oracle_result['error']}")

    harness_config = _read_object(output_dir / "harness.local.json")
    prompt_files = sorted(sandbox.glob("prompt-round*.txt"))
    if len(prompt_files) != len(rounds):
        raise ValueError("prompt file count does not match completed round count")
    adapter_rounds = []
    for round_payload, prompt_file in zip(rounds, prompt_files, strict=True):
        adapter_rounds.append(
            {
                "ok": True,
                "command": _command_for_round(
                    harness_config,
                    workspace=workspace,
                    sandbox=sandbox,
                    prompt_file=prompt_file,
                    session_id=session_id,
                    task_id=args.task_id,
                ),
                "stdout": json.dumps(round_payload, ensure_ascii=False),
                "stderr": "",
                "metadata": {"returncode": 0},
            }
        )

    payload: dict[str, Any] = {
        "task_id": args.task_id,
        "model_id": "openharness-local",
        "api_model_slug": str(run_config["model"]),
        "api_model_label": str(run_config["model"]),
        "mode": "live",
        "sandbox": str(sandbox),
        "workspace": str(workspace),
        "session_id": session_id,
        "prompt_file": str(prompt_files[-1]),
        "adapter_result": {
            **adapter_rounds[-1],
            "stdout": json.dumps(trace, ensure_ascii=False, indent=2),
        },
        "adapter_results": adapter_rounds,
        "usage_summary": usage,
        "oracle_result": oracle_result,
        "scoring": {},
        "runtime_state": {},
        "elapsed_sec": _actor_elapsed_seconds(sandbox, rounds_file),
        "result_label": RESULT_LABEL,
        "dataset_id": run_config.get("dataset_id"),
        "openharness_mode": run_config.get("openharness_mode"),
        "director_harness_enabled": run_config.get("director_harness_enabled"),
        "grading_mode": run_config.get("grading", {}).get("mode"),
    }
    writer_gate = _writer_gate(payload)
    director_gate = _director_gate(payload)
    if run_config.get("openharness_mode") == "writer_harness" and not writer_gate["passed"]:
        raise ValueError(f"Writer gate failed in recovered sandbox: {writer_gate}")
    if run_config.get("director_harness_enabled") is True and not director_gate["passed"]:
        raise ValueError(f"Director gate failed in recovered sandbox: {director_gate}")

    if args.check_only:
        print(
            json.dumps(
                {
                    "check_only": True,
                    "task_id": args.task_id,
                    "repeat": args.repeat,
                    "agent_statuses": [row.get("status") for row in rounds],
                    "oracle_outcome_score": oracle_result.get("outcome_score"),
                    "proxy_request_count": usage.get("request_count"),
                    "writer_gate": writer_gate,
                    "director_gate": director_gate,
                    "paid_process_grade_required": True,
                },
                ensure_ascii=False,
                indent=2,
            )
        )
        return 0

    grading_started = time.perf_counter()
    scoring = compute_scoring(tasks[args.task_id], sandbox, oracle_result)
    payload["elapsed_sec"] = round(
        float(payload["elapsed_sec"]) + time.perf_counter() - grading_started, 3
    )
    payload["scoring"] = scoring
    if not _full_grading_complete(payload):
        raise ValueError(f"paid process grading did not produce a complete score: {scoring}")

    result_matches = sorted(
        (repeat_dir / "results").rglob(f"{args.task_id}.json")
    )
    if len(result_matches) != 1:
        raise ValueError(
            f"expected one existing result for replacement, found {len(result_matches)}"
        )
    result_file = result_matches[0]
    previous = _read_object(result_file)
    previous_adapter = previous.get("adapter_result")
    previous_oracle = previous.get("oracle_result")
    if not (
        isinstance(previous_adapter, dict)
        and previous_adapter.get("ok") is False
        or isinstance(previous_oracle, dict)
        and previous_oracle.get("error")
    ):
        raise ValueError(f"refusing to replace a non-infrastructure result: {result_file}")

    recovery_dir = sandbox / "recovery"
    recovery_dir.mkdir(parents=True, exist_ok=True)
    backup = recovery_dir / "previous-result.json"
    if not backup.exists():
        shutil.copy2(result_file, backup)
    temporary = result_file.with_suffix(".json.recovering")
    _write_json(temporary, payload)
    temporary.replace(result_file)
    summary = _summary_for_output(output_dir, run_config)
    _write_json(output_dir / "baseline-summary.json", summary)
    print(
        json.dumps(
            {
                "recovered_result": str(result_file),
                "backup": str(backup),
                "oracle_outcome_score": oracle_result.get("outcome_score"),
                "combined_score": scoring.get("combined_score"),
                "baseline_ready": summary.get("baseline_ready"),
            },
            ensure_ascii=False,
            indent=2,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
