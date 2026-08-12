from __future__ import annotations

import json
from pathlib import Path

from scripts.summarize_writer_domain_validation import (
    _confirmation,
    _same_config,
    _task_ids,
    _validate_config,
    paired_metric,
    render_markdown,
)


def test_domain_validation_config_locks_minimal_candidate_scope() -> None:
    workspace_root = Path(__file__).resolve().parents[3]
    config = json.loads(
        (workspace_root / "results/config/writer-domain-validation-v1.json").read_text(
            encoding="utf-8"
        )
    )

    _validate_config(config)

    assert config["model"] == config["writer_model"] == "qwen3.6-plus"
    assert config["repeats"] == 1
    assert len(_task_ids(config, "harnessbench")) == 36
    assert _task_ids(config, "mcp_persona") == [92, 93, 125, 141, 173]


def test_paired_metric_confirms_consistent_task_level_gain() -> None:
    task_ids = ["a", "b", "c", "d"]
    original = {
        task_id: {"score": score}
        for task_id, score in zip(task_ids, (0.4, 0.5, 0.6, 0.7))
    }
    writer = {
        task_id: {"score": score}
        for task_id, score in zip(task_ids, (0.5, 0.6, 0.7, 0.8))
    }

    metric = paired_metric(
        task_ids=task_ids,
        original=original,
        writer=writer,
        metric="score",
        bootstrap_samples=2000,
    )
    confirmation = _confirmation(metric, structurally_complete=True)

    assert metric["mean_delta"] == 0.1
    assert metric["bootstrap_95pct_ci"] == {
        "lower": 0.1,
        "upper": 0.1,
        "samples": 2000,
    }
    assert metric["wins"] == 4
    assert confirmation["status"] == "confirmed"
    assert confirmation["confirmed"] is True


def test_pairing_ignores_source_provenance_fields() -> None:
    original = {"tasks": [1], "model": "same", "source_sha256": {"a": "old"}}
    writer = {"tasks": [1], "model": "same", "source_sha256": {"a": "new"}}

    _same_config(original, writer, fields=("tasks", "model"))


def test_markdown_exposes_primary_delta_ci_cost_and_status() -> None:
    summary = {
        "experiment_id": "test",
        "model": "same-model",
        "all_arms_complete": True,
        "domain_results": [
            {
                "dataset": "HarnessBench",
                "domain_label": "候选领域",
                "task_count": 2,
                "primary_metric": "combined_score",
                "confirmation": {"status": "confirmed"},
                "metrics": {
                    "combined_score": {
                        "original_mean": 0.5,
                        "writer_mean": 0.6,
                        "mean_delta": 0.1,
                        "bootstrap_95pct_ci": {"lower": 0.05, "upper": 0.15},
                        "wins": 2,
                        "ties": 0,
                        "losses": 0,
                    },
                    "total_tokens": {"mean_delta_percent": 20.0},
                    "duration_seconds": {"mean_delta_percent": 10.0},
                },
            }
        ],
        "confirmed_domain_ids": ["candidate"],
        "directionally_positive_domain_ids": [],
        "regression_domain_ids": [],
        "limitations": ["one repeat"],
    }

    markdown = render_markdown(summary)

    assert "+10.00 pp" in markdown
    assert "[+5.00 pp, +15.00 pp]" in markdown
    assert "2/0/0" in markdown
    assert "`confirmed`" in markdown
    assert "+20.0%" in markdown
