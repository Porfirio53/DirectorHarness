from __future__ import annotations

import json
import re
from pathlib import Path

from openharness.rehearsal.mcp_persona_rehearsal import VERIFIED_TASK_IDS
from scripts.merge_writer_full_repeats import _validate_compatibility
from scripts.summarize_writer_full_results import build_summary


def _write_json(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value), encoding="utf-8")


def test_full_shell_commands_lock_task_scopes_model_writer_and_resume() -> None:
    openharness_root = Path(__file__).resolve().parents[2]
    for relative in (
        "scripts/run_writer_mcp_full.sh",
        "scripts/score_writer_full_results.sh",
    ):
        text = (openharness_root / relative).read_text(encoding="utf-8")
        match = re.search(r"task_ids=\(\s*(.*?)\s*\)", text, re.DOTALL)
        assert match is not None
        assert tuple(int(value) for value in match.group(1).split()) == VERIFIED_TASK_IDS
    mcp_command = (
        openharness_root / "scripts/run_writer_mcp_full.sh"
    ).read_text(encoding="utf-8")
    hb_command = (
        openharness_root / "scripts/run_writer_harnessbench_full.sh"
    ).read_text(encoding="utf-8")
    for text in (mcp_command, hb_command):
        assert 'model="qwen3.6-plus"' in text
        assert "--openharness-mode writer_harness" in text
        assert '--writer-model "${model}"' in text
        assert "resume_args" in text
    assert "--experiment-stage writer-full" in mcp_command
    assert "--repeats 1" in mcp_command
    assert "harnessbench-writer-full-v1.tasks.json" in hb_command
    assert "--grading full" in hb_command
    assert "--repeats 1" in hb_command
    second_repeat_command = (
        openharness_root / "scripts/run_writer_second_repeat.sh"
    ).read_text(encoding="utf-8")
    assert "run_writer_mcp_full.sh" in second_repeat_command
    assert "run_writer_harnessbench_full.sh" in second_repeat_command
    assert "merge_writer_full_repeats.py" in second_repeat_command


def test_merge_does_not_lock_repository_or_source_hashes() -> None:
    mcp_common = {
        "tasks": [1],
        "language": "en",
        "mode": "full",
        "model": "test-model",
        "tool_scope": "server",
        "max_turns": 30,
        "max_tokens": 4096,
        "temperature": 0.0,
        "seed": 42,
        "chain_guidance": False,
        "experiment_stage": "writer-full",
        "openharness_mode": "writer_harness",
        "writer_model": "test-model",
        "writer_max_tokens": 4096,
    }
    hb_common = {
        "dataset_id": "test",
        "tasks": ["001-file"],
        "excluded_tasks": [],
        "model": "test-model",
        "profile": "qwen",
        "api_format": "openai",
        "temperature": 0.0,
        "seed": 42,
        "max_turns": 80,
        "api_timeout_sec": 300,
        "openharness_mode": "writer_harness",
        "writer_model": "test-model",
        "writer_max_tokens": 4096,
        "public_url_mode": "loopback",
        "grading": {"mode": "full"},
    }

    _validate_compatibility(
        {**mcp_common, "source_sha256": {"runner": "old"}},
        {**mcp_common, "source_sha256": {"runner": "new"}},
        {**hb_common, "harnessbench_git": {"commit": "old"}},
        {**hb_common, "harnessbench_git": {"commit": "new"}},
    )


