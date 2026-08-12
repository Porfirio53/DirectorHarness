from __future__ import annotations

import os
from pathlib import Path

import pytest

from openharness.rehearsal.mcp_persona_rehearsal import (
    NO_PUBLIC_CHECKPOINT_TASK_IDS,
    VERIFIED_TASK_IDS,
    build_rehearsal_spec,
)
from scripts.summarize_mcp_persona_verified52 import (
    _index_exact,
    build_baseline1_summary,
)


def _mcp_root() -> Path:
    configured = os.environ.get("MCP_PERSONA_ROOT")
    root = (
        Path(configured)
        if configured
        else Path(__file__).resolve().parents[3] / "MCP-Persona"
    )
    if not (root / "data/tasks/en_release_data.json").is_file():
        pytest.skip("MCP-Persona checkout is unavailable")
    return root.resolve()


def _run_config() -> dict[str, object]:
    return {
        "dataset_id": "mcp-persona-verified52",
        "formal_verified52": True,
        "tasks": list(VERIFIED_TASK_IDS),
        "repeats": 2,
        "language": "en",
        "tool_scope": "server",
        "chain_guidance": False,
        "model": "test-model",
        "temperature": 0,
        "seed": 42,
    }


def test_baseline1_summary_keeps_one_unified_52_task_dataset() -> None:
    spec = build_rehearsal_spec(_mcp_root())
    no_checkpoint = set(NO_PUBLIC_CHECKPOINT_TASK_IDS)
    results = []
    reviews = []
    for task_id in VERIFIED_TASK_IDS:
        for trial in (1, 2):
            results.append(
                {
                    "task_id": task_id,
                    "trial": trial,
                    "run_status": {
                        "detail": "completed",
                        "baseline_valid": True,
                    },
                    "state": {"exact_reset": True},
                    "tool_calls": [{"is_error": False}],
                    "usage": {"input_tokens": 10, "output_tokens": 2},
                    "duration_seconds": 1.0,
                    "local_scores": {
                        "local_pass": task_id % 2 == 0,
                        "execution_score": 0.75,
                        "sequence_score": 0.5,
                        "expected_tool_recall": 1.0,
                    },
                }
            )
            has_checkpoint = task_id not in no_checkpoint
            reviews.append(
                {
                    "task_id": task_id,
                    "trial": trial,
                    "checkpoint_available": has_checkpoint,
                    "semantic_checkpoint_score": 0.8 if has_checkpoint else None,
                    "semantic_task_complete": False if has_checkpoint else None,
                    "fully_resolved": True,
                    "judge_error": None,
                }
            )

    summary = build_baseline1_summary(
        results,
        reviews,
        run_config=_run_config(),
        spec=spec,
    )

    assert summary["task_count"] == 52
    assert summary["unique_trial_count"] == 104
    assert summary["overall"]["semantic_checkpoint"]["coverage"] == 90
    assert summary["baseline_ready"] is True
    assert "stable_task_ids" not in summary


def test_baseline1_exact_index_rejects_duplicate() -> None:
    row = {"task_id": VERIFIED_TASK_IDS[0], "trial": 1}

    with pytest.raises(ValueError, match="duplicate"):
        _index_exact([row, row], label="results")
