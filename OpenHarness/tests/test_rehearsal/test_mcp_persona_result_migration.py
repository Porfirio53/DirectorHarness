from __future__ import annotations

import json
from pathlib import Path

from openharness.rehearsal.mcp_persona_rehearsal import VERIFIED_TASK_IDS
from scripts.migrate_mcp_persona_full_results import (
    build_comparability_audit,
    migrate_output_metadata,
    normalize_external_environment,
)


def _write(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value), encoding="utf-8")


def _make_output(root: Path, *, writer: bool) -> None:
    mode = "writer_harness" if writer else "original"
    dataset = (
        "mcp-persona-verified52-writer-full"
        if writer
        else "mcp-persona-verified52"
    )
    config: dict[str, object] = {
        "dataset_id": dataset,
        "formal_verified52": not writer,
        "tasks": list(VERIFIED_TASK_IDS),
        "repeats": 2,
        "language": "en",
        "tool_scope": "server",
        "chain_guidance": False,
        "experiment_stage": "writer-full" if writer else "baseline",
        "openharness_mode": mode,
        "model": "test-model",
        "writer_model": "test-model" if writer else None,
        "runtime_fingerprint_sha256": "legacy",
        "source_sha256": {
            "/workspace/OpenHarness/scripts/run_mcp_persona_week1.py": (
                "writer-runner" if writer else "original-runner"
            ),
            "/workspace/OpenHarness/scripts/mcp_persona_stdio_server.py": "server",
            "/workspace/OpenHarness/src/openharness/engine/query.py": "query",
            "/workspace/MCP-Persona/data/tasks/en_release_data.json": "tasks",
        },
        "openharness_git": {"root": "/missing", "commit": "missing"},
    }
    if writer:
        config["writer_full_verified52"] = True
    _write(root / "run-config.json", config)
    _write(
        root / "version-manifest.json",
        {
            "runtime": {"python": "3.11", "packages": {}},
            "mcp_persona": {
                "external_environment": {} if writer else {
                    "python": "external-python",
                    "returncode": 0,
                    "details": json.dumps({"python": "3.12", "packages": {}}),
                }
            },
            "file_sha256": config["source_sha256"],
        },
    )
    rows = []
    for task_id in VERIFIED_TASK_IDS:
        for trial in (1, 2):
            row: dict[str, object] = {
                "task_id": task_id,
                "trial": trial,
                "errors": [],
                "state": {"exact_reset": True},
                "tool_calls": [],
                "usage": {},
            }
            if writer:
                row["writer"] = {
                    "mandatory_passed": True,
                    "planning_state_unchanged": True,
                    "event_validation": {"events_complete": True},
                }
            rows.append(row)
    (root / "results.jsonl").write_text(
        "".join(json.dumps(row) + "\n" for row in rows),
        encoding="utf-8",
    )
    _write(root / "semantic-summary.json", {"semantic_review_ready": True})
    _write(
        root / ("rehearsal-step-summary.json" if writer else "baseline2-summary.json"),
        {"evaluator_sha256": "same", "spec_sha256": "same"},
    )


def test_legacy_external_environment_is_normalized() -> None:
    captured = normalize_external_environment(
        {
            "python": "external-python",
            "returncode": 0,
            "details": json.dumps(
                {"python": "3.12", "packages": {"mcp": "1.0"}}
            ),
        }
    )

    assert captured["status"] == "captured"
    assert captured["python_version"] == "3.12"
    assert normalize_external_environment({})["status"] == "not_requested"


def test_migration_makes_writer_ready_and_emits_honest_audit(tmp_path: Path) -> None:
    original = tmp_path / "original"
    writer = tmp_path / "writer"
    _make_output(original, writer=False)
    _make_output(writer, writer=True)

    migrate_output_metadata(original)
    migrate_output_metadata(writer)
    audit = build_comparability_audit(original, writer)

    writer_summary = json.loads(
        (writer / "baseline-summary.json").read_text(encoding="utf-8")
    )
    original_config = json.loads(
        (original / "run-config.json").read_text(encoding="utf-8")
    )
    writer_config = json.loads(
        (writer / "run-config.json").read_text(encoding="utf-8")
    )
    assert writer_summary["verified52_ready"] is True
    assert writer_summary["baseline_ready"] is True
    assert writer_summary["formal_original_baseline"] is False
    assert original_config["runtime_fingerprint_sha256"] == writer_config[
        "runtime_fingerprint_sha256"
    ]
    assert original_config["runtime_fingerprint_scope"] == (
        "shared_actor_execution_core"
    )
    assert audit["comparability"]["shared_actor_execution_core_match"] is True
    assert audit["comparability"]["historical_runner_source_match"] is False
    assert audit["comparability"]["descriptive_comparison_ready"] is True
    assert audit["comparability"]["strict_paired_causal_claim_ready"] is False
