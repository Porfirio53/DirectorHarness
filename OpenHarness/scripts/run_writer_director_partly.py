#!/usr/bin/env python3
"""Prepare and run the fixed Writer v1 + Director optimization subset.

The launcher deliberately keeps the benchmark runners unchanged.  It creates a
new timestamped result root, stores a filtered copy of the two full-run arms for
comparison, and then delegates execution to the existing MCP-Persona and
HarnessBench runners.  ``--resume`` reuses the same root and lets those runners
skip valid completed slots.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shlex
import shutil
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence
from zipfile import ZIP_DEFLATED, BadZipFile, ZipFile
from zoneinfo import ZoneInfo


SCRIPT_DIR = Path(__file__).resolve().parent
WORKSPACE_DEFAULT = SCRIPT_DIR.parents[1]
SELECTION_FILE = SCRIPT_DIR / "writer_director_partly_selection.json"
HARNESSBENCH_MANIFEST = SCRIPT_DIR / "writer_director_partly_harnessbench.tasks.json"
EXPECTED_BRANCH = "writer-director-optimazation"
DEFAULT_MODEL = "qwen3.6-plus"
RUN_DIR_NAME = "writer-director-partly"
RUN_ID_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]*$")
RUN_TIMEZONE = ZoneInfo("Asia/Hong_Kong")

WRITER_CORE_FILES = (
    "writer_harness/__init__.py",
    "writer_harness/__main__.py",
    "writer_harness/actor_harness.py",
    "writer_harness/capability_matching.py",
    "writer_harness/cli.py",
    "writer_harness/llm_clients.py",
    "writer_harness/models.py",
    "writer_harness/orchestrator.py",
    "writer_harness/prompts.py",
    "writer_harness/writer_harness.py",
    "writer_harness/test_capability_matching.py",
    "writer_excute.py",
    "writer_excute_multiturn.py",
)
DIRECTOR_FILES = (
    "director_harness/__init__.py",
    "director_harness/catalog.py",
    "director_harness/director_mcp_catalog.json",
    "director_harness/events.py",
    "director_harness/harness.py",
    "director_harness/models.py",
)
ARCHIVE_WRITER_PREFIX = "writer_director_0812/writer_harness_demo/"
ARCHIVE_DIRECTOR_PREFIX = "writer_director_0812/writer_harness_demo/"

MCP_REFERENCE_METADATA = (
    "run-config.json",
    "summary.json",
    "baseline-summary.json",
    "baseline1-summary.json",
    "baseline2-summary.json",
    "baseline2-step-scores.jsonl",
    "semantic-reviews.jsonl",
    "semantic-run-config.json",
    "semantic-summary.json",
    "static-audit.csv",
    "static-audit.json",
    "version-manifest.json",
    "official-evaluator-probe.json",
    "timeout-recovery-report.json",
    "infrastructure-failures.jsonl",
)
HB_REFERENCE_METADATA = (
    "run-config.json",
    "baseline-summary.json",
    "harness.local.json",
    "run-state.json",
)


def _read_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def _write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.tmp")
    temporary.write_text(
        json.dumps(value, ensure_ascii=False, indent=2, default=str) + "\n",
        encoding="utf-8",
    )
    temporary.replace(path)


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _copy(source: Path, destination: Path, copied: list[str], base: Path) -> None:
    if not source.is_file():
        return
    destination.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(source, destination)
    copied.append(str(destination.relative_to(base)))


def _copy_tree(source: Path, destination: Path, copied: list[str], base: Path) -> None:
    if not source.is_dir():
        return
    for item in sorted(source.rglob("*")):
        if item.is_file():
            _copy(item, destination / item.relative_to(source), copied, base)


def _filter_jsonl(source: Path, destination: Path, task_ids: set[int], copied: list[str], base: Path) -> int:
    if not source.is_file():
        return 0
    destination.parent.mkdir(parents=True, exist_ok=True)
    count = 0
    with source.open(encoding="utf-8") as source_stream, destination.open("w", encoding="utf-8") as output:
        for line in source_stream:
            if not line.strip():
                continue
            try:
                value = json.loads(line)
            except json.JSONDecodeError:
                continue
            try:
                task_id = int(value.get("task_id"))
            except (AttributeError, TypeError, ValueError):
                continue
            if task_id not in task_ids:
                continue
            output.write(json.dumps(value, ensure_ascii=False, default=str) + "\n")
            count += 1
    copied.append(str(destination.relative_to(base)))
    return count


def _filter_failures(source: Path, destination: Path, task_ids: set[int], copied: list[str], base: Path) -> int:
    if not source.is_file():
        return 0
    return _filter_jsonl(source, destination, task_ids, copied, base)


def _task_result_matches(path: Path, task_ids: set[str]) -> bool:
    try:
        value = _read_json(path)
    except (OSError, json.JSONDecodeError):
        return False
    return isinstance(value, Mapping) and str(value.get("task_id")) in task_ids


def _copy_harnessbench_arm(source: Path, destination: Path, task_ids: set[str], copied: list[str], base: Path) -> dict[str, int]:
    counts: dict[str, int] = {}
    for metadata_name in HB_REFERENCE_METADATA:
        _copy(source / metadata_name, destination / "source-metadata" / metadata_name, copied, base)
    for repeat_dir in sorted(source.glob("repeat-*")):
        if not repeat_dir.is_dir():
            continue
        repeat_name = repeat_dir.name
        result_root = repeat_dir / "results"
        result_count = 0
        if result_root.is_dir():
            for path in sorted(result_root.rglob("*.json")):
                if _task_result_matches(path, task_ids):
                    _copy(path, destination / repeat_name / "results" / path.relative_to(result_root), copied, base)
                    result_count += 1
        log_count = 0
        for task_id in sorted(task_ids):
            log = repeat_dir / "logs" / f"{task_id}.log"
            if log.is_file():
                _copy(log, destination / repeat_name / "logs" / log.name, copied, base)
                log_count += 1
        counts[repeat_name] = result_count
        if log_count:
            counts[f"{repeat_name}_logs"] = log_count
    return counts


def _copy_mcp_arm(source: Path, destination: Path, task_ids: set[int], copied: list[str], base: Path) -> dict[str, int]:
    counts: dict[str, int] = {}
    for metadata_name in MCP_REFERENCE_METADATA:
        source_file = source / metadata_name
        if metadata_name in {"results.jsonl", "semantic-reviews.jsonl", "baseline2-step-scores.jsonl"}:
            continue
        _copy(source_file, destination / "source-metadata" / metadata_name, copied, base)
    counts["results"] = _filter_jsonl(source / "results.jsonl", destination / "results.jsonl", task_ids, copied, base)
    counts["semantic_reviews"] = _filter_jsonl(source / "semantic-reviews.jsonl", destination / "semantic-reviews.jsonl", task_ids, copied, base)
    counts["baseline2_step_scores"] = _filter_jsonl(source / "baseline2-step-scores.jsonl", destination / "baseline2-step-scores.jsonl", task_ids, copied, base)
    counts["rehearsal_step_scores"] = _filter_jsonl(source / "rehearsal-step-scores.jsonl", destination / "rehearsal-step-scores.jsonl", task_ids, copied, base)
    counts["infrastructure_failures"] = _filter_failures(source / "infrastructure-failures.jsonl", destination / "infrastructure-failures.jsonl", task_ids, copied, base)
    artifact_count = 0
    for task_id in sorted(task_ids):
        artifact_source = source / "artifacts" / f"task-{task_id}"
        if artifact_source.is_dir():
            _copy_tree(artifact_source, destination / "artifacts" / artifact_source.name, copied, base)
            artifact_count += 1
    counts["artifacts"] = artifact_count
    return counts


def _verify_archive(workspace: Path) -> dict[str, Any]:
    archive = workspace / "docs" / "writer_director_0812.zip"
    missing_deployed: list[str] = []
    missing_archive: list[str] = []
    mismatched: list[str] = []
    archive_error: str | None = None
    if not archive.is_file():
        return {
            "ok": False,
            "archive": str(archive),
            "missing": [str(archive)],
            "missing_deployed": [],
            "missing_archive": [],
            "mismatched": [],
            "archive_match": False,
            "archive_error": "archive is not a file",
        }
    try:
        with ZipFile(archive) as source:
            names = set(source.namelist())
            for relative in WRITER_CORE_FILES:
                deployed = workspace / relative
                archived = ARCHIVE_WRITER_PREFIX + relative
                if not deployed.is_file():
                    missing_deployed.append(relative)
                if archived not in names:
                    missing_archive.append(archived)
                elif deployed.is_file() and deployed.read_bytes() != source.read(archived):
                    mismatched.append(relative)
            for relative in DIRECTOR_FILES:
                deployed = workspace / relative
                archived = ARCHIVE_DIRECTOR_PREFIX + relative
                if not deployed.is_file():
                    missing_deployed.append(relative)
                if archived not in names:
                    missing_archive.append(archived)
                elif deployed.is_file() and deployed.read_bytes() != source.read(archived):
                    mismatched.append(relative)
    except (BadZipFile, OSError) as exc:
        archive_error = str(exc)
    archive_match = bool(
        not archive_error and not missing_deployed and not missing_archive and not mismatched
    )
    return {
        # ``ok`` means that the source archive is readable and all live files
        # exist.  A byte mismatch is expected once an optimization is being
        # tested; the runner will snapshot the live Writer files below.
        "ok": not archive_error and not missing_deployed,
        "archive": str(archive),
        "missing": sorted(set(missing_deployed + missing_archive)),
        "missing_deployed": sorted(set(missing_deployed)),
        "missing_archive": sorted(set(missing_archive)),
        "mismatched": mismatched,
        "archive_match": archive_match,
        "archive_error": archive_error,
    }


def _materialize_runtime_writer_archive(
    workspace: Path,
    run_root: Path,
    archive_report: Mapping[str, Any],
) -> tuple[Path, str]:
    """Return an archive that the existing runners can use for this run.

    The full-run runners intentionally enforce the team's Writer-v1 source
    lock.  That is useful for provenance, but it would make an optimization
    impossible to test.  Keep the original archive when the live files still
    match it; otherwise create a run-local source lock containing the live
    Writer core files.  No OpenHarness execution code is changed, and the
    original archive remains available for comparison.
    """

    workspace = workspace.resolve()
    original = Path(str(archive_report["archive"])).resolve()
    missing_deployed = set(str(value) for value in archive_report.get("missing_deployed", []))
    missing_writer = missing_deployed.intersection(WRITER_CORE_FILES)
    if missing_writer:
        raise ValueError("Writer files are missing: " + ", ".join(sorted(missing_writer)))

    writer_drift = set(str(value) for value in archive_report.get("mismatched", []))
    writer_drift.update(
        str(value)
        for value in archive_report.get("missing_archive", [])
        if str(value).startswith(ARCHIVE_WRITER_PREFIX)
    )
    if not writer_drift:
        return original, "team_archive"

    target = run_root / "source-lock" / "writer-current.zip"
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.with_suffix(".tmp")
    with ZipFile(temporary, "w", compression=ZIP_DEFLATED) as output:
        for relative in WRITER_CORE_FILES:
            source = workspace / relative
            if not source.is_file():
                raise ValueError(f"Writer file disappeared while creating source lock: {source}")
            output.write(source, arcname=ARCHIVE_WRITER_PREFIX + relative)
    temporary.replace(target)
    return target, "workspace_writer_snapshot"


def _runtime_archive_for_run(
    workspace: Path,
    run_root: Path,
    archive_report: Mapping[str, Any],
    existing_config: Mapping[str, Any] | None = None,
) -> tuple[Path, str]:
    """Reuse a run's source lock on resume, or create one for a new run."""

    if existing_config:
        configured = existing_config.get("writer_archive")
        if configured:
            configured_path = Path(str(configured)).expanduser().resolve()
            if configured_path.is_file():
                return configured_path, str(existing_config.get("writer_source_mode") or "saved_source_lock")
    return _materialize_runtime_writer_archive(workspace, run_root, archive_report)


