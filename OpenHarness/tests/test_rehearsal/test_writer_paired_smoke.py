from __future__ import annotations

import json
from pathlib import Path

from openharness.rehearsal.writer_paired_smoke import (
    HARNESSBENCH_CONTROL_TASK_IDS,
    HARNESSBENCH_TARGET_TASK_IDS,
    MCP_CONTROL_TASK_IDS,
    MCP_TARGET_TASK_IDS,
    load_harnessbench_arm,
    load_mcp_arm,
    summarize_paired_smoke,
)


def _write(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value), encoding="utf-8")


def _mcp_arm(root: Path, *, mode: str, writer_delta: float) -> None:
    tasks = list(MCP_TARGET_TASK_IDS + MCP_CONTROL_TASK_IDS)
    _write(
        root / "run-config.json",
        {
            "tasks": tasks,
            "repeats": 1,
            "experiment_stage": "paired-smoke",
            "openharness_mode": mode,
            "model": "test-model",
        },
    )
    _write(
        root / "baseline-summary.json",
        {
            "run_complete": True,
            "result_count": len(tasks),
            "writer_smoke_gate": {
                "required": mode == "writer_harness",
                "passed": mode == "writer_harness",
            },
        },
    )
    rows = [
        {
            "task_id": task_id,
            "trial": 1,
            "duration_seconds": 12.0 if mode == "writer_harness" else 10.0,
            "usage": {
                "input_tokens": 120 if mode == "writer_harness" else 100,
                "output_tokens": 10,
            },
            "writer": {
                "usage": {
                    "input_tokens": 20 if mode == "writer_harness" else 0,
                    "output_tokens": 5 if mode == "writer_harness" else 0,
                }
            },
            "tool_calls": [{}, {}],
            "state": {"exact_reset": True},
            "local_scores": {
                "execution_score": (0.5 + writer_delta if task_id in MCP_TARGET_TASK_IDS else 1.0)
            },
        }
        for task_id in tasks
    ]
    (root / "results.jsonl").write_text(
        "".join(json.dumps(value) + "\n" for value in rows),
        encoding="utf-8",
    )
    (root / "rehearsal-step-scores-v1.jsonl").write_text(
        "".join(
            json.dumps(
                {
                    "task_id": task_id,
                    "trial": 1,
                    "step_score": (0.4 + writer_delta if task_id in MCP_TARGET_TASK_IDS else 1.0),
                }
            )
            + "\n"
            for task_id in tasks
        ),
        encoding="utf-8",
    )


def _hb_arm(root: Path, *, mode: str, writer_delta: float) -> None:
    tasks = list(HARNESSBENCH_TARGET_TASK_IDS + HARNESSBENCH_CONTROL_TASK_IDS)
    _write(
        root / "run-config.json",
        {
            "tasks": tasks,
            "repeats": 1,
            "openharness_mode": mode,
            "model": "test-model",
        },
    )
    summary_rows = []
    for task_id in tasks:
        result_path = root / "repeat-01/results/openharness-local/test-model" / f"{task_id}.json"
        score = 0.4 + writer_delta if task_id in HARNESSBENCH_TARGET_TASK_IDS else 1.0
        _write(
            result_path,
            {
                "elapsed_sec": (15.0 if mode == "writer_harness" else 10.0),
                "usage_summary": {
                    "total_tokens": (1500 if mode == "writer_harness" else 1000),
                    "request_count": (3 if mode == "writer_harness" else 2),
                },
                "adapter_results": [{"stdout": json.dumps({"tool_calls": 4})}],
            },
        )
        summary_rows.append(
            {
                "task_id": task_id,
                "outcome_score": score,
                "result_file": str(result_path),
            }
        )
    _write(
        root / "baseline-summary.json",
        {
            "baseline_ready": True,
            "result_count": len(tasks),
            "writer_smoke_gate": {
                "required": mode == "writer_harness",
                "passed": mode == "writer_harness",
            },
            "repeats": [{"tasks": summary_rows}],
        },
    )


def test_paired_smoke_summary_finds_target_advantage_without_control_drop(
    tmp_path: Path,
) -> None:
    mcp_original_dir = tmp_path / "mcp-original"
    mcp_writer_dir = tmp_path / "mcp-writer"
    hb_original_dir = tmp_path / "hb-original"
    hb_writer_dir = tmp_path / "hb-writer"
    _mcp_arm(mcp_original_dir, mode="original", writer_delta=0.0)
    _mcp_arm(
        mcp_writer_dir,
        mode="writer_harness",
        writer_delta=0.2,
    )
    _hb_arm(hb_original_dir, mode="original", writer_delta=0.0)
    _hb_arm(
        hb_writer_dir,
        mode="writer_harness",
        writer_delta=0.3,
    )

    summary = summarize_paired_smoke(
        mcp_original=load_mcp_arm(
            mcp_original_dir,
            expected_mode="original",
        ),
        mcp_writer=load_mcp_arm(
            mcp_writer_dir,
            expected_mode="writer_harness",
        ),
        harnessbench_original=load_harnessbench_arm(
            hb_original_dir,
            expected_mode="original",
        ),
        harnessbench_writer=load_harnessbench_arm(
            hb_writer_dir,
            expected_mode="writer_harness",
        ),
    )

    assert summary["stage2_gate_passed"] is True
    assert summary["outcome_delta"]["target_mean"] > 0
    assert summary["outcome_delta"]["control_mean"] == 0
    assert summary["mcp_frozen_rehearsal_step_delta"]["target_mean"] == 0.2
    assert summary["mcp_frozen_rehearsal_step_delta"]["control_mean"] == 0.0
    assert len(summary["advantage_candidates"]) == (
        len(MCP_TARGET_TASK_IDS) + len(HARNESSBENCH_TARGET_TASK_IDS)
    )
    assert summary["resource_delta"]["all_targets"]["total_tokens"]["delta_percent"] > 0
    assert summary["strict_quality_and_cost_advantage_candidates"] == []
    assert len(summary["mcp_rehearsal_step_advantage_candidates"]) == len(MCP_TARGET_TASK_IDS)
    assert summary["effect_claim_ready"] is False
    assert summary["full_exploration_started"] is False
    assert summary["full_exploration_allowed"] is False
