#!/usr/bin/env python3
"""Run OpenHarness through HarnessBench's existing generic_cli adapter."""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import importlib.util
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from statistics import mean, median
from typing import Any, Mapping, Sequence

from dotenv import dotenv_values, load_dotenv

RESULT_LABEL = "OpenHarness-compatible local result"
DEFAULT_TASKS = ("001-file", "021-batch-rename-transform", "007-session-memory")
LOOPBACK_MOCK_TASKS = (
    "003-browser",
    "006-access-bilibili",
    "078-local-api-cursor-retry-ledger",
    "081-local-html-dom-form-extract",
    "088-api-contract-mock-client-compat",
)
LOOPBACK_PROBES = {
    "003-browser": ("MOCK_PAGE", ""),
    "006-access-bilibili": ("MOCK_PAGE", ""),
    "078-local-api-cursor-retry-ledger": ("MOCK_API_BASE", "/checkpoint"),
    "081-local-html-dom-form-extract": ("MOCK_SITE_BASE", "/"),
    "088-api-contract-mock-client-compat": ("MOCK_API_BASE", "/v1/users?page=1"),
}


def _adapter_parser(subparsers: Any) -> None:
    parser = subparsers.add_parser("adapter", help="Execute one generic_cli adapter round")
    parser.add_argument("--workspace", type=Path, required=True)
    parser.add_argument("--sandbox", type=Path, required=True)
    parser.add_argument("--prompt-file", type=Path, required=True)
    parser.add_argument("--session-id", required=True)
    parser.add_argument("--task-id", required=True)
    parser.add_argument("--model")
    parser.add_argument("--profile")
    parser.add_argument("--api-format")
    parser.add_argument("--temperature", type=float)
    parser.add_argument("--seed", type=int)
    parser.add_argument("--max-turns", type=int)
    parser.add_argument("--api-timeout-sec", type=float, default=300.0)
    parser.add_argument("--permission-mode", default="full_auto")
    parser.add_argument("--env-file", type=Path)
    parser.add_argument(
        "--openharness-mode",
        choices=("original", "writer_harness"),
        default="original",
    )
    parser.add_argument("--writer-workspace-root", type=Path)
    parser.add_argument("--writer-model")
    parser.add_argument("--writer-max-tokens", type=int, default=4096)
    parser.add_argument("--director-harness-enabled", action="store_true")
    parser.add_argument("--director-mcp-catalog", type=Path)


def _add_source_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--harnessbench-root", type=Path, default=Path("../HarnessBench"))
    selection = parser.add_mutually_exclusive_group()
    selection.add_argument(
        "--tasks",
        default=None,
        help="Comma-separated task IDs, or 'all' for every HarnessBench task",
    )
    selection.add_argument(
        "--task-manifest",
        type=Path,
        help="Manifest containing the ordered HarnessBench task list",
    )
    parser.add_argument("--model", required=True)
    parser.add_argument("--profile", default="qwen")
    parser.add_argument("--api-format", default="openai")
    parser.add_argument("--temperature", type=float, default=0.0)
    parser.add_argument("--seed", type=int, default=42)
    parser.add_argument("--env-file", type=Path, required=True)
    parser.add_argument("--max-turns", type=int, default=80)
    parser.add_argument("--api-timeout-sec", type=float, default=300.0)
    parser.add_argument(
        "--grading",
        choices=("outcome-only", "full"),
        default="outcome-only",
        help="Deterministic Oracle only, or Oracle plus process/security/quality LLM grading",
    )
    parser.add_argument("--rubric-model")
    parser.add_argument("--rubric-vision-model")
    parser.add_argument("--rubric-base-url")
    parser.add_argument("--rubric-base-url-env", default="OPENAI_API_BASE")
    parser.add_argument("--rubric-api-key-env", default="OPENAI_API_KEY")
    parser.add_argument(
        "--public-url-mode",
        choices=("loopback", "tunnel"),
        default="loopback",
        help="Expose task-provided local mocks directly or through an externally configured tunnel",
    )
    parser.add_argument(
        "--openharness-mode",
        choices=("original", "writer_harness"),
        default="original",
        help="Execute directly or require the external Writer handoff first",
    )
    parser.add_argument(
        "--writer-workspace-root",
        type=Path,
        help="Workspace root containing writer_harness/ and its original archive",
    )
    parser.add_argument("--writer-archive", type=Path)
    parser.add_argument("--writer-model")
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


def _run_parser(subparsers: Any) -> None:
    parser = subparsers.add_parser("run", help="Run selected HarnessBench tasks and repeats")
    _add_source_arguments(parser)
    parser.add_argument("--repeats", type=int, default=2)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument(
        "--resume",
        action="store_true",
        help="Skip valid task results already present in the same locked output directory",
    )


def _preflight_parser(subparsers: Any) -> None:
    parser = subparsers.add_parser(
        "preflight",
        help="Validate credentials, dependencies, task manifest, and local mock hooks",
    )
    _add_source_arguments(parser)


def _summarize_parser(subparsers: Any) -> None:
    parser = subparsers.add_parser(
        "summarize",
        help="Rebuild an incremental baseline summary without running any task",
    )
    parser.add_argument("--output-dir", type=Path, required=True)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)
    _adapter_parser(subparsers)
    _run_parser(subparsers)
    _preflight_parser(subparsers)
    _summarize_parser(subparsers)
    return parser


def _resolve(path: Path, base: Path) -> Path:
    expanded = path.expanduser()
    return (base / expanded).resolve() if not expanded.is_absolute() else expanded.resolve()