def _git_branch(workspace: Path) -> str:
    completed = subprocess.run(
        ["git", "branch", "--show-current"],
        cwd=workspace,
        text=True,
        capture_output=True,
        check=False,
    )
    return completed.stdout.strip()


def _env_keys(path: Path) -> set[str]:
    keys: set[str] = set()
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        match = re.match(r"\s*(?:export\s+)?([A-Za-z_][A-Za-z0-9_]*)\s*=\s*(.*?)\s*$", line)
        if match and match.group(2).strip().strip("\"'"):
            keys.add(match.group(1))
    return keys


def _selection() -> dict[str, Any]:
    value = _read_json(SELECTION_FILE)
    if not isinstance(value, dict):
        raise ValueError(f"selection file is not an object: {SELECTION_FILE}")
    return value


def _task_ids(selection: Mapping[str, Any]) -> tuple[set[int], set[str]]:
    mcp = selection.get("mcp_persona", {})
    hb = selection.get("harnessbench", {})
    mcp_ids = {int(value) for value in mcp.get("task_ids", [])}
    hb_ids = {str(value) for value in hb.get("task_ids", [])}
    if len(mcp_ids) != 5 or len(hb_ids) != 5:
        raise ValueError("the fixed partial selection must contain exactly five tasks per dataset")
    return mcp_ids, hb_ids


