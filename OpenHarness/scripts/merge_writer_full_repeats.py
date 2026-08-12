#!/usr/bin/env python3
"""Merge two compatible one-repeat Writer full runs into one two-repeat result set."""

from __future__ import annotations

import argparse
import json
import shutil
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping, Sequence

from openharness.rehearsal.mcp_persona_rehearsal import (
    VERIFIED52_PROTOCOL_ID,
    VERIFIED52_WRITER_ARM,
    VERIFIED_TASK_IDS,
)
from scripts.run_mcp_persona_week1 import _summary_for_results
from scripts.run_openharness_harnessbench import _summary_for_output


MODEL = "qwen3.6-plus"
MCP_OUTPUT_NAME = "MCP-Persona"
HARNESSBENCH_OUTPUT_NAME = "HarnessBench"


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


def _write_jsonl(path: Path, rows: Sequence[Mapping[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.tmp")
    temporary.write_text(
        "".join(json.dumps(row, ensure_ascii=False) + "\n" for row in rows),
        encoding="utf-8",
    )
    temporary.replace(path)


def _validate_mcp(output: Path, *, label: str) -> dict[str, Any]:
    config = _read_json(output / "run-config.json")
    summary = _read_json(output / "baseline-summary.json")
    rows = _read_jsonl(output / "results.jsonl")
    expected = {(task_id, 1) for task_id in VERIFIED_TASK_IDS}
    actual = {
        (int(row.get("task_id", -1)), int(row.get("trial", -1)))
        for row in rows
    }
    if (
        config.get("tasks") != list(VERIFIED_TASK_IDS)
        or config.get("repeats") != 1
        or config.get("model") != MODEL
        or config.get("writer_model") != MODEL
        or config.get("experiment_stage") != "writer-full"
        or config.get("openharness_mode") != "writer_harness"
        or config.get("chain_guidance") is not False
        or actual != expected
        or len(rows) != len(expected)
    ):
        raise ValueError(f"{label} MCP output is not the locked 52x1 Writer full run")
    if (
        summary.get("expected_results") != 52
        or summary.get("result_count") != 52
        or summary.get("valid_result_count") != 52
        or summary.get("run_complete") is not True
    ):
        raise ValueError(f"{label} MCP run is incomplete")
    writer_gate = summary.get("writer_smoke_gate")
    if not isinstance(writer_gate, Mapping) or writer_gate.get("passed") is not True:
        raise ValueError(f"{label} MCP Writer gate did not pass")
    return {"config": config, "summary": summary, "rows": rows}


def _harnessbench_result_ids(output: Path) -> set[str]:
    result_ids: set[str] = set()
    for path in output.glob("repeat-01/results/**/*.json"):
        payload = _read_json(path)
        task_id = payload.get("task_id")
        if not isinstance(task_id, str) or not task_id:
            raise ValueError(f"invalid HarnessBench result: {path}")
        if task_id in result_ids:
            raise ValueError(f"duplicate HarnessBench result for {task_id}: {path}")
        result_ids.add(task_id)
    return result_ids


def _validate_harnessbench(output: Path, *, label: str) -> dict[str, Any]:
    config = _read_json(output / "run-config.json")
    summary = _read_json(output / "baseline-summary.json")
    tasks = config.get("tasks")
    result_ids = _harnessbench_result_ids(output)
    if (
        not isinstance(tasks, list)
        or len(tasks) != 106
        or len(set(tasks)) != 106
        or config.get("repeats") != 1
        or config.get("model") != MODEL
        or config.get("writer_model") != MODEL
        or config.get("openharness_mode") != "writer_harness"
        or result_ids != set(tasks)
    ):
        raise ValueError(f"{label} HarnessBench output is not the locked 106x1 Writer full run")
    grading = config.get("grading")
    if not isinstance(grading, Mapping) or grading.get("mode") != "full":
        raise ValueError(f"{label} HarnessBench output is not fully graded")
    if (
        summary.get("expected_results") != 106
        or summary.get("result_count") != 106
        or summary.get("full_grading_complete") is not True
    ):
        raise ValueError(f"{label} HarnessBench run or grading is incomplete")
    missing_logs = [
        task_id
        for task_id in tasks
        if not (output / "repeat-01" / "logs" / f"{task_id}.log").is_file()
    ]
    if missing_logs:
        raise ValueError(
            f"{label} HarnessBench logs are missing: " + ", ".join(missing_logs[:5])
        )
    return {"config": config, "summary": summary}


def _check_equal(
    first: Mapping[str, Any],
    second: Mapping[str, Any],
    *,
    fields: Sequence[str],
    label: str,
) -> None:
    mismatches = [field for field in fields if first.get(field) != second.get(field)]
    if mismatches:
        raise ValueError(
            f"the two {label} runs are not comparable; mismatched fields: "
            + ", ".join(mismatches)
        )


def _validate_compatibility(
    first_mcp: Mapping[str, Any],
    second_mcp: Mapping[str, Any],
    first_hb: Mapping[str, Any],
    second_hb: Mapping[str, Any],
) -> None:
    _check_equal(
        first_mcp,
        second_mcp,
        fields=(
            "tasks",
            "language",
            "mode",
            "model",
            "tool_scope",
            "max_turns",
            "max_tokens",
            "temperature",
            "seed",
            "chain_guidance",
            "experiment_stage",
            "openharness_mode",
            "writer_model",
            "writer_max_tokens",
        ),
        label="MCP",
    )
    _check_equal(
        first_hb,
        second_hb,
        fields=(
            "dataset_id",
            "tasks",
            "excluded_tasks",
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
            "public_url_mode",
            "grading",
        ),
        label="HarnessBench",
    )


def _copy_tree(source: Path, target: Path) -> None:
    if not source.is_dir():
        raise ValueError(f"source directory not found: {source}")
    shutil.copytree(source, target, dirs_exist_ok=True)


def _prepare_output_root(
    output_root: Path,
    *,
    first_root: Path,
    second_root: Path,
) -> None:
    marker_path = output_root / "merge-manifest.json"
    if output_root.exists() and any(output_root.iterdir()):
        if not marker_path.is_file():
            raise ValueError(
                f"refusing to merge into a non-empty untracked directory: {output_root}"
            )
        marker = _read_json(marker_path)
        if (
            marker.get("first_result_root") != str(first_root)
            or marker.get("second_result_root") != str(second_root)
        ):
            raise ValueError(f"{output_root} belongs to different source runs")
    output_root.mkdir(parents=True, exist_ok=True)
    _write_json(
        marker_path,
        {
            "schema_version": 1,
            "status": "building",
            "first_result_root": str(first_root),
            "second_result_root": str(second_root),
            "output_root": str(output_root),
            "updated_at": datetime.now(timezone.utc).isoformat(),
        },
    )


def merge_runs(
    *,
    first_root: Path,
    second_root: Path,
    output_root: Path,
) -> dict[str, Any]:
    first_root = first_root.resolve()
    second_root = second_root.resolve()
    output_root = output_root.resolve()
    if len({first_root, second_root, output_root}) != 3:
        raise ValueError("first, second, and merged result roots must be different")

    first_mcp_dir = first_root / MCP_OUTPUT_NAME
    second_mcp_dir = second_root / MCP_OUTPUT_NAME
    first_hb_dir = first_root / HARNESSBENCH_OUTPUT_NAME
    second_hb_dir = second_root / HARNESSBENCH_OUTPUT_NAME
    first_mcp = _validate_mcp(first_mcp_dir, label="first")
    second_mcp = _validate_mcp(second_mcp_dir, label="second")
    first_hb = _validate_harnessbench(first_hb_dir, label="first")
    second_hb = _validate_harnessbench(second_hb_dir, label="second")
    _validate_compatibility(
        first_mcp["config"],
        second_mcp["config"],
        first_hb["config"],
        second_hb["config"],
    )
    _prepare_output_root(
        output_root,
        first_root=first_root,
        second_root=second_root,
    )

    merged_mcp_dir = output_root / MCP_OUTPUT_NAME
    merged_hb_dir = output_root / HARNESSBENCH_OUTPUT_NAME
    merged_mcp_dir.mkdir(parents=True, exist_ok=True)
    merged_hb_dir.mkdir(parents=True, exist_ok=True)

    first_rows = [dict(row) for row in first_mcp["rows"]]
    second_rows: list[dict[str, Any]] = []
    for source in second_mcp["rows"]:
        row = dict(source)
        row["trial"] = 2
        second_rows.append(row)
    merged_rows = sorted(
        first_rows + second_rows,
        key=lambda row: (int(row["task_id"]), int(row["trial"])),
    )
    merged_mcp_config = dict(first_mcp["config"])
    merged_mcp_config["repeats"] = 2
    merged_mcp_config["formal_verified52"] = False
    merged_mcp_config["writer_full_verified52"] = True
    merged_mcp_config["protocol_id"] = VERIFIED52_PROTOCOL_ID
    merged_mcp_config["experiment_arm"] = VERIFIED52_WRITER_ARM
    merged_mcp_config["source_lock_enforced"] = False
    merged_mcp_config["merge_sources"] = {
        "repeat_1": str(first_mcp_dir),
        "repeat_2": str(second_mcp_dir),
    }
    _write_json(merged_mcp_dir / "run-config.json", merged_mcp_config)
    _write_jsonl(merged_mcp_dir / "results.jsonl", merged_rows)
    grouped: dict[int, list[dict[str, Any]]] = {
        task_id: [
            row for row in merged_rows if int(row["task_id"]) == task_id
        ]
        for task_id in VERIFIED_TASK_IDS
    }
    mcp_summary = _summary_for_results(grouped, merged_mcp_config)
    _write_json(merged_mcp_dir / "baseline-summary.json", mcp_summary)
    for name in (
        "official-evaluator-probe.json",
        "static-audit.csv",
        "static-audit.json",
        "version-manifest.json",
    ):
        source = first_mcp_dir / name
        if source.is_file():
            shutil.copy2(source, merged_mcp_dir / name)
    first_semantic = first_mcp_dir / "semantic-reviews.jsonl"
    if not first_semantic.is_file():
        raise ValueError(f"first-repeat semantic reviews not found: {first_semantic}")
    shutil.copy2(first_semantic, merged_mcp_dir / "semantic-repeat-01.jsonl")

    merged_hb_config = dict(first_hb["config"])
    merged_hb_config["repeats"] = 2
    merged_hb_config["source_lock_enforced"] = False
    merged_hb_config["merge_sources"] = {
        "repeat_1": str(first_hb_dir),
        "repeat_2": str(second_hb_dir),
    }
    _write_json(merged_hb_dir / "run-config.json", merged_hb_config)
    for target_repeat, source_dir in (
        ("repeat-01", first_hb_dir / "repeat-01"),
        ("repeat-02", second_hb_dir / "repeat-01"),
    ):
        _copy_tree(
            source_dir / "results",
            merged_hb_dir / target_repeat / "results",
        )
        _copy_tree(
            source_dir / "logs",
            merged_hb_dir / target_repeat / "logs",
        )
    harness_config = first_hb_dir / "harness.local.json"
    if harness_config.is_file():
        shutil.copy2(harness_config, merged_hb_dir / harness_config.name)
    hb_summary = _summary_for_output(merged_hb_dir, merged_hb_config)
    _write_json(merged_hb_dir / "baseline-summary.json", hb_summary)

    payload = {
        "schema_version": 1,
        "status": "complete",
        "first_result_root": str(first_root),
        "second_result_root": str(second_root),
        "output_root": str(output_root),
        "mcp_persona": {
            "task_count": 52,
            "repeats": 2,
            "result_count": len(merged_rows),
            "run_complete": mcp_summary.get("run_complete"),
            "writer_gate_passed": mcp_summary.get("writer_smoke_gate", {}).get(
                "passed"
            ),
        },
        "harnessbench": {
            "task_count": 106,
            "repeats": 2,
            "result_count": hb_summary.get("result_count"),
            "full_grading_complete": hb_summary.get("full_grading_complete"),
        },
        "updated_at": datetime.now(timezone.utc).isoformat(),
    }
    _write_json(output_root / "merge-manifest.json", payload)
    return payload


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--first-result-root", type=Path, required=True)
    parser.add_argument("--second-result-root", type=Path)
    parser.add_argument("--output-root", type=Path)
    parser.add_argument("--check-only", action="store_true")
    return parser


def main() -> int:
    args = build_parser().parse_args()
    first_root = args.first_result_root.resolve()
    first_mcp = _validate_mcp(first_root / MCP_OUTPUT_NAME, label="first")
    first_hb = _validate_harnessbench(
        first_root / HARNESSBENCH_OUTPUT_NAME,
        label="first",
    )
    if args.check_only:
        payload: dict[str, Any] = {
            "ready": True,
            "external_model_called": False,
            "first_result_root": str(first_root),
            "mcp_result_count": len(first_mcp["rows"]),
            "harnessbench_result_count": first_hb["summary"].get("result_count"),
            "source_locking_enabled": False,
        }
        if args.second_result_root:
            payload["second_result_root"] = str(args.second_result_root.resolve())
        print(json.dumps(payload, ensure_ascii=False, indent=2))
        return 0
    if args.second_result_root is None or args.output_root is None:
        raise ValueError("--second-result-root and --output-root are required to merge")
    payload = merge_runs(
        first_root=first_root,
        second_root=args.second_result_root,
        output_root=args.output_root,
    )
    print(json.dumps(payload, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