def _writer_deployment(
    args: argparse.Namespace,
    *,
    openharness_root: Path,
    invocation_dir: Path,
) -> dict[str, Any] | None:
    if args.openharness_mode != "writer_harness":
        return None
    from openharness.rehearsal.writer_handoff import (
        verify_writer_deployment,
    )

    workspace_root = (
        _resolve(args.writer_workspace_root, invocation_dir)
        if args.writer_workspace_root is not None
        else openharness_root.parent.resolve()
    )
    archive = (
        _resolve(args.writer_archive, invocation_dir)
        if args.writer_archive is not None
        else workspace_root / "docs" / "writer_director_0812.zip"
    )
    verification = verify_writer_deployment(
        workspace_root,
        archive,
        include_support_files=False,
    )
    if not verification.ok:
        raise ValueError(
            "deployed Writer does not exactly match the team archive: "
            f"missing={list(verification.missing_files)}, "
            f"mismatched={list(verification.mismatched_files)}"
        )
    args.writer_workspace_root = workspace_root
    args.writer_archive = archive
    result = dict(verification.to_dict())
    result["source_lock_enforced"] = True
    return result


def _run_adapter(args: argparse.Namespace) -> int:
    from openharness.rehearsal.harnessbench_runtime import (
        HarnessBenchRoundConfig,
        execute_harnessbench_round,
        public_result_payload,
    )

    if args.env_file is not None:
        env_file = _resolve(args.env_file, Path.cwd())
        if not env_file.is_file():
            print(f"env file not found: {env_file}", file=sys.stderr)
            return 20
        load_dotenv(env_file, override=True)
    if args.director_harness_enabled:
        project_root = (
            args.writer_workspace_root.resolve()
            if args.writer_workspace_root is not None
            else Path(__file__).resolve().parents[2]
        )
        if str(project_root) not in sys.path:
            sys.path.insert(0, str(project_root))

    config = HarnessBenchRoundConfig(
        workspace=args.workspace,
        sandbox=args.sandbox,
        prompt_file=args.prompt_file,
        session_id=args.session_id,
        task_id=args.task_id,
        model=args.model,
        active_profile=args.profile,
        api_format=args.api_format,
        temperature=args.temperature,
        seed=args.seed,
        max_turns=args.max_turns,
        api_timeout_sec=args.api_timeout_sec,
        permission_mode=args.permission_mode,
        openharness_mode=args.openharness_mode,
        writer_workspace_root=args.writer_workspace_root,
        writer_model=args.writer_model,
        writer_max_tokens=args.writer_max_tokens,
        director_harness_enabled=args.director_harness_enabled,
        director_mcp_catalog=args.director_mcp_catalog,
    )
    try:
        result = asyncio.run(execute_harnessbench_round(config))
    except Exception as exc:
        payload = {
            "status": "openharness_process_failed",
            "error_type": type(exc).__name__,
            "error": str(exc),
            "task_id": args.task_id,
            "session_id": args.session_id,
            "result_label": RESULT_LABEL,
        }
        print(json.dumps(payload, ensure_ascii=False))
        return 20
    print(json.dumps(public_result_payload(result), ensure_ascii=False))
    return 0