def _validate_inputs(workspace: Path, env_file: Path, selection: Mapping[str, Any]) -> dict[str, Any]:
    mcp_ids, hb_ids = _task_ids(selection)
    required = [
        workspace / "OpenHarness" / ".venv" / "bin" / "python",
        workspace / "HarnessBench" / "src" / "harnessbench" / "cli.py",
        workspace / "MCP-Persona" / "data" / "tasks" / "en_release_data.json",
        workspace / "director_harness" / "director_mcp_catalog.json",
        env_file,
        HARNESSBENCH_MANIFEST,
    ]
    missing = [str(path) for path in required if not path.is_file()]
    if missing:
        raise ValueError("partial-run inputs are missing: " + ", ".join(missing))
    branch = _git_branch(workspace)
    if branch != EXPECTED_BRANCH:
        raise ValueError(f"partial optimization must run on {EXPECTED_BRANCH!r}; current branch is {branch!r}")
    env_keys = _env_keys(env_file)
    missing_env = sorted({"OPENAI_API_KEY", "OPENAI_API_BASE"} - env_keys)
    archive = _verify_archive(workspace)
    if not archive["ok"]:
        raise ValueError(f"Writer/Director archive verification failed: {archive}")
    harnessbench_root = workspace / "HarnessBench" / "tasks"
    missing_hb = [task_id for task_id in hb_ids if not (harnessbench_root / task_id / "task.yaml").is_file()]
    if missing_hb:
        raise ValueError("HarnessBench tasks are missing: " + ", ".join(sorted(missing_hb)))
    task_payload = _read_json(workspace / "MCP-Persona" / "data" / "tasks" / "en_release_data.json")
    available_mcp = {int(item.get("id")) for item in task_payload if isinstance(item, Mapping)}
    missing_mcp = sorted(mcp_ids - available_mcp)
    if missing_mcp:
        raise ValueError("MCP-Persona tasks are missing: " + ", ".join(map(str, missing_mcp)))
    fixed_hb = _read_json(HARNESSBENCH_MANIFEST)
    if list(fixed_hb.get("tasks", [])) != list(selection["harnessbench"]["task_ids"]):
        raise ValueError("HarnessBench fixed manifest does not match the selection file")
    return {
        "branch": branch,
        "missing_env_keys": missing_env,
        "archive": archive,
        "mcp_task_count": len(mcp_ids),
        "harnessbench_task_count": len(hb_ids),
        "repeats": int(selection.get("repeats", 2)),
    }


