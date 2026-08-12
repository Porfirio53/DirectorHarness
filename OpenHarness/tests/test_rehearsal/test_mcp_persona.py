from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

import pytest

from openharness.rehearsal.mcp_persona import (
    audit_language_alignment,
    build_audit_report,
    discover_simulated_tools,
    extract_annotation_tools,
    run_lark_task1_dependency_probe,
    run_lark_task8_state_smoke,
)


def _write_release(root: Path, tasks: list[dict[str, Any]]) -> None:
    task_dir = root / "data" / "tasks"
    task_dir.mkdir(parents=True)
    for language in ("en", "zh"):
        (task_dir / f"{language}_release_data.json").write_text(
            json.dumps(tasks), encoding="utf-8"
        )


def test_discover_simulated_tools_normalizes_file_prefixes(tmp_path: Path) -> None:
    pycode = tmp_path / "data" / "simulated_tools" / "slack" / "pycode"
    pycode.mkdir(parents=True)
    (pycode / "slack_send_message.py").write_text("", encoding="utf-8")
    (pycode / "dynamic_context_handler.py").write_text("", encoding="utf-8")

    assert discover_simulated_tools(tmp_path) == {"slack": {"slack:send_message"}}


def test_extract_annotation_tools_supports_calls_without_arguments() -> None:
    task = {
        "gt_annotation": {
            "tool_calling_sequence": (
                "1. xiaohongshu:check_login_status\n"
                "2. xiaohongshu:get_login_qrcode: {}"
            )
        }
    }
    assert extract_annotation_tools(task) == (
        "xiaohongshu:check_login_status",
        "xiaohongshu:get_login_qrcode",
    )


def test_build_audit_report_distinguishes_coverage_and_strict_candidates(
    tmp_path: Path,
) -> None:
    pycode = tmp_path / "data" / "simulated_tools" / "demo" / "pycode"
    pycode.mkdir(parents=True)
    (pycode / "demo_lookup.py").write_text("", encoding="utf-8")
    tasks = [
        {
            "id": 1,
            "query_type": "demo_single_short",
            "chains": ["demo:lookup"],
            "sampled_contexts": {"demo": [{"id": "u1", "data": {}}]},
            "gt": [{"checkpoint_type": "personalized_search", "GT_value": "ok"}],
            "gt_annotation": {"tool_calling_sequence": "1. demo:lookup: key=1"},
        },
        {
            "id": 2,
            "query_type": "demo_single_short",
            "chains": ["demo:lookup"],
            "sampled_contexts": {"demo": [{"id": "u1", "data": {}}]},
            "gt": [],
            "gt_annotation": {"tool_calling_sequence": "1. demo:lookup: key=1"},
        },
    ]
    _write_release(tmp_path, tasks)
    eval_dir = tmp_path / "eval"
    eval_dir.mkdir()
    (eval_dir / "checkpoint_eval.py").write_text("", encoding="utf-8")
    (eval_dir / "execution_eval.py").write_text("", encoding="utf-8")

    report = build_audit_report(tmp_path)

    assert report["summary"]["coverage_candidate_task_count"] == 2
    assert report["summary"]["strict_candidate_task_count"] == 1


def test_language_alignment_uses_ids_as_canonical_tasks(tmp_path: Path) -> None:
    tasks = [
        {
            "id": 1,
            "query_type": "demo_single_short",
            "chains": ["demo:lookup"],
        }
    ]
    _write_release(tmp_path, tasks)
    assert audit_language_alignment(tmp_path)["canonical_id_alignment"] is True


def test_real_release_task8_state_smoke() -> None:
    root_value = os_environ_root()
    if root_value is None:
        pytest.skip("MCP_PERSONA_ROOT is not set")
    report = run_lark_task8_state_smoke(root_value)
    assert report["success"] is True


def test_real_release_task1_records_known_dependency_mismatch() -> None:
    root_value = os_environ_root()
    if root_value is None:
        pytest.skip("MCP_PERSONA_ROOT is not set")
    report = run_lark_task1_dependency_probe(root_value)
    assert report["dependency_satisfied"] is False
    assert "User not found" in str(report["observed_error"])


def os_environ_root() -> Path | None:
    value = os.environ.get("MCP_PERSONA_ROOT")
    return Path(value) if value else None