def _write_json(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def _read_json(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"invalid JSON file {path}: {exc}") from exc
    if not isinstance(value, dict):
        raise ValueError(f"JSON file must contain an object: {path}")
    return value


def _git_metadata(root: Path) -> dict[str, Any]:
    def run(*args: str) -> str:
        completed = subprocess.run(
            ["git", *args],
            cwd=root,
            text=True,
            capture_output=True,
            check=False,
        )
        return completed.stdout.strip()

    status = run("status", "--short", "--branch")
    return {
        "root": str(root.resolve()),
        "branch": run("branch", "--show-current"),
        "commit": run("rev-parse", "HEAD"),
        "status_short_branch": status.splitlines(),
        "dirty": len(status.splitlines()) > 1,
    }


def _file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _source_hashes(openharness_root: Path) -> dict[str, str]:
    relative_paths = (
        "scripts/run_openharness_harnessbench.py",
        "src/openharness/engine/query.py",
        "src/openharness/engine/query_engine.py",
        "src/openharness/engine/stream_events.py",
        "src/openharness/rehearsal/harnessbench_runtime.py",
        "src/openharness/rehearsal/writer_handoff.py",
        "src/openharness/ui/app.py",
        "src/openharness/ui/runtime.py",
    )
    return {
        relative: _file_sha256(openharness_root / relative)
        for relative in relative_paths
    }


def _harnessbench_source_hashes(harnessbench_root: Path) -> dict[str, str]:
    relative = "src/harnessbench/usage_proxy.py"
    return {relative: _file_sha256(harnessbench_root / relative)}


def _load_environment(env_file: Path) -> dict[str, str]:
    env = os.environ.copy()
    values = dotenv_values(env_file)
    for key, value in values.items():
        if value is not None:
            env[str(key)] = str(value)
    return env


def _configure_grading_environment(
    env: dict[str, str],
    args: argparse.Namespace,
) -> dict[str, Any]:
    if args.grading == "outcome-only":
        env["HARNESSBENCH_SKIP_PROCESS_GRADE"] = "1"
        env["HARNESSBENCH_SKIP_ORACLE_QUALITY_LLM"] = "1"
        return {
            "mode": "outcome-only",
            "process_grade": "skipped",
            "security_grade": "default-pass-unassessed",
            "oracle_quality_llm": "skipped",
        }

    env.pop("HARNESSBENCH_SKIP_PROCESS_GRADE", None)
    env.pop("HARNESSBENCH_SKIP_ORACLE_QUALITY_LLM", None)
    key_source = str(args.rubric_api_key_env).strip()
    key = env.get(key_source, "").strip()
    if not key:
        raise ValueError(
            f"--grading full requires a non-empty {key_source!r} in the environment or env file"
        )
    base_url = (
        str(args.rubric_base_url or "").strip()
        or env.get(str(args.rubric_base_url_env), "").strip()
        or env.get("RUBRIC_BASE_URL", "").strip()
    )
    if not base_url:
        raise ValueError(
            "--grading full requires --rubric-base-url or a populated "
            f"{args.rubric_base_url_env!r} environment variable"
        )
    rubric_model = str(args.rubric_model or args.model).strip()
    if not rubric_model:
        raise ValueError("--grading full requires --rubric-model or --model")
    vision_model = str(args.rubric_vision_model or rubric_model).strip()
    env["RUBRIC_API_KEY"] = key
    env["RUBRIC_BASE_URL"] = base_url.rstrip("/")
    env["RUBRIC_MODEL"] = rubric_model
    env["RUBRIC_VISION_MODEL"] = vision_model
    return {
        "mode": "full",
        "process_grade": "enabled",
        "security_grade": "enabled",
        "oracle_quality_llm": "enabled-for-weighted-tasks",
        "rubric_model": rubric_model,
        "rubric_vision_model": vision_model,
        "rubric_base_url": base_url.rstrip("/"),
        "rubric_api_key_source": key_source,
    }


def _load_task_manifest(path: Path, harnessbench_root: Path) -> tuple[dict[str, Any], tuple[str, ...]]:
    manifest = _read_json(path)
    raw_tasks = manifest.get("tasks")
    if not isinstance(raw_tasks, list) or not all(isinstance(item, str) for item in raw_tasks):
        raise ValueError(f"task manifest has no valid string task list: {path}")
    task_ids = tuple(str(item).strip() for item in raw_tasks if str(item).strip())
    if len(task_ids) != len(set(task_ids)):
        raise ValueError(f"task manifest contains duplicate task IDs: {path}")
    declared_count = manifest.get("task_count")
    if declared_count != len(task_ids):
        raise ValueError(
            f"task manifest task_count={declared_count!r} does not match {len(task_ids)} tasks"
        )
    return manifest, task_ids


def _resolve_selection(
    args: argparse.Namespace,
    harnessbench_root: Path,
    invocation_dir: Path,
) -> tuple[dict[str, Any], tuple[str, ...], Path | None]:
    if args.task_manifest is not None:
        manifest_path = _resolve(args.task_manifest, invocation_dir)
        manifest, task_ids = _load_task_manifest(manifest_path, harnessbench_root)
    else:
        manifest_path = None
        raw = args.tasks if args.tasks is not None else ",".join(DEFAULT_TASKS)
        task_ids = _resolve_task_ids(raw, harnessbench_root)
        manifest = {
            "schema_version": 1,
            "dataset_id": "ad-hoc",
            "task_count": len(task_ids),
            "tasks": list(task_ids),
            "excluded_tasks": [],
        }
    missing = [
        task_id
        for task_id in task_ids
        if not (harnessbench_root / "tasks" / task_id / "task.yaml").is_file()
    ]
    if missing:
        raise ValueError("unknown HarnessBench task IDs: " + ", ".join(missing))
    return manifest, task_ids, manifest_path


def _copy_task_fixtures(task_dir: Path, workspace: Path) -> None:
    workspace.mkdir(parents=True, exist_ok=True)
    (workspace / "in").mkdir(parents=True, exist_ok=True)
    (workspace / "out").mkdir(parents=True, exist_ok=True)
    fixtures = task_dir / "fixtures"
    if not fixtures.is_dir():
        return
    for child in fixtures.iterdir():
        destination = workspace / child.name
        if child.is_dir():
            shutil.copytree(child, destination, dirs_exist_ok=True)
        else:
            shutil.copy2(child, destination)


def _load_hook_module(task_id: str, hook_path: Path) -> Any:
    module_name = "openharness_harnessbench_preflight_" + task_id.replace("-", "_")
    spec = importlib.util.spec_from_file_location(module_name, hook_path)
    if spec is None or spec.loader is None:
        raise ValueError(f"cannot import HarnessBench hook: {hook_path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _probe_loopback_hooks(
    harnessbench_root: Path,
    task_ids: Sequence[str],
) -> list[dict[str, Any]]:
    selected = [task_id for task_id in LOOPBACK_MOCK_TASKS if task_id in task_ids]
    if not selected:
        return []
    previous_template = os.environ.get("HARNESSBENCH_PUBLIC_URL_TEMPLATE")
    os.environ["HARNESSBENCH_PUBLIC_URL_TEMPLATE"] = "{local_url}"
    outcomes: list[dict[str, Any]] = []
    try:
        for task_id in selected:
            task_dir = harnessbench_root / "tasks" / task_id
            hook = _load_hook_module(task_id, task_dir / "hooks.py")
            with tempfile.TemporaryDirectory(prefix=f"openharness-hb-{task_id}-") as raw_tmp:
                sandbox = Path(raw_tmp)
                workspace = sandbox / "workspace"
                _copy_task_fixtures(task_dir, workspace)
                state: dict[str, Any] = {}
                try:
                    prepared = hook.prepare_runtime(
                        {"task": None, "sandbox": sandbox, "workspace": workspace}
                    )
                    if not isinstance(prepared, dict):
                        raise ValueError(f"{task_id} prepare_runtime returned no state mapping")
                    state.update(prepared)
                    env_name, suffix = LOOPBACK_PROBES[task_id]
                    base = str(state.get(env_name) or "").rstrip("/")
                    if not base.startswith("http://127.0.0.1:"):
                        raise ValueError(f"{task_id} did not expose a loopback URL: {base!r}")
                    with urllib.request.urlopen(base + suffix, timeout=5) as response:
                        status = int(response.status)
                        response.read(256)
                    if status >= 400:
                        raise ValueError(f"{task_id} loopback probe returned HTTP {status}")
                    outcomes.append(
                        {
                            "task_id": task_id,
                            "url_env": env_name,
                            "probe_status": status,
                            "transport": "loopback",
                        }
                    )
                finally:
                    cleanup = getattr(hook, "cleanup_runtime", None)
                    if callable(cleanup):
                        cleanup(
                            {"task": None, "sandbox": sandbox, "workspace": workspace},
                            state,
                        )
    finally:
        if previous_template is None:
            os.environ.pop("HARNESSBENCH_PUBLIC_URL_TEMPLATE", None)
        else:
            os.environ["HARNESSBENCH_PUBLIC_URL_TEMPLATE"] = previous_template
    return outcomes


def _mark_results(
    results_root: Path,
    *,
    metadata: Mapping[str, Any] | None = None,
) -> list[dict[str, Any]]:
    summaries: list[dict[str, Any]] = []
    for result_file in sorted(results_root.rglob("*.json")):
        try:
            payload = json.loads(result_file.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            continue
        if not isinstance(payload, dict) or "task_id" not in payload:
            continue
        payload["result_label"] = RESULT_LABEL
        if metadata:
            payload.update(metadata)
        result_file.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        adapter = payload.get("adapter_result")
        adapter_rounds = payload.get("adapter_results")
        oracle = payload.get("oracle_result")
        agent_statuses: list[str] = []
        if isinstance(adapter_rounds, list):
            for adapter_round in adapter_rounds:
                if not isinstance(adapter_round, dict):
                    continue
                stdout = str(adapter_round.get("stdout") or "").strip()
                try:
                    round_payload = json.loads(stdout)
                except json.JSONDecodeError:
                    continue
                if isinstance(round_payload, dict) and round_payload.get("status"):
                    agent_statuses.append(str(round_payload["status"]))
        summaries.append(
            {
                "task_id": payload.get("task_id"),
                "adapter_ok": adapter.get("ok") if isinstance(adapter, dict) else None,
                "agent_statuses": agent_statuses,
                "oracle_error": oracle.get("error") if isinstance(oracle, dict) else None,
                "outcome_score": oracle.get("outcome_score") if isinstance(oracle, dict) else None,
                "result_file": str(result_file),
            }
        )
    return summaries


def _resolve_task_ids(raw: str, harnessbench_root: Path) -> tuple[str, ...]:
    if raw.strip().lower() == "all":
        task_ids = tuple(
            path.name
            for path in sorted((harnessbench_root / "tasks").iterdir())
            if path.is_dir() and (path / "task.yaml").is_file()
        )
    else:
        task_ids = tuple(item.strip() for item in raw.split(",") if item.strip())
    if not task_ids:
        raise ValueError("--tasks must contain at least one task ID")
    missing = [
        task_id
        for task_id in task_ids
        if not (harnessbench_root / "tasks" / task_id / "task.yaml").is_file()
    ]
    if missing:
        raise ValueError("unknown HarnessBench task IDs: " + ", ".join(missing))
    return task_ids


def _find_result_file(results_root: Path, task_id: str) -> Path | None:
    matches = sorted(results_root.rglob(f"{task_id}.json")) if results_root.is_dir() else []
    valid: list[Path] = []
    for path in matches:
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if isinstance(payload, dict) and payload.get("task_id") == task_id:
            valid.append(path)
    if len(valid) > 1:
        raise ValueError(
            f"multiple result files found for {task_id} under {results_root}: "
            + ", ".join(str(path) for path in valid)
        )
    return valid[0] if valid else None


def _adapter_round_payloads(
    payload: Mapping[str, Any],
) -> list[dict[str, Any]]:
    values: list[dict[str, Any]] = []
    rounds = payload.get("adapter_results")
    if not isinstance(rounds, list):
        return values
    for adapter_round in rounds:
        if not isinstance(adapter_round, dict):
            continue
        stdout = str(adapter_round.get("stdout") or "").strip()
        try:
            round_payload = json.loads(stdout)
        except json.JSONDecodeError:
            continue
        if isinstance(round_payload, dict):
            values.append(round_payload)
    return values


def _adapter_statuses(payload: Mapping[str, Any]) -> list[str]:
    return [
        str(value["status"])
        for value in _adapter_round_payloads(payload)
        if value.get("status")
    ]


def _writer_gate(payload: Mapping[str, Any]) -> dict[str, Any]:
    rounds = _adapter_round_payloads(payload)
    writer_rounds = [value for value in rounds if value.get("writer_required")]
    mandatory = sum(
        value.get("writer_mandatory_passed") is True for value in writer_rounds
    )
    isolated = sum(
        value.get("writer_state_unchanged") is True for value in writer_rounds
    )
    events_complete = sum(
        isinstance(value.get("writer_event_validation"), Mapping)
        and value["writer_event_validation"].get("events_complete") is True
        for value in writer_rounds
    )
    return {
        "adapter_rounds": len(rounds),
        "writer_rounds": len(writer_rounds),
        "mandatory_passed": mandatory,
        "planning_state_isolated": isolated,
        "events_complete": events_complete,
        "passed": (
            bool(rounds)
            and len(writer_rounds) == len(rounds)
            and mandatory == len(rounds)
            and isolated == len(rounds)
            and events_complete == len(rounds)
        ),
    }


def _director_gate(payload: Mapping[str, Any]) -> dict[str, Any]:
    rounds = _adapter_round_payloads(payload)
    director_rounds = [value for value in rounds if value.get("director_enabled")]
    checked = sum(
        isinstance(value.get("director_event_validation"), Mapping)
        and value["director_event_validation"].get("all_tool_calls_checked") is True
        for value in director_rounds
    )
    ordered = sum(
        isinstance(value.get("director_event_validation"), Mapping)
        and value["director_event_validation"].get(
            "director_before_tool_completion"
        )
        is True
        for value in director_rounds
    )
    return {
        "adapter_rounds": len(rounds),
        "director_rounds": len(director_rounds),
        "all_tool_calls_checked": checked,
        "preflight_order_passed": ordered,
        "passed": (
            bool(rounds)
            and len(director_rounds) == len(rounds)
            and checked == len(rounds)
            and ordered == len(rounds)
        ),
    }


def _classify_result(payload: Mapping[str, Any]) -> str:
    oracle = payload.get("oracle_result")
    adapter = payload.get("adapter_result")
    if isinstance(oracle, dict) and oracle.get("error"):
        return "oracle_failed"
    if isinstance(adapter, dict) and adapter.get("ok") is False:
        return "openharness_process_failed"
    if "agent_task_failed" in _adapter_statuses(payload):
        return "agent_task_failed"
    return "completed"


def _full_grading_complete(payload: Mapping[str, Any]) -> bool:
    scoring = payload.get("scoring")
    oracle = payload.get("oracle_result")
    if not isinstance(scoring, dict) or not isinstance(oracle, dict):
        return False
    rubric = scoring.get("rubric")
    if (
        not isinstance(rubric, dict)
        or rubric.get("skipped")
        or rubric.get("parse_error")
        or scoring.get("process_score") is None
        or scoring.get("security_score") is None
    ):
        return False
    weight = oracle.get("outcome_llm_weight")
    if isinstance(weight, (int, float)) and float(weight) > 0:
        if not isinstance(oracle.get("quality"), (int, float)):
            return False
    return isinstance(scoring.get("combined_score"), (int, float))


def _summary_for_output(output_dir: Path, run_config: Mapping[str, Any]) -> dict[str, Any]:
    task_ids = tuple(str(item) for item in run_config.get("tasks", []))
    repeats = int(run_config.get("repeats", 0))
    grading = str(run_config.get("grading", {}).get("mode", "outcome-only"))
    repeat_summaries: list[dict[str, Any]] = []
    state_counts: dict[str, int] = {}
    total_expected = len(task_ids) * repeats
    full_grading_missing: list[str] = []
    writer_required = run_config.get("openharness_mode") == "writer_harness"
    director_required = run_config.get("director_harness_enabled") is True
    writer_expected_rounds = 0
    writer_rounds = 0
    writer_mandatory = 0
    writer_isolated = 0
    writer_events_complete = 0
    director_expected_rounds = 0
    director_rounds = 0
    director_checked = 0
    director_ordered = 0

    for repeat in range(1, repeats + 1):
        repeat_dir = output_dir / f"repeat-{repeat:02d}"
        task_rows: list[dict[str, Any]] = []
        outcome_scores: list[float] = []
        combined_scores: list[float] = []
        for task_id in task_ids:
            row: dict[str, Any]
            result_file = _find_result_file(repeat_dir / "results", task_id)
            if result_file is None:
                log_file = repeat_dir / "logs" / f"{task_id}.log"
                state = "harness_process_failed" if log_file.is_file() else "pending"
                row = {
                    "task_id": task_id,
                    "state": state,
                    "result_file": None,
                    "log_file": str(log_file) if log_file.is_file() else None,
                }
            else:
                payload = _read_json(result_file)
                state = _classify_result(payload)
                oracle = payload.get("oracle_result")
                scoring = payload.get("scoring")
                outcome = oracle.get("outcome_score") if isinstance(oracle, dict) else None
                combined = scoring.get("combined_score") if isinstance(scoring, dict) else None
                if isinstance(outcome, (int, float)):
                    outcome_scores.append(float(outcome))
                if isinstance(combined, (int, float)):
                    combined_scores.append(float(combined))
                if grading == "full" and not _full_grading_complete(payload):
                    full_grading_missing.append(f"repeat-{repeat:02d}/{task_id}")
                writer_gate = _writer_gate(payload)
                director_gate = _director_gate(payload)
                if writer_required:
                    writer_expected_rounds += int(
                        writer_gate["adapter_rounds"]
                    )
                    writer_rounds += int(writer_gate["writer_rounds"])
                    writer_mandatory += int(
                        writer_gate["mandatory_passed"]
                    )
                    writer_isolated += int(
                        writer_gate["planning_state_isolated"]
                    )
                    writer_events_complete += int(
                        writer_gate["events_complete"]
                    )
                if director_required:
                    director_expected_rounds += int(
                        director_gate["adapter_rounds"]
                    )
                    director_rounds += int(director_gate["director_rounds"])
                    director_checked += int(
                        director_gate["all_tool_calls_checked"]
                    )
                    director_ordered += int(
                        director_gate["preflight_order_passed"]
                    )
                row = {
                    "task_id": task_id,
                    "state": state,
                    "outcome_score": outcome,
                    "combined_score": combined,
                    "agent_statuses": _adapter_statuses(payload),
                    "writer_gate": writer_gate if writer_required else None,
                    "director_gate": (
                        director_gate if director_required else None
                    ),
                    "result_file": str(result_file),
                }
            state_counts[state] = state_counts.get(state, 0) + 1
            task_rows.append(row)
        repeat_summaries.append(
            {
                "repeat": repeat,
                "expected_tasks": len(task_ids),
                "completed_results": sum(row["result_file"] is not None for row in task_rows),
                "outcome_mean": round(mean(outcome_scores), 6) if outcome_scores else None,
                "outcome_median": round(median(outcome_scores), 6) if outcome_scores else None,
                "combined_mean": round(mean(combined_scores), 6) if combined_scores else None,
                "combined_median": round(median(combined_scores), 6) if combined_scores else None,
                "tasks": task_rows,
            }
        )

    result_count = sum(
        int(repeat_summary["completed_results"]) for repeat_summary in repeat_summaries
    )
    failure_count = sum(
        count
        for state, count in state_counts.items()
        if state not in {"completed", "pending"}
    )
    full_complete = grading != "full" or not full_grading_missing
    writer_gate_passed = (
        writer_required
        and result_count == total_expected
        and writer_expected_rounds > 0
        and writer_rounds == writer_expected_rounds
        and writer_mandatory == writer_expected_rounds
        and writer_isolated == writer_expected_rounds
        and writer_events_complete == writer_expected_rounds
    )
    director_gate_passed = (
        director_required
        and result_count == total_expected
        and director_expected_rounds > 0
        and director_rounds == director_expected_rounds
        and director_checked == director_expected_rounds
        and director_ordered == director_expected_rounds
    )
    baseline_ready = (
        total_expected > 0
        and result_count == total_expected
        and failure_count == 0
        and full_complete
        and (not writer_required or writer_gate_passed)
        and (not director_required or director_gate_passed)
    )
    return {
        "schema_version": 1,
        "result_label": RESULT_LABEL,
        "dataset_id": run_config.get("dataset_id"),
        "openharness_mode": run_config.get("openharness_mode"),
        "grading_mode": grading,
        "expected_results": total_expected,
        "result_count": result_count,
        "state_counts": dict(sorted(state_counts.items())),
        "full_grading_complete": full_complete,
        "full_grading_missing": full_grading_missing,
        "writer_smoke_gate": {
            "required": writer_required,
            "expected_rounds": writer_expected_rounds,
            "writer_rounds": writer_rounds,
            "mandatory_passed": writer_mandatory,
            "planning_state_isolated": writer_isolated,
            "events_complete": writer_events_complete,
            "passed": writer_gate_passed,
        },
        "director_smoke_gate": {
            "required": director_required,
            "expected_rounds": director_expected_rounds,
            "director_rounds": director_rounds,
            "all_tool_calls_checked": director_checked,
            "preflight_order_passed": director_ordered,
            "passed": director_gate_passed,
        },
        "baseline_ready": baseline_ready,
        "repeats": repeat_summaries,
        "updated_at": datetime.now(timezone.utc).isoformat(),
    }


def _write_incremental_summary(output_dir: Path, run_config: Mapping[str, Any]) -> dict[str, Any]:
    summary = _summary_for_output(output_dir, run_config)
    _write_json(output_dir / "baseline-summary.json", summary)
    return summary


def _build_run_config(
    *,
    args: argparse.Namespace,
    manifest: Mapping[str, Any],
    manifest_path: Path | None,
    task_ids: Sequence[str],
    harnessbench_root: Path,
    openharness_root: Path,
    grading: Mapping[str, Any],
    writer_deployment: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    return {
        "schema_version": 1,
        "result_label": RESULT_LABEL,
        "dataset_id": manifest.get("dataset_id", "ad-hoc"),
        "dataset_manifest": str(manifest_path) if manifest_path else None,
        "tasks": list(task_ids),
        "excluded_tasks": list(manifest.get("excluded_tasks", [])),
        "repeats": args.repeats,
        "model": args.model,
        "profile": args.profile,
        "api_format": args.api_format,
        "temperature": args.temperature,
        "seed": args.seed,
        "max_turns": args.max_turns,
        "api_timeout_sec": args.api_timeout_sec,
        "openharness_mode": args.openharness_mode,
        "writer_model": (
            args.writer_model or args.model
            if args.openharness_mode == "writer_harness"
            else None
        ),
        "writer_max_tokens": (
            args.writer_max_tokens
            if args.openharness_mode == "writer_harness"
            else None
        ),
        "writer_deployment": (
            dict(writer_deployment)
            if writer_deployment is not None
            else None
        ),
        "director_harness_enabled": bool(args.director_harness_enabled),
        "director_mcp_catalog": (
            str(args.director_mcp_catalog.resolve())
            if args.director_harness_enabled
            and args.director_mcp_catalog is not None
            else None
        ),
        "public_url_mode": args.public_url_mode,
        "grading": dict(grading),
        "harnessbench_git": _git_metadata(harnessbench_root),
        "harnessbench_source_sha256": _harnessbench_source_hashes(harnessbench_root),
        "openharness_git": _git_metadata(openharness_root),
        "openharness_source_sha256": _source_hashes(openharness_root),
        "source_lock_enforced": False,
    }


def _prepare_output_dir(
    output_dir: Path,
    requested_config: dict[str, Any],
    *,
    resume: bool,
) -> dict[str, Any]:
    config_path = output_dir / "run-config.json"
    if config_path.is_file():
        existing = _read_json(config_path)
        comparable_keys = (
            "dataset_id",
            "tasks",
            "excluded_tasks",
            "repeats",
            "model",
            "profile",
            "api_format",
            "temperature",
            "seed",
            "max_turns",
            "api_timeout_sec",
            "openharness_mode",
            "writer_model",
            "writer_max_tokens",
            "director_harness_enabled",
            "director_mcp_catalog",
            "public_url_mode",
            "grading",
        )
        mismatches = [
            key for key in comparable_keys if existing.get(key) != requested_config.get(key)
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


def _run_suite(args: argparse.Namespace) -> int:
    if args.repeats < 1:
        raise ValueError("--repeats must be at least 1")
    invocation_dir = Path.cwd()
    openharness_root = Path(__file__).resolve().parents[1]
    harnessbench_root = _resolve(args.harnessbench_root, invocation_dir)
    env_file = _resolve(args.env_file, invocation_dir)
    output_dir = _resolve(args.output_dir, invocation_dir)
    if not (harnessbench_root / "src/harnessbench/cli.py").is_file():
        raise ValueError(f"invalid HarnessBench root: {harnessbench_root}")
    if not env_file.is_file():
        raise ValueError(f"env file not found: {env_file}")
    writer_deployment = _writer_deployment(
        args,
        openharness_root=openharness_root,
        invocation_dir=invocation_dir,
    )
    director_catalog: Path | None = None
    if args.director_harness_enabled:
        if args.director_mcp_catalog is None:
            raise ValueError(
                "--director-mcp-catalog is required when Director is enabled"
            )
        director_catalog = _resolve(args.director_mcp_catalog, invocation_dir)
        if not director_catalog.is_file():
            raise ValueError(f"Director MCP catalog not found: {director_catalog}")
        args.director_mcp_catalog = director_catalog
    manifest, task_ids, manifest_path = _resolve_selection(
        args,
        harnessbench_root,
        invocation_dir,
    )
    env = _load_environment(env_file)
    grading = _configure_grading_environment(env, args)
    if args.public_url_mode == "loopback":
        env["HARNESSBENCH_PUBLIC_URL_TEMPLATE"] = "{local_url}"
        _probe_loopback_hooks(harnessbench_root, task_ids)

    requested_config = _build_run_config(
        args=args,
        manifest=manifest,
        manifest_path=manifest_path,
        task_ids=task_ids,
        harnessbench_root=harnessbench_root,
        openharness_root=openharness_root,
        grading=grading,
        writer_deployment=writer_deployment,
    )
    run_config = _prepare_output_dir(
        output_dir,
        requested_config,
        resume=bool(args.resume),
    )
    result_metadata = {
        "dataset_id": run_config["dataset_id"],
        "openharness_mode": run_config["openharness_mode"],
        "director_harness_enabled": run_config["director_harness_enabled"],
        "grading_mode": run_config["grading"]["mode"],
    }
    harness_config = output_dir / "harness.local.json"
    wrapper = Path(__file__).resolve()
    python = openharness_root / ".venv/bin/python"
    if not python.is_file():
        raise ValueError(f"OpenHarness virtualenv Python not found: {python}")
    adapter_args = [
        str(wrapper),
        "adapter",
        "--workspace",
        "{workspace}",
        "--sandbox",
        "{sandbox}",
        "--prompt-file",
        "{prompt_file}",
        "--session-id",
        "{session_id}",
        "--task-id",
        "{task_id}",
        "--model",
        args.model,
        "--profile",
        args.profile,
        "--api-format",
        args.api_format,
        "--temperature",
        str(args.temperature),
        "--seed",
        str(args.seed),
        "--max-turns",
        str(args.max_turns),
        "--api-timeout-sec",
        str(args.api_timeout_sec),
        "--env-file",
        str(env_file),
        "--openharness-mode",
        args.openharness_mode,
    ]
    if args.openharness_mode == "writer_harness":
        adapter_args.extend(
            [
                "--writer-workspace-root",
                str(args.writer_workspace_root),
                "--writer-model",
                str(args.writer_model or args.model),
                "--writer-max-tokens",
                str(args.writer_max_tokens),
            ]
        )
    if args.director_harness_enabled:
        adapter_args.extend(
            [
                "--director-harness-enabled",
                "--director-mcp-catalog",
                str(args.director_mcp_catalog),
            ]
        )
    _write_json(
        harness_config,
        {
            "models": {
                "openharness-local": {
                    "adapter": "generic_cli",
                    "command": str(python),
                    "session_prefix": "harnessbench-openharness",
                    "model": args.model,
                    "timeout_sec": 2400,
                    "args": adapter_args,
                }
            }
        },
    )

    state_path = output_dir / "run-state.json"
    state = _read_json(state_path) if state_path.is_file() else {"task_runs": []}
    task_runs = state.get("task_runs")
    if not isinstance(task_runs, list):
        task_runs = []
    exit_code = 0
    for repeat in range(1, args.repeats + 1):
        repeat_dir = output_dir / f"repeat-{repeat:02d}"
        app_config = repeat_dir / "app.local.json"
        _write_json(
            app_config,
            {
                "data_dir": str(repeat_dir / "data"),
                "tasks_dir": str(harnessbench_root / "tasks"),
                "results_dir": str(repeat_dir / "results"),
                "work_root": str(repeat_dir / "sandboxes"),
                "default_timeout_sec": 2400,
            },
        )
        repeat_env = env.copy()
        repeat_env.update(
            {
                "PYTHONPATH": str(harnessbench_root / "src"),
                "HARNESSBENCH_APP_CONFIG": str(app_config),
                "HARNESSBENCH_HARNESS_CONFIG": str(harness_config),
            }
        )
        for task_id in task_ids:
            existing_result = _find_result_file(repeat_dir / "results", task_id)
            if existing_result is not None:
                if not args.resume:
                    raise ValueError(
                        f"result already exists without --resume: {existing_result}"
                    )
                print(
                    f"[openharness-harnessbench] repeat {repeat}/{args.repeats} "
                    f"skipping completed {task_id}",
                    flush=True,
                )
                continue
            print(
                f"[openharness-harnessbench] repeat {repeat}/{args.repeats} "
                f"starting {task_id}",
                flush=True,
            )
            command = [
                sys.executable,
                "-m",
                "harnessbench.cli",
                "run-task",
                "--task",
                task_id,
                "--harness",
                "openharness-local",
                "--mode",
                "live",
            ]
            task_started = time.perf_counter()
            try:
                completed = subprocess.run(
                    command,
                    cwd=harnessbench_root,
                    env=repeat_env,
                    text=True,
                    capture_output=True,
                    check=False,
                )
            except KeyboardInterrupt:
                _mark_results(repeat_dir / "results", metadata=result_metadata)
                state["task_runs"] = task_runs
                state["interrupted_at"] = {
                    "repeat": repeat,
                    "task_id": task_id,
                    "timestamp": datetime.now(timezone.utc).isoformat(),
                }
                _write_json(state_path, state)
                _write_incremental_summary(output_dir, run_config)
                print(
                    f"[openharness-harnessbench] interrupted while running {task_id}",
                    file=sys.stderr,
                    flush=True,
                )
                return 130
            elapsed = round(time.perf_counter() - task_started, 1)
            log_file = repeat_dir / "logs" / f"{task_id}.log"
            log_file.parent.mkdir(parents=True, exist_ok=True)
            log_file.write_text(
                completed.stdout + ("\n[stderr]\n" + completed.stderr if completed.stderr else ""),
                encoding="utf-8",
            )
            task_runs.append(
                {
                    "repeat": repeat,
                    "task_id": task_id,
                    "returncode": completed.returncode,
                    "log_file": str(log_file),
                }
            )
            state["task_runs"] = task_runs
            state["last_finished"] = {
                "repeat": repeat,
                "task_id": task_id,
                "timestamp": datetime.now(timezone.utc).isoformat(),
            }
            _mark_results(repeat_dir / "results", metadata=result_metadata)
            _write_json(state_path, state)
            _write_incremental_summary(output_dir, run_config)
            print(
                f"[openharness-harnessbench] repeat {repeat}/{args.repeats} "
                f"finished {task_id} rc={completed.returncode} elapsed={elapsed}s "
                f"log={log_file}",
                flush=True,
            )
            if completed.returncode != 0:
                exit_code = 1
        _mark_results(repeat_dir / "results", metadata=result_metadata)

    state["task_runs"] = task_runs
    state["finished_at"] = datetime.now(timezone.utc).isoformat()
    _write_json(state_path, state)
    summary = _write_incremental_summary(output_dir, run_config)
    if not summary["baseline_ready"]:
        exit_code = 1
    print(
        json.dumps(
            {
                "result_label": RESULT_LABEL,
                "output_dir": str(output_dir),
                "baseline_ready": summary["baseline_ready"],
                "result_count": summary["result_count"],
                "expected_results": summary["expected_results"],
            },
            ensure_ascii=False,
        )
    )
    return exit_code


def _run_preflight(args: argparse.Namespace) -> int:
    invocation_dir = Path.cwd()
    openharness_root = Path(__file__).resolve().parents[1]
    harnessbench_root = _resolve(args.harnessbench_root, invocation_dir)
    env_file = _resolve(args.env_file, invocation_dir)
    if not (harnessbench_root / "src/harnessbench/cli.py").is_file():
        raise ValueError(f"invalid HarnessBench root: {harnessbench_root}")
    if not env_file.is_file():
        raise ValueError(f"env file not found: {env_file}")
    writer_deployment = _writer_deployment(
        args,
        openharness_root=openharness_root,
        invocation_dir=invocation_dir,
    )
    director_catalog: Path | None = None
    if args.director_harness_enabled:
        if args.director_mcp_catalog is None:
            raise ValueError(
                "--director-mcp-catalog is required when Director is enabled"
            )
        director_catalog = _resolve(args.director_mcp_catalog, invocation_dir)
        if not director_catalog.is_file():
            raise ValueError(f"Director MCP catalog not found: {director_catalog}")
        args.director_mcp_catalog = director_catalog
    manifest, task_ids, manifest_path = _resolve_selection(
        args,
        harnessbench_root,
        invocation_dir,
    )
    env = _load_environment(env_file)
    grading = _configure_grading_environment(env, args)
    if args.public_url_mode == "loopback":
        env["HARNESSBENCH_PUBLIC_URL_TEMPLATE"] = "{local_url}"
        hook_probes = _probe_loopback_hooks(harnessbench_root, task_ids)
    else:
        hook_probes = []
        if any(task_id in task_ids for task_id in LOOPBACK_MOCK_TASKS):
            if not (
                env.get("HARNESSBENCH_PUBLIC_URL_TEMPLATE")
                or env.get("HARNESSBENCH_TUNNEL_CMD")
                or shutil.which("cloudflared")
            ):
                raise ValueError(
                    "--public-url-mode tunnel requires cloudflared, "
                    "HARNESSBENCH_PUBLIC_URL_TEMPLATE, or HARNESSBENCH_TUNNEL_CMD"
                )

    previous: dict[str, str | None] = {key: os.environ.get(key) for key in env}
    os.environ.update(env)
    try:
        from openharness.config import load_settings

        settings = load_settings().merge_cli_overrides(
            model=args.model,
            active_profile=args.profile,
            api_format=args.api_format,
        )
        auth = settings.resolve_auth()
        if not auth.value:
            raise ValueError(
                f"OpenHarness profile {args.profile!r} has no configured authentication"
            )
        profile_summary = {
            "profile": args.profile,
            "provider": settings.provider,
            "api_format": settings.api_format,
            "model": settings.model,
            "base_url": settings.base_url,
            "auth_configured": True,
            "auth_source": auth.source,
        }
    finally:
        for key, old_value in previous.items():
            if old_value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = old_value

    payload = {
        "ok": True,
        "result_label": RESULT_LABEL,
        "dataset_id": manifest.get("dataset_id"),
        "task_manifest": str(manifest_path) if manifest_path else None,
        "task_count": len(task_ids),
        "excluded_tasks": manifest.get("excluded_tasks", []),
        "openharness_mode": args.openharness_mode,
        "writer_model": (
            args.writer_model or args.model
            if args.openharness_mode == "writer_harness"
            else None
        ),
        "writer_deployment": writer_deployment,
        "director_harness_enabled": bool(args.director_harness_enabled),
        "director_mcp_catalog": (
            str(director_catalog) if director_catalog is not None else None
        ),
        "public_url_mode": args.public_url_mode,
        "grading": grading,
        "profile": profile_summary,
        "loopback_hook_probes": hook_probes,
        "harnessbench_git": _git_metadata(harnessbench_root),
        "openharness_git": _git_metadata(openharness_root),
    }
    print(json.dumps(payload, ensure_ascii=False, indent=2))
    return 0


def _run_summarize(args: argparse.Namespace) -> int:
    output_dir = _resolve(args.output_dir, Path.cwd())
    config_path = output_dir / "run-config.json"
    if not config_path.is_file():
        raise ValueError(f"run-config.json not found: {config_path}")
    summary = _write_incremental_summary(output_dir, _read_json(config_path))
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return 0 if summary["baseline_ready"] else 1


def main(argv: Sequence[str] | None = None) -> int:
    try:
        args = build_parser().parse_args(argv)
        if args.command == "adapter":
            return _run_adapter(args)
        if args.command == "preflight":
            return _run_preflight(args)
        if args.command == "summarize":
            return _run_summarize(args)
        return _run_suite(args)
    except KeyboardInterrupt:
        print("Interrupted.", file=sys.stderr)
        return 130
    except (OSError, ValueError) as exc:
        print(f"{type(exc).__name__}: {exc}", file=sys.stderr)
        return 20


if __name__ == "__main__":
    raise SystemExit(main())