def _new_run_id() -> str:
    return datetime.now(RUN_TIMEZONE).strftime("%Y_%m%d_%H%M")


def _resolve_run_root(workspace: Path, run_id: str | None, resume: bool) -> Path:
    parent = workspace / "results" / "runs" / RUN_DIR_NAME
    parent.mkdir(parents=True, exist_ok=True)
    if run_id:
        if not RUN_ID_PATTERN.fullmatch(run_id):
            raise ValueError(f"invalid run id: {run_id!r}")
        return parent / run_id
    if resume:
        resumable_statuses = {"prepared", "running", "incomplete", "interrupted"}
        candidates = []
        for path in parent.iterdir():
            config_path = path / "partly-run-config.json"
            if not path.is_dir() or not config_path.is_file():
                continue
            try:
                config = _read_json(config_path)
                status = str(config.get("status") or "")
            except (OSError, json.JSONDecodeError):
                continue
            if status in resumable_statuses and config.get("execution_started") is True:
                candidates.append(path)
        if not candidates:
            raise ValueError("--resume requires --run-id or an existing partial run")
        return sorted(candidates)[-1]
    candidate = parent / _new_run_id()
    if candidate.exists():
        raise ValueError(
            f"timestamped partial run already exists: {candidate}; "
            "choose --run-id explicitly or retry in the next minute"
        )
    return candidate


