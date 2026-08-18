#!/usr/bin/env python3
"""Offline preflight for the team Writer+Director full experiments."""

from __future__ import annotations

import argparse
import hashlib
import json
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping, Sequence
from zipfile import ZipFile

from openharness.rehearsal.mcp_persona_rehearsal import VERIFIED_TASK_IDS
from openharness.rehearsal.writer_handoff import verify_writer_deployment


MODEL = "qwen3.6-plus"
ARCHIVE_PREFIX = "writer_director_0812/writer_harness_demo/"
DIRECTOR_FILES = (
    "director_harness/__init__.py",
    "director_harness/catalog.py",
    "director_harness/director_mcp_catalog.json",
    "director_harness/events.py",
    "director_harness/harness.py",
    "director_harness/models.py",
    "director_harness/README.md",
    "director_harness/test_harness.py",
)


def _read_json(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"JSON file must contain an object: {path}")
    return value


def _write_json(path: Path, value: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(value, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )


def _sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _configured_env_keys(path: Path) -> set[str]:
    keys: set[str] = set()
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        match = re.match(r"\s*(?:export\s+)?([A-Za-z_][A-Za-z0-9_]*)\s*=\s*(.+?)\s*$", line)
        if match and match.group(2).strip().strip("\"'"):
            keys.add(match.group(1))
    return keys


def _verify_director(workspace_root: Path, archive: Path) -> dict[str, Any]:
    missing: list[str] = []
    mismatched: list[str] = []
    hashes: dict[str, str] = {}
    with ZipFile(archive) as source:
        names = set(source.namelist())
        for relative in DIRECTOR_FILES:
            deployed = workspace_root / relative
            archived = ARCHIVE_PREFIX + relative
            if not deployed.is_file() or archived not in names:
                missing.append(relative)
                continue
            deployed_bytes = deployed.read_bytes()
            hashes[relative] = _sha256_bytes(deployed_bytes)
            if deployed_bytes != source.read(archived):
                mismatched.append(relative)
    return {
        "ok": not missing and not mismatched,
        "checked_files": len(DIRECTOR_FILES),
        "missing_files": missing,
        "mismatched_files": mismatched,
        "file_sha256": hashes,
    }


def _output_status(path: Path, expected: Mapping[str, Any]) -> str:
    if not path.exists() or not any(path.iterdir()):
        return "fresh"
    config_path = path / "run-config.json"
    if not config_path.is_file():
        raise ValueError(
            f"non-empty output directory has no run-config.json: {path}"
        )
    config = _read_json(config_path)
    mismatches = [key for key, value in expected.items() if config.get(key) != value]
    if mismatches:
        raise ValueError(
            f"{path} belongs to a different experiment; mismatched fields: "
            + ", ".join(mismatches)
        )
    return "resumable"


def build_preflight(
    *,
    workspace_root: Path,
    env_file: Path,
    result_root: Path,
    model: str,
    repeats: int,
) -> dict[str, Any]:
    workspace_root = workspace_root.resolve()
    env_file = env_file.resolve()
    result_root = result_root.resolve()
    openharness_root = workspace_root / "OpenHarness"
    harnessbench_root = workspace_root / "HarnessBench"
    mcp_root = workspace_root / "MCP-Persona"
    archive = workspace_root / "docs" / "writer_director_0812.zip"
    catalog = workspace_root / "director_harness" / "director_mcp_catalog.json"
    hb_manifest_path = (
        workspace_root
        / "results/config/harnessbench-writer-full-v1.tasks.json"
    )
    required = (
        openharness_root / ".venv/bin/python",
        harnessbench_root / "src/harnessbench/cli.py",
        mcp_root / "data/tasks/en_release_data.json",
        archive,
        catalog,
        hb_manifest_path,
        env_file,
    )
    missing = [str(path) for path in required if not path.is_file()]
    if missing:
        raise ValueError("required full-run inputs are missing: " + ", ".join(missing))
    if model != MODEL:
        raise ValueError(f"Writer+Director full experiment is locked to {MODEL}")
    if repeats != 2:
        raise ValueError("Writer+Director full experiment is locked to two repeats")
    env_keys = _configured_env_keys(env_file)
    missing_env = sorted({"OPENAI_API_KEY", "OPENAI_API_BASE"} - env_keys)
    if missing_env:
        raise ValueError(
            "env file lacks non-empty required settings: " + ", ".join(missing_env)
        )

    writer = verify_writer_deployment(
        workspace_root,
        archive,
        include_support_files=False,
    )
    if not writer.ok:
        raise ValueError(
            "deployed Writer does not match the team archive: "
            f"missing={list(writer.missing_files)}, "
            f"mismatched={list(writer.mismatched_files)}"
        )
    director = _verify_director(workspace_root, archive)
    if not director["ok"]:
        raise ValueError(
            "deployed Director does not match the team archive: "
            f"missing={director['missing_files']}, "
            f"mismatched={director['mismatched_files']}"
        )
    catalog_payload = _read_json(catalog)
    if not isinstance(catalog_payload.get("mcp_candidates"), list):
        raise ValueError("Director MCP catalog lacks mcp_candidates")

    hb_manifest = _read_json(hb_manifest_path)
    hb_tasks = [str(value) for value in hb_manifest.get("tasks", [])]
    if len(hb_tasks) != 106 or len(set(hb_tasks)) != 106:
        raise ValueError("HarnessBench full manifest is not the complete 106-task set")
    missing_hb = [
        task_id
        for task_id in hb_tasks
        if not (harnessbench_root / "tasks" / task_id / "task.yaml").is_file()
    ]
    if missing_hb:
        raise ValueError(
            "HarnessBench task files are missing: " + ", ".join(missing_hb[:10])
        )

    mcp_output = result_root / "MCP-Persona"
    hb_output = result_root / "HarnessBench"
    mcp_status = _output_status(
        mcp_output,
        {
            "tasks": list(VERIFIED_TASK_IDS),
            "repeats": repeats,
            "model": model,
            "writer_model": model,
            "language": "en",
            "tool_scope": "server",
            "chain_guidance": False,
            "experiment_stage": "writer-director-full",
            "openharness_mode": "writer_harness",
            "director_harness_enabled": True,
            "director_mcp_catalog": str(catalog.resolve()),
        },
    )
    hb_status = _output_status(
        hb_output,
        {
            "tasks": hb_tasks,
            "repeats": repeats,
            "model": model,
            "writer_model": model,
            "openharness_mode": "writer_harness",
            "director_harness_enabled": True,
            "director_mcp_catalog": str(catalog.resolve()),
            "public_url_mode": "loopback",
        },
    )
    if hb_status == "resumable":
        grading = _read_json(hb_output / "run-config.json").get("grading")
        if not isinstance(grading, Mapping) or grading.get("mode") != "full":
            raise ValueError("HarnessBench output is not locked to full grading")

    return {
        "schema_version": 1,
        "ready": True,
        "external_model_called": False,
        "branch_expected": "feature/writer-director",
        "model": {"actor": model, "writer": model},
        "writer_deployment": writer.to_dict(),
        "director_deployment": director,
        "director_mcp_catalog": str(catalog.resolve()),
        "mcp_persona": {
            "dataset": "Verified52",
            "task_count": len(VERIFIED_TASK_IDS),
            "repeats": repeats,
            "concurrency": 1,
            "output_dir": str(mcp_output),
            "output_status": mcp_status,
        },
        "harnessbench": {
            "dataset": hb_manifest.get("dataset_id"),
            "task_count": len(hb_tasks),
            "repeats": repeats,
            "concurrency": 1,
            "grading": "full",
            "output_dir": str(hb_output),
            "output_status": hb_status,
        },
        "checked_at": datetime.now(timezone.utc).isoformat(),
    }


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--workspace-root", type=Path, required=True)
    parser.add_argument("--env-file", type=Path, required=True)
    parser.add_argument("--result-root", type=Path, required=True)
    parser.add_argument("--model", default=MODEL)
    parser.add_argument("--repeats", type=int, default=2)
    parser.add_argument("--output", type=Path)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    payload = build_preflight(
        workspace_root=args.workspace_root,
        env_file=args.env_file,
        result_root=args.result_root,
        model=args.model,
        repeats=args.repeats,
    )
    if args.output is not None:
        _write_json(args.output, payload)
    print(json.dumps(payload, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