def test_build_summary_combines_full_scores_tokens_and_latency(
    tmp_path: Path,
) -> None:
    mcp_output = tmp_path / "mcp"
    hb_output = tmp_path / "hb"
    mcp_output.mkdir()
    hb_output.mkdir()

    _write_json(
        mcp_output / "run-config.json",
        {
            "writer_full_verified52": True,
            "repeats": 1,
            "model": "qwen3.6-plus",
            "writer_model": "qwen3.6-plus",
            "openharness_mode": "writer_harness",
        },
    )
    _write_json(
        mcp_output / "baseline-summary.json",
        {
            "run_complete": True,
            "local_score_means": {
                "execution_score": 0.75,
                "sequence_score": 0.8,
            },
            "writer_smoke_gate": {"passed": True},
        },
    )
    _write_json(
        mcp_output / "semantic-summary.json",
        {
            "expected_task_count": 52,
            "expected_trial_count": 52,
            "semantic_review_ready": True,
            "semantic_score_means": {
                "checkpoint_micro": 0.7,
                "trial_macro": 0.65,
                "task_macro": 0.6,
            },
        },
    )
    _write_json(
        mcp_output / "rehearsal-step-summary.json",
        {
            "expected_task_count": 52,
            "expected_trial_count": 52,
            "structurally_complete": True,
            "semantic_review_complete": True,
            "step_score": {"coverage": 52, "mean": 0.8},
            "rehearsal_aware_combined": {"coverage": 45, "mean": 0.7},
        },
    )
    (mcp_output / "results.jsonl").write_text(
        "".join(
            json.dumps(
                {
                    "task_id": task_id,
                    "trial": 1,
                    "usage": {"input_tokens": 10, "output_tokens": 2},
                    "writer": {"usage": {"input_tokens": 3, "output_tokens": 4}},
                    "duration_seconds": 1.5,
                }
            )
            + "\n"
            for task_id in VERIFIED_TASK_IDS
        ),
        encoding="utf-8",
    )

    hb_tasks = [f"{value:03d}-task" for value in range(1, 107)]
    _write_json(
        hb_output / "run-config.json",
        {
            "dataset_id": "harnessbench-writer-full-v1",
            "tasks": hb_tasks,
            "repeats": 1,
            "model": "qwen3.6-plus",
            "writer_model": "qwen3.6-plus",
            "openharness_mode": "writer_harness",
            "grading": {"mode": "full"},
        },
    )
    hb_rows: list[dict[str, object]] = []
    for task_id in hb_tasks:
        result_file = hb_output / "results" / f"{task_id}.json"
        _write_json(
            result_file,
            {
                "oracle_result": {
                    "outcome_score": 0.8,
                    "quality": 0.7,
                },
                "scoring": {
                    "process_score": 0.9,
                    "security_score": 1.0,
                    "combined_score": 0.85,
                },
                "usage_summary": {
                    "input_tokens": 100,
                    "output_tokens": 20,
                },
                "adapter_results": [
                    {
                        "stdout": json.dumps(
                            {
                                "writer_usage": {
                                    "input_tokens": 5,
                                    "output_tokens": 6,
                                }
                            }
                        )
                    }
                ],
                "elapsed_sec": 2.0,
            },
        )
        hb_rows.append({"task_id": task_id, "result_file": str(result_file)})
    _write_json(
        hb_output / "baseline-summary.json",
        {
            "expected_results": 106,
            "result_count": 106,
            "full_grading_complete": True,
            "baseline_ready": False,
            "writer_smoke_gate": {"passed": False},
            "state_counts": {
                "completed": 104,
                "openharness_process_failed": 2,
            },
            "repeats": [{"repeat": 1, "tasks": hb_rows}],
        },
    )

    summary = build_summary(
        mcp_output=mcp_output,
        harnessbench_output=hb_output,
    )

    assert summary["all_scoring_complete"] is True
    assert summary["harnessbench"]["run_and_full_grading_complete"] is True
    assert summary["harnessbench"]["execution_gate_passed"] is False
    assert summary["harnessbench"]["writer_gate_passed"] is False
    assert summary["harnessbench"]["execution_failure_count"] == 2
    assert summary["mcp_persona"]["final_score"]["mean"] == 0.75
    assert summary["mcp_persona"]["process_score"]["mean"] == 0.8
    assert summary["mcp_persona"]["semantic_score"]["task_macro"] == 0.6
    assert summary["mcp_persona"]["resources"]["tokens"] == {
        "actor": {"input": 520, "output": 104, "total": 624},
        "writer": {"input": 156, "output": 208, "total": 364},
        "combined_total": 988,
    }
    hb_metrics = summary["harnessbench"]["benchmark_scores_and_resources"]
    assert hb_metrics["scores"]["combined"]["mean"] == 0.85
    assert hb_metrics["scores"]["combined"]["coverage"] == 106
    assert hb_metrics["tokens"]["combined_total"] == 13_886
    assert hb_metrics["latency_seconds"]["total"] == 212.0