def _copy_references(workspace: Path, run_root: Path, selection: Mapping[str, Any]) -> dict[str, Any]:
    reference_root = run_root / "reference"
    if (run_root / "reference-manifest.json").is_file():
        return _read_json(run_root / "reference-manifest.json")
    if reference_root.exists() and any(reference_root.iterdir()):
        raise ValueError(f"reference directory is partially populated: {reference_root}")
    reference_root.mkdir(parents=True, exist_ok=True)
    mcp_ids, hb_ids = _task_ids(selection)
    sources = {
        "Original": {
            "MCP-Persona": workspace / "results" / "full" / "results_without_Writer" / "MCP-Persona",
            "HarnessBench": workspace / "results" / "full" / "results_without_Writer" / "HarnessBench",
        },
        "Writer-Director": {
            "MCP-Persona": workspace / "results" / "runs" / "writer-director-full" / "20260817T040809Z-group-writer-v1" / "MCP-Persona",
            "HarnessBench": workspace / "results" / "runs" / "writer-director-full" / "20260817T040809Z-group-writer-v1" / "HarnessBench",
        },
    }
    manifest: dict[str, Any] = {
        "schema_version": 1,
        "source_type": "filtered_copy_of_completed_full_results",
        "created_at": datetime.now(timezone.utc).isoformat(),
        "selection_file": str(SELECTION_FILE),
        "arms": {},
    }
    for arm, dataset_sources in sources.items():
        arm_manifest: dict[str, Any] = {}
        for dataset, source in dataset_sources.items():
            if not source.is_dir():
                raise ValueError(f"full-run source is missing: {source}")
            destination = reference_root / arm / dataset
            copied: list[str] = []
            if dataset == "MCP-Persona":
                counts = _copy_mcp_arm(source, destination, mcp_ids, copied, run_root)
            else:
                counts = _copy_harnessbench_arm(source, destination, hb_ids, copied, run_root)
            arm_manifest[dataset] = {
                "source": str(source),
                "counts": counts,
                "files": sorted(copied),
            }
        manifest["arms"][arm] = arm_manifest
    manifest["file_sha256"] = {
        relative: _sha256(run_root / relative)
        for relative in manifest_file_list(run_root / "reference", run_root)
    }
    _write_json(run_root / "reference-manifest.json", manifest)
    return manifest


def manifest_file_list(root: Path, base: Path) -> Iterable[str]:
    for path in sorted(root.rglob("*")):
        if path.is_file():
            yield str(path.relative_to(base))


def _prepare_run_root(workspace: Path, run_root: Path, selection: Mapping[str, Any], args: argparse.Namespace, *, reference_only: bool) -> dict[str, Any]:
    if run_root.exists() and any(run_root.iterdir()) and not (run_root / "partly-run-config.json").is_file():
        raise ValueError(f"refusing to use a non-partial result directory: {run_root}")
    run_root.mkdir(parents=True, exist_ok=True)
    _copy_references(workspace, run_root, selection)
    selection_copy = run_root / "selection.json"
    if not selection_copy.exists():
        shutil.copy2(SELECTION_FILE, selection_copy)
        shutil.copy2(HARNESSBENCH_MANIFEST, run_root / "harnessbench.tasks.json")
    config_path = run_root / "partly-run-config.json"
    existing = _read_json(config_path) if config_path.is_file() else {}
    config = {
        **existing,
        "schema_version": 1,
        "dataset_id": selection.get("dataset_id"),
        "run_id": run_root.name,
        "status": "reference_only" if reference_only else existing.get("status", "prepared"),
        "workspace_root": str(workspace),
        "env_file": str(args.env_file),
        "model": args.model,
        "repeats": args.repeats,
        "openharness_mode": "writer_harness",
        "director_harness_enabled": True,
        "mcp_task_ids": list(selection["mcp_persona"]["task_ids"]),
        "harnessbench_task_ids": list(selection["harnessbench"]["task_ids"]),
        "reference_manifest": str(run_root / "reference-manifest.json"),
        "launcher": str(Path(__file__).resolve()),
        "updated_at": datetime.now(timezone.utc).isoformat(),
    }
    if not reference_only:
        config["status"] = existing.get("status", "prepared")
    _write_json(config_path, config)
    return config


def _command_common(
    workspace: Path,
    env_file: Path,
    model: str,
    repeats: int,
    run_root: Path,
    mcp_task_ids: Sequence[int],
    writer_archive: Path,
) -> tuple[list[str], list[str]]:
    openharness_python = workspace / "OpenHarness" / ".venv" / "bin" / "python"
    catalog = workspace / "director_harness" / "director_mcp_catalog.json"
    writer_args = [
        "--openharness-mode", "writer_harness",
        "--writer-workspace-root", str(workspace),
        "--writer-archive", str(writer_archive),
        "--writer-model", model,
        "--writer-max-tokens", "4096",
        "--director-harness-enabled",
        "--director-mcp-catalog", str(catalog),
    ]
    mcp = [
        str(openharness_python), "scripts/run_mcp_persona_week1.py",
        "--mcp-persona-root", str(workspace / "MCP-Persona"),
        "--output-dir", str(run_root / "MCP-Persona"),
        "--language", "en", "--mode", "full",
        "--task-ids", *(str(value) for value in mcp_task_ids),
        "--repeats", str(repeats), "--experiment-stage", "paired-smoke",
        "--model", model, "--env-file", str(env_file),
        "--tool-scope", "server", "--no-chain-guidance",
        "--max-turns", "30", "--max-tokens", "4096",
        "--temperature", "0", "--seed", "42",
        *writer_args,
    ]
    hb = [
        str(openharness_python), "scripts/run_openharness_harnessbench.py", "run",
        "--harnessbench-root", str(workspace / "HarnessBench"),
        "--task-manifest", str(HARNESSBENCH_MANIFEST),
        "--model", model, "--profile", "qwen", "--api-format", "openai",
        "--env-file", str(env_file), "--grading", "full",
        "--rubric-model", model, "--rubric-vision-model", model,
        "--rubric-base-url-env", "OPENAI_API_BASE", "--rubric-api-key-env", "OPENAI_API_KEY",
        "--public-url-mode", "loopback", "--temperature", "0", "--seed", "42",
        "--max-turns", "80", "--api-timeout-sec", "300", "--repeats", str(repeats),
        "--output-dir", str(run_root / "HarnessBench"),
        *writer_args,
    ]
    return mcp, hb


def _preflight_command(
    workspace: Path,
    env_file: Path,
    model: str,
    writer_archive: Path,
) -> list[str]:
    openharness_python = workspace / "OpenHarness" / ".venv" / "bin" / "python"
    catalog = workspace / "director_harness" / "director_mcp_catalog.json"
    return [
        str(openharness_python), "scripts/run_openharness_harnessbench.py", "preflight",
        "--harnessbench-root", str(workspace / "HarnessBench"),
        "--task-manifest", str(HARNESSBENCH_MANIFEST),
        "--model", model, "--profile", "qwen", "--api-format", "openai",
        "--env-file", str(env_file), "--grading", "full",
        "--rubric-model", model, "--rubric-vision-model", model,
        "--rubric-base-url-env", "OPENAI_API_BASE", "--rubric-api-key-env", "OPENAI_API_KEY",
        "--public-url-mode", "loopback",
        "--openharness-mode", "writer_harness",
        "--writer-workspace-root", str(workspace), "--writer-archive", str(writer_archive),
        "--writer-model", model, "--writer-max-tokens", "4096",
        "--director-harness-enabled", "--director-mcp-catalog", str(catalog),
    ]


def _run_command(
    command: Sequence[str],
    cwd: Path,
    log_path: Path,
    *,
    label: str,
) -> int:
    """Run a child runner while teeing its output to the terminal and log.

    The benchmark runners already emit task/trial progress.  Keeping their
    stream attached to a pipe lets this launcher preserve an exact log while
    forwarding each line immediately to the user's terminal.
    """

    log_path.parent.mkdir(parents=True, exist_ok=True)
    display_command = shlex.join(str(value) for value in command)
    started = time.monotonic()
    print(f"\n[writer-director-partly] {label} starting", flush=True)
    print(f"[writer-director-partly] log: {log_path}", flush=True)
    with log_path.open("w", encoding="utf-8") as log:
        log.write("$ " + display_command + "\n\n")
        log.flush()
        child_env = os.environ.copy()
        # Python otherwise block-buffers stdout when it is connected to this
        # pipe, which makes long API calls appear idle in the terminal.
        child_env["PYTHONUNBUFFERED"] = "1"
        process = subprocess.Popen(
            list(command),
            cwd=cwd,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            bufsize=1,
            env=child_env,
        )
        try:
            assert process.stdout is not None
            for line in process.stdout:
                log.write(line)
                log.flush()
                sys.stdout.write(line)
                sys.stdout.flush()
            return_code = process.wait()
        except KeyboardInterrupt:
            log.write("\n[writer-director-partly] interrupted by user\n")
            log.flush()
            process.terminate()
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait()
            raise
        finally:
            if process.stdout is not None:
                process.stdout.close()
    elapsed = time.monotonic() - started
    print(
        f"[writer-director-partly] {label} finished "
        f"rc={return_code} elapsed={elapsed:.1f}s",
        flush=True,
    )
    return return_code


def _print_commands(commands: Mapping[str, Sequence[str]]) -> None:
    for name, command in commands.items():
        print(f"[{name}] {' '.join(command)}")


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("workspace_root", nargs="?", type=Path, default=WORKSPACE_DEFAULT)
    parser.add_argument("env_file", nargs="?", type=Path)
    parser.add_argument("--run-id")
    parser.add_argument("--resume", action="store_true")
    parser.add_argument("--prepare-only", action="store_true", help="Copy the fixed Original and Writer-Director references without calling a model")
    parser.add_argument("--check-only", action="store_true", help="Validate branch, dependencies, source archive and task manifests without calling a model")
    parser.add_argument("--preflight-only", action="store_true", help="Run the HarnessBench local preflight and stop before any model task")
    parser.add_argument("--dry-run", action="store_true", help="Prepare the root and print commands without calling a model")
    parser.add_argument("--dataset", choices=("all", "mcp", "harnessbench"), default="all")
    parser.add_argument("--model", default=DEFAULT_MODEL)
    parser.add_argument("--repeats", type=int, default=2)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    workspace = args.workspace_root.expanduser().resolve()
    args.env_file = (args.env_file or workspace / ".env").expanduser().resolve()
    selection = _selection()
    if args.repeats < 1:
        raise SystemExit("--repeats must be positive")
    if args.repeats != int(selection.get("repeats", 2)):
        raise SystemExit(
            f"the fixed partial protocol is locked to {selection.get('repeats', 2)} repeats"
        )
    validation = _validate_inputs(workspace, args.env_file, selection)
    if args.check_only:
        print(json.dumps({"ok": True, **validation}, ensure_ascii=False, indent=2))
        return 0
    if args.preflight_only:
        preflight_root = workspace / "results" / "runs" / RUN_DIR_NAME / ".preflight-source"
        writer_archive, source_mode = _runtime_archive_for_run(
            workspace, preflight_root, validation["archive"]
        )
        command = _preflight_command(workspace, args.env_file, args.model, writer_archive)
        print(f"[writer_source] mode={source_mode} archive={writer_archive}")
        print("[harnessbench_preflight] " + " ".join(command))
        return _run_command(
            command,
            workspace / "OpenHarness",
            workspace / "results" / "runs" / RUN_DIR_NAME / "preflight.log",
            label="harnessbench_preflight",
        )
    run_root = _resolve_run_root(workspace, args.run_id, args.resume)
    if args.resume and not run_root.exists():
        raise SystemExit(f"partial run does not exist: {run_root}")
    if args.resume and (run_root / "partly-run-config.json").is_file():
        existing_config = _read_json(run_root / "partly-run-config.json")
        existing_status = str(existing_config.get("status") or "")
        if existing_status == "reference_only":
            raise SystemExit(
                "the selected root is a reference-only snapshot; choose a new --run-id for an actual run"
            )
        if existing_config.get("execution_started") is not True:
            raise SystemExit(
                "the selected root has no recorded model execution; choose a new --run-id"
            )
    reference_only = bool(args.prepare_only)
    config = _prepare_run_root(workspace, run_root, selection, args, reference_only=reference_only)
    if reference_only:
        print(json.dumps({"reference_root": str(run_root), "reference_manifest": str(run_root / "reference-manifest.json"), "status": "reference_only"}, ensure_ascii=False, indent=2))
        return 0
    writer_archive, source_mode = _runtime_archive_for_run(
        workspace,
        run_root,
        validation["archive"],
        config if args.resume else None,
    )
    config = {
        **config,
        "writer_archive": str(writer_archive),
        "writer_source_mode": source_mode,
        "archive_verification": validation["archive"],
    }
    mcp_command, hb_command = _command_common(
        workspace,
        args.env_file,
        args.model,
        args.repeats,
        run_root,
        selection["mcp_persona"]["task_ids"],
        writer_archive,
    )
    commands: dict[str, Sequence[str]] = {}
    if args.dataset in {"all", "harnessbench"}:
        commands["harnessbench_preflight"] = _preflight_command(
            workspace, args.env_file, args.model, writer_archive
        )
    if args.dataset in {"all", "mcp"}:
        commands["mcp_persona"] = mcp_command
    if args.dataset in {"all", "harnessbench"}:
        commands["harnessbench"] = hb_command
    if args.resume:
        if (run_root / "MCP-Persona" / "run-config.json").is_file() and "mcp_persona" in commands:
            commands["mcp_persona"] = [*commands["mcp_persona"], "--resume"]
        if (run_root / "HarnessBench" / "run-config.json").is_file() and "harnessbench" in commands:
            commands["harnessbench"] = [*commands["harnessbench"], "--resume"]
    _write_json(run_root / "partly-run-config.json", {**config, "status": "running", "execution_started": False, "commands": {key: list(value) for key, value in commands.items()}, "updated_at": datetime.now(timezone.utc).isoformat()})
    if args.dry_run:
        _write_json(run_root / "partly-run-config.json", {**config, "status": "dry_run", "execution_started": False, "commands": {key: list(value) for key, value in commands.items()}, "updated_at": datetime.now(timezone.utc).isoformat()})
        _print_commands(commands)
        return 0
    openharness_root = workspace / "OpenHarness"
    _write_json(run_root / "partly-run-config.json", {**config, "status": "running", "execution_started": True, "commands": {key: list(value) for key, value in commands.items()}, "updated_at": datetime.now(timezone.utc).isoformat()})
    print(f"[writer-director-partly] result root: {run_root}", flush=True)
    print(f"[writer-director-partly] writer source: {source_mode} ({writer_archive})", flush=True)
    print(
        "[writer-director-partly] selected tasks: "
        f"MCP={selection['mcp_persona']['task_ids']} "
        f"HarnessBench={selection['harnessbench']['task_ids']}",
        flush=True,
    )
    try:
        for name, command in commands.items():
            # The HarnessBench preflight only validates local configuration; it does not send a model request.
            return_code = _run_command(
                command,
                openharness_root,
                run_root / "launcher-logs" / f"{name}.log",
                label=name,
            )
            if return_code != 0:
                _write_json(run_root / "partly-run-config.json", {**config, "status": "incomplete", "execution_started": True, "failed_command": name, "return_code": return_code, "updated_at": datetime.now(timezone.utc).isoformat()})
                return return_code
    except KeyboardInterrupt:
        _write_json(run_root / "partly-run-config.json", {**config, "status": "interrupted", "execution_started": True, "resume_run_id": run_root.name, "updated_at": datetime.now(timezone.utc).isoformat()})
        print(f"Interrupted. Resume with: bash OpenHarness/scripts/run_writer_director_partly.sh --resume --run-id {run_root.name}", file=sys.stderr)
        return 130
    _write_json(run_root / "partly-run-config.json", {**config, "status": "completed", "execution_started": True, "updated_at": datetime.now(timezone.utc).isoformat()})
    print(json.dumps({"result_root": str(run_root), "status": "completed", "resume_command": f"bash OpenHarness/scripts/run_writer_director_partly.sh --resume --run-id {run_root.name}"}, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        print(f"{type(exc).__name__}: {exc}", file=sys.stderr)
        raise SystemExit(20)
