#!/usr/bin/env python3
"""Preflight and summarize the focused Writer same-model domain validation."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import random
import re
from pathlib import Path
from statistics import mean, median
from typing import Any, Mapping, Sequence

from openharness.rehearsal.writer_handoff import verify_writer_deployment


HB_METRICS = ("combined_score", "outcome_score", "process_score", "security_score")
MCP_METRICS = (
    "rehearsal_aware_combined",
    "execution_score",
    "expected_tool_recall",
    "sequence_score",
    "semantic_checkpoint_score",
)
RESOURCE_METRICS = ("total_tokens", "duration_seconds")
EPSILON = 1e-9


def _read_json(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"JSON file must contain an object: {path}")
    return value


def _read_jsonl(path: Path) -> list[dict[str, Any]]:
    values: list[dict[str, Any]] = []
    for line_number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        if not line.strip():
            continue
        value = json.loads(line)
        if not isinstance(value, dict):
            raise ValueError(f"JSONL row must contain an object: {path}:{line_number}")
        values.append(value)
    return values


def _write_json(path: Path, value: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(value, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _number(value: Any) -> float | None:
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return float(value)
    return None


def _domains(config: Mapping[str, Any], dataset: str) -> list[dict[str, Any]]:
    section = config.get(dataset)
    if not isinstance(section, Mapping) or not isinstance(section.get("domains"), list):
        raise ValueError(f"Config has no {dataset}.domains list")
    domains = [value for value in section["domains"] if isinstance(value, dict)]
    if len(domains) != len(section["domains"]):
        raise ValueError(f"Config contains an invalid {dataset} domain")
    return domains


def _task_ids(config: Mapping[str, Any], dataset: str) -> list[int | str]:
    return [task_id for domain in _domains(config, dataset) for task_id in domain["task_ids"]]


def _validate_config(config: Mapping[str, Any]) -> None:
    if config.get("schema_version") != 1:
        raise ValueError("Domain validation config must use schema_version 1")
    if config.get("model") != config.get("writer_model"):
        raise ValueError("Actor and Writer models must be identical")
    if config.get("repeats") != 1:
        raise ValueError("The minimal confirmatory design is locked to one fresh repeat")
    if config.get("temperature") != 0.0 or config.get("seed") != 42:
        raise ValueError("The confirmatory design is locked to temperature 0 and seed 42")
    if not isinstance(config.get("bootstrap_samples"), int) or config["bootstrap_samples"] < 1000:
        raise ValueError("bootstrap_samples must be an integer of at least 1000")
    for dataset, expected_type in (("harnessbench", str), ("mcp_persona", int)):
        task_ids = _task_ids(config, dataset)
        if not task_ids or len(task_ids) != len(set(task_ids)):
            raise ValueError(f"{dataset} task IDs must be non-empty and unique")
        if not all(isinstance(value, expected_type) for value in task_ids):
            raise ValueError(f"{dataset} task IDs have the wrong type")
        domain_ids = [value.get("id") for value in _domains(config, dataset)]
        if len(domain_ids) != len(set(domain_ids)) or not all(domain_ids):
            raise ValueError(f"{dataset} domain IDs must be non-empty and unique")


def _configured_env_keys(env_file: Path) -> set[str]:
    keys: set[str] = set()
    for line in env_file.read_text(encoding="utf-8", errors="replace").splitlines():
        match = re.match(r"\s*(?:export\s+)?([A-Za-z_][A-Za-z0-9_]*)\s*=", line)
        if match:
            keys.add(match.group(1))
    return keys


def build_preflight(
    *,
    workspace_root: Path,
    env_file: Path,
    config_path: Path,
    result_root: Path,
) -> dict[str, Any]:
    workspace_root = workspace_root.resolve()
    env_file = env_file.resolve()
    config_path = config_path.resolve()
    result_root = result_root.resolve()
    config = _read_json(config_path)
    _validate_config(config)

    openharness_root = workspace_root / "OpenHarness"
    harnessbench_root = workspace_root / "HarnessBench"
    mcp_root = workspace_root / "MCP-Persona"
    python_bin = openharness_root / ".venv/bin/python"
    archive = workspace_root / "docs" / "writer_director_0812.zip"
    required_paths = (
        python_bin,
        env_file,
        harnessbench_root / "src/harnessbench/cli.py",
        mcp_root / "data/tasks/en_release_data.json",
        workspace_root / "results/config/mcp-persona-rehearsal-spec-v1.json",
    )
    missing = [str(path) for path in required_paths if not path.is_file()]
    if missing:
        raise ValueError("Required validation inputs are missing: " + ", ".join(missing))

    hb_missing: list[str] = []
    hb_class_mismatches: list[str] = []
    for domain in _domains(config, "harnessbench"):
        expected_class = str(domain["source_class"])
        for task_id in domain["task_ids"]:
            task_yaml = harnessbench_root / "tasks" / str(task_id) / "task.yaml"
            if not task_yaml.is_file():
                hb_missing.append(str(task_id))
                continue
            match = re.search(
                r'^class:\s*["\']?(.+?)["\']?\s*$',
                task_yaml.read_text(encoding="utf-8"),
                re.MULTILINE,
            )
            actual_class = match.group(1).strip("\"'") if match else None
            if actual_class != expected_class:
                hb_class_mismatches.append(f"{task_id}:{actual_class}")
    if hb_missing or hb_class_mismatches:
        raise ValueError(
            "HarnessBench domain lock is invalid: "
            f"missing={hb_missing}, class_mismatches={hb_class_mismatches}"
        )

    mcp_tasks_value = json.loads(
        (mcp_root / "data/tasks/en_release_data.json").read_text(encoding="utf-8")
    )
    if not isinstance(mcp_tasks_value, list):
        raise ValueError("MCP-Persona English release data is not a list")
    mcp_by_id = {
        int(value["id"]): value
        for value in mcp_tasks_value
        if isinstance(value, Mapping) and isinstance(value.get("id"), int)
    }
    invalid_mcp: list[int] = []
    for raw_task_id in _task_ids(config, "mcp_persona"):
        task_id = int(raw_task_id)
        task = mcp_by_id.get(task_id)
        if task is None:
            invalid_mcp.append(task_id)
            continue
        chains = task.get("chains")
        gt = task.get("gt")
        chain_values = chains if isinstance(chains, list) else []
        servers = {
            value.split(":", 1)[0]
            for value in chain_values
            if isinstance(value, str) and ":" in value
        }
        has_write = isinstance(gt, list) and any(
            isinstance(value, Mapping) and value.get("checkpoint_type") == "operate"
            for value in gt
        )
        if len(servers) < 2 or not has_write:
            invalid_mcp.append(task_id)
    if invalid_mcp:
        raise ValueError(f"MCP task lock contains non-cross-server-write tasks: {invalid_mcp}")

    deployment = verify_writer_deployment(
        workspace_root,
        archive,
        include_support_files=False,
    )
    env_keys = _configured_env_keys(env_file)
    missing_env = [
        name
        for name in ("OPENAI_API_KEY", "OPENAI_API_BASE")
        if not os.environ.get(name) and name not in env_keys
    ]
    if missing_env:
        raise ValueError("Environment file is missing required variable names: " + ", ".join(missing_env))

    hb_count = len(_task_ids(config, "harnessbench"))
    mcp_count = len(_task_ids(config, "mcp_persona"))
    return {
        "schema_version": 1,
        "ready": True,
        "external_model_called": False,
        "experiment_id": config["experiment_id"],
        "config": str(config_path),
        "config_sha256": _sha256(config_path),
        "result_root": str(result_root),
        "model": config["model"],
        "writer_model": config["writer_model"],
        "repeats": config["repeats"],
        "harnessbench": {
            "domain_count": len(_domains(config, "harnessbench")),
            "task_count": hb_count,
            "new_trajectory_count": hb_count * 2,
        },
        "mcp_persona": {
            "domain_count": len(_domains(config, "mcp_persona")),
            "task_count": mcp_count,
            "new_trajectory_count": mcp_count * 2,
        },
        "total_new_trajectory_count": (hb_count + mcp_count) * 2,
        "writer_deployment": {
            "ok": deployment.ok,
            "checked_files": deployment.checked_files,
            "archive_sha256": deployment.archive_sha256,
            "source_lock_enforced": False,
        },
    }


def _writer_tokens_from_hb(payload: Mapping[str, Any]) -> int:
    adapters = payload.get("adapter_results")
    if not isinstance(adapters, list):
        adapter = payload.get("adapter_result")
        adapters = [adapter] if isinstance(adapter, Mapping) else []
    total = 0
    for adapter in adapters:
        if not isinstance(adapter, Mapping) or not isinstance(adapter.get("stdout"), str):
            continue
        try:
            public_result = json.loads(adapter["stdout"])
        except json.JSONDecodeError:
            continue
        usage = public_result.get("writer_usage") if isinstance(public_result, Mapping) else None
        if isinstance(usage, Mapping):
            total += int(usage.get("input_tokens", 0) or 0)
            total += int(usage.get("output_tokens", 0) or 0)
    return total


def _hb_result_path(root: Path, task_id: str, row: Mapping[str, Any]) -> Path | None:
    raw = row.get("result_file")
    if isinstance(raw, str) and Path(raw).is_file():
        return Path(raw)
    matches = sorted(root.glob(f"repeat-01/results/openharness-local/*/{task_id}.json"))
    return matches[0] if len(matches) == 1 else None


def _load_hb_arm(
    root: Path,
    *,
    expected_mode: str,
    expected_tasks: Sequence[str],
    config: Mapping[str, Any],
) -> dict[str, Any]:
    run_config = _read_json(root / "run-config.json")
    summary = _read_json(root / "baseline-summary.json")
    expected_fields = {
        "tasks": list(expected_tasks),
        "repeats": config["repeats"],
        "model": config["model"],
        "temperature": config["temperature"],
        "seed": config["seed"],
        "openharness_mode": expected_mode,
    }
    mismatches = [key for key, value in expected_fields.items() if run_config.get(key) != value]
    grading = run_config.get("grading")
    if not isinstance(grading, Mapping) or grading.get("mode") != "full":
        mismatches.append("grading.mode")
    if mismatches:
        raise ValueError(f"HarnessBench {expected_mode} arm mismatch: {mismatches}")

    repeats = summary.get("repeats")
    rows = repeats[0].get("tasks") if isinstance(repeats, list) and len(repeats) == 1 else None
    if not isinstance(rows, list):
        raise ValueError(f"HarnessBench summary has no one-repeat task rows: {root}")
    indexed = {str(row["task_id"]): row for row in rows if isinstance(row, Mapping)}
    if set(indexed) != set(expected_tasks):
        raise ValueError(f"HarnessBench task coverage mismatch: {root}")

    metrics: dict[str, dict[str, float]] = {}
    imputed_zero: list[str] = []
    for task_id in expected_tasks:
        row = indexed[task_id]
        result_path = _hb_result_path(root, task_id, row)
        payload = _read_json(result_path) if result_path is not None else {}
        scoring = payload.get("scoring")
        scoring = scoring if isinstance(scoring, Mapping) else {}
        usage = payload.get("usage_summary")
        usage = usage if isinstance(usage, Mapping) else {}
        outcome = _number(row.get("outcome_score"))
        combined = _number(row.get("combined_score"))
        if outcome is None or combined is None:
            imputed_zero.append(task_id)
        actor_tokens = int(usage.get("total_tokens", 0) or 0)
        metrics[task_id] = {
            "outcome_score": outcome if outcome is not None else 0.0,
            "combined_score": combined if combined is not None else 0.0,
            "process_score": _number(scoring.get("process_score")) or 0.0,
            "security_score": _number(scoring.get("security_score")) or 0.0,
            "total_tokens": float(actor_tokens + _writer_tokens_from_hb(payload)),
            "duration_seconds": _number(payload.get("elapsed_sec")) or 0.0,
        }
    return {
        "run_config": run_config,
        "summary": summary,
        "metrics": metrics,
        "imputed_zero_tasks": imputed_zero,
        "complete": (
            summary.get("result_count") == len(expected_tasks)
            and summary.get("full_grading_complete") is True
        ),
        "writer_gate_passed": (
            summary.get("writer_smoke_gate", {}).get("passed") is True
            if expected_mode == "writer_harness"
            else True
        ),
    }


def _load_mcp_arm(
    root: Path,
    *,
    expected_mode: str,
    expected_tasks: Sequence[int],
    config: Mapping[str, Any],
) -> dict[str, Any]:
    run_config = _read_json(root / "run-config.json")
    summary = _read_json(root / "baseline-summary.json")
    expected_fields = {
        "tasks": list(expected_tasks),
        "repeats": config["repeats"],
        "model": config["model"],
        "temperature": config["temperature"],
        "seed": config["seed"],
        "openharness_mode": expected_mode,
        "experiment_stage": "paired-smoke",
    }
    mismatches = [key for key, value in expected_fields.items() if run_config.get(key) != value]
    if mismatches:
        raise ValueError(f"MCP-Persona {expected_mode} arm mismatch: {mismatches}")

    results = {(row["task_id"], row["trial"]): row for row in _read_jsonl(root / "results.jsonl")}
    reviews = {
        (row["task_id"], row["trial"]): row
        for row in _read_jsonl(root / "semantic-reviews.jsonl")
    }
    steps = {
        (row["task_id"], row["trial"]): row
        for row in _read_jsonl(root / "rehearsal-step-scores.jsonl")
    }
    expected_keys = {(task_id, 1) for task_id in expected_tasks}
    if set(results) != expected_keys or set(reviews) != expected_keys or set(steps) != expected_keys:
        raise ValueError(f"MCP-Persona result/review/step coverage mismatch: {root}")

    metrics: dict[int, dict[str, float]] = {}
    for task_id in expected_tasks:
        key = (task_id, 1)
        row = results[key]
        local = row.get("local_scores")
        local = local if isinstance(local, Mapping) else {}
        review = reviews[key]
        step = steps[key]
        usage = row.get("usage")
        usage = usage if isinstance(usage, Mapping) else {}
        writer = row.get("writer")
        writer_usage = writer.get("usage") if isinstance(writer, Mapping) else {}
        writer_usage = writer_usage if isinstance(writer_usage, Mapping) else {}
        actor_tokens = int(usage.get("input_tokens", 0) or 0) + int(
            usage.get("output_tokens", 0) or 0
        )
        writer_tokens = int(writer_usage.get("input_tokens", 0) or 0) + int(
            writer_usage.get("output_tokens", 0) or 0
        )
        numeric = {
            "rehearsal_aware_combined": _number(step.get("rehearsal_aware_combined")),
            "execution_score": _number(local.get("execution_score")),
            "expected_tool_recall": _number(local.get("expected_tool_recall")),
            "sequence_score": _number(local.get("sequence_score")),
            "semantic_checkpoint_score": _number(review.get("semantic_checkpoint_score")),
        }
        missing = [name for name, value in numeric.items() if value is None]
        if missing:
            raise ValueError(f"MCP-Persona {task_id} has missing metrics {missing}: {root}")
        metrics[task_id] = {
            **{name: float(value) for name, value in numeric.items() if value is not None},
            "total_tokens": float(actor_tokens + writer_tokens),
            "duration_seconds": _number(row.get("duration_seconds")) or 0.0,
        }
    return {
        "run_config": run_config,
        "summary": summary,
        "metrics": metrics,
        "complete": summary.get("result_count") == len(expected_tasks),
        "writer_gate_passed": (
            summary.get("writer_smoke_gate", {}).get("passed") is True
            if expected_mode == "writer_harness"
            else True
        ),
    }


def _same_config(first: Mapping[str, Any], second: Mapping[str, Any], fields: Sequence[str]) -> None:
    mismatches = [field for field in fields if first.get(field) != second.get(field)]
    if mismatches:
        raise ValueError("Paired arms use different experiment fields: " + ", ".join(mismatches))


def _percentile(values: Sequence[float], fraction: float) -> float:
    if not values:
        raise ValueError("Cannot compute a percentile of an empty sequence")
    position = (len(values) - 1) * fraction
    lower = int(position)
    upper = min(lower + 1, len(values) - 1)
    weight = position - lower
    return values[lower] * (1 - weight) + values[upper] * weight


def paired_metric(
    *,
    task_ids: Sequence[int | str],
    original: Mapping[int | str, Mapping[str, float]],
    writer: Mapping[int | str, Mapping[str, float]],
    metric: str,
    bootstrap_samples: int,
    seed: int = 42,
) -> dict[str, Any]:
    pairs = [(float(original[task_id][metric]), float(writer[task_id][metric])) for task_id in task_ids]
    deltas = [writer_value - original_value for original_value, writer_value in pairs]
    generator = random.Random(seed)
    bootstrap = sorted(
        mean(deltas[generator.randrange(len(deltas))] for _ in deltas)
        for _ in range(bootstrap_samples)
    )
    original_mean = mean(value[0] for value in pairs)
    writer_mean = mean(value[1] for value in pairs)
    return {
        "coverage": len(pairs),
        "original_mean": round(original_mean, 6),
        "writer_mean": round(writer_mean, 6),
        "mean_delta": round(mean(deltas), 6),
        "median_delta": round(median(deltas), 6),
        "bootstrap_95pct_ci": {
            "lower": round(_percentile(bootstrap, 0.025), 6),
            "upper": round(_percentile(bootstrap, 0.975), 6),
            "samples": bootstrap_samples,
        },
        "wins": sum(value > EPSILON for value in deltas),
        "ties": sum(abs(value) <= EPSILON for value in deltas),
        "losses": sum(value < -EPSILON for value in deltas),
        "paired_values": [
            {
                "task_id": task_id,
                "original": round(original_value, 6),
                "writer": round(writer_value, 6),
                "delta": round(writer_value - original_value, 6),
            }
            for task_id, (original_value, writer_value) in zip(task_ids, pairs)
        ],
    }


def _confirmation(metric: Mapping[str, Any], *, structurally_complete: bool) -> dict[str, Any]:
    interval = metric["bootstrap_95pct_ci"]
    mean_positive = metric["mean_delta"] > 0
    lower_positive = interval["lower"] > 0
    wins_greater = metric["wins"] > metric["losses"]
    confirmed = structurally_complete and mean_positive and lower_positive and wins_greater
    if confirmed:
        status = "confirmed"
    elif structurally_complete and mean_positive and wins_greater:
        status = "directionally_positive"
    elif metric["mean_delta"] < 0:
        status = "regression"
    else:
        status = "not_confirmed"
    return {
        "status": status,
        "confirmed": confirmed,
        "structurally_complete": structurally_complete,
        "mean_delta_positive": mean_positive,
        "bootstrap_95pct_lower_bound_positive": lower_positive,
        "wins_greater_than_losses": wins_greater,
    }


def _domain_summary(
    *,
    dataset: str,
    domain: Mapping[str, Any],
    original: Mapping[int | str, Mapping[str, float]],
    writer: Mapping[int | str, Mapping[str, float]],
    primary_metric: str,
    metric_names: Sequence[str],
    bootstrap_samples: int,
    structurally_complete: bool,
) -> dict[str, Any]:
    task_ids = list(domain["task_ids"])
    metrics = {
        metric: paired_metric(
            task_ids=task_ids,
            original=original,
            writer=writer,
            metric=metric,
            bootstrap_samples=bootstrap_samples,
            seed=42 + index,
        )
        for index, metric in enumerate((*metric_names, *RESOURCE_METRICS))
    }
    for resource in RESOURCE_METRICS:
        original_mean = metrics[resource]["original_mean"]
        writer_mean = metrics[resource]["writer_mean"]
        metrics[resource]["mean_delta_percent"] = (
            round((writer_mean / original_mean - 1) * 100, 6) if original_mean else None
        )
    return {
        "dataset": dataset,
        "domain_id": domain["id"],
        "domain_label": domain["label"],
        "task_count": len(task_ids),
        "task_ids": task_ids,
        "primary_metric": primary_metric,
        "confirmation": _confirmation(
            metrics[primary_metric],
            structurally_complete=structurally_complete,
        ),
        "metrics": metrics,
    }


def build_summary(*, config_path: Path, result_root: Path) -> dict[str, Any]:
    config = _read_json(config_path)
    _validate_config(config)
    result_root = result_root.resolve()
    hb_tasks = [str(value) for value in _task_ids(config, "harnessbench")]
    mcp_tasks = [int(value) for value in _task_ids(config, "mcp_persona")]
    hb_original = _load_hb_arm(
        result_root / "HarnessBench/original",
        expected_mode="original",
        expected_tasks=hb_tasks,
        config=config,
    )
    hb_writer = _load_hb_arm(
        result_root / "HarnessBench/writer",
        expected_mode="writer_harness",
        expected_tasks=hb_tasks,
        config=config,
    )
    mcp_original = _load_mcp_arm(
        result_root / "MCP-Persona/original",
        expected_mode="original",
        expected_tasks=mcp_tasks,
        config=config,
    )
    mcp_writer = _load_mcp_arm(
        result_root / "MCP-Persona/writer",
        expected_mode="writer_harness",
        expected_tasks=mcp_tasks,
        config=config,
    )

    _same_config(
        hb_original["run_config"],
        hb_writer["run_config"],
        fields=(
            "tasks",
            "repeats",
            "model",
            "profile",
            "api_format",
            "temperature",
            "seed",
            "max_turns",
            "api_timeout_sec",
            "public_url_mode",
            "grading",
        ),
    )
    _same_config(
        mcp_original["run_config"],
        mcp_writer["run_config"],
        fields=(
            "tasks",
            "repeats",
            "language",
            "model",
            "tool_scope",
            "max_turns",
            "max_tokens",
            "temperature",
            "seed",
            "chain_guidance",
        ),
    )

    arm_status = {
        "harnessbench_original_complete": hb_original["complete"],
        "harnessbench_writer_complete": hb_writer["complete"],
        "harnessbench_writer_gate_passed": hb_writer["writer_gate_passed"],
        "mcp_persona_original_complete": mcp_original["complete"],
        "mcp_persona_writer_complete": mcp_writer["complete"],
        "mcp_persona_writer_gate_passed": mcp_writer["writer_gate_passed"],
    }
    all_arms_complete = all(arm_status.values())
    bootstrap_samples = int(config["bootstrap_samples"])
    domain_results: list[dict[str, Any]] = []
    for domain in _domains(config, "harnessbench"):
        domain_results.append(
            _domain_summary(
                dataset="HarnessBench",
                domain=domain,
                original=hb_original["metrics"],
                writer=hb_writer["metrics"],
                primary_metric=str(config["harnessbench"]["primary_metric"]),
                metric_names=HB_METRICS,
                bootstrap_samples=bootstrap_samples,
                structurally_complete=all_arms_complete,
            )
        )
    for domain in _domains(config, "mcp_persona"):
        domain_results.append(
            _domain_summary(
                dataset="MCP-Persona",
                domain=domain,
                original=mcp_original["metrics"],
                writer=mcp_writer["metrics"],
                primary_metric=str(config["mcp_persona"]["primary_metric"]),
                metric_names=MCP_METRICS,
                bootstrap_samples=bootstrap_samples,
                structurally_complete=all_arms_complete,
            )
        )
    confirmed = [value["domain_id"] for value in domain_results if value["confirmation"]["confirmed"]]
    directional = [
        value["domain_id"]
        for value in domain_results
        if value["confirmation"]["status"] == "directionally_positive"
    ]
    regressions = [
        value["domain_id"]
        for value in domain_results
        if value["confirmation"]["status"] == "regression"
    ]
    return {
        "schema_version": 1,
        "result_scope": (
            "Fresh same-model paired domain confirmation; local OpenHarness-compatible scores, "
            "not official benchmark submissions"
        ),
        "experiment_id": config["experiment_id"],
        "config": str(config_path.resolve()),
        "config_sha256": _sha256(config_path),
        "model": config["model"],
        "writer_model": config["writer_model"],
        "repeats": config["repeats"],
        "all_arms_complete": all_arms_complete,
        "arm_status": arm_status,
        "harnessbench_zero_imputations": {
            "original": hb_original["imputed_zero_tasks"],
            "writer": hb_writer["imputed_zero_tasks"],
        },
        "confirmation_rule": config["confirmation_rule"],
        "domain_results": domain_results,
        "confirmed_domain_ids": confirmed,
        "directionally_positive_domain_ids": directional,
        "regression_domain_ids": regressions,
        "all_candidate_domains_confirmed": (
            all_arms_complete and len(confirmed) == len(domain_results)
        ),
        "limitations": [
            "This is one fresh repeat per task; uncertainty is bootstrapped across tasks, not run repeats.",
            "The candidate domains were discovered previously, so this is fresh-run confirmation but not a held-out-domain study.",
            "Scores are local OpenHarness-compatible metrics rather than official remote benchmark submissions.",
            "Quality confirmation and efficiency are reported separately; Writer may improve quality while increasing cost.",
        ],
        "source_paths": {
            "result_root": str(result_root),
            "harnessbench_original": str(result_root / "HarnessBench/original"),
            "harnessbench_writer": str(result_root / "HarnessBench/writer"),
            "mcp_persona_original": str(result_root / "MCP-Persona/original"),
            "mcp_persona_writer": str(result_root / "MCP-Persona/writer"),
        },
    }


def _percent(value: float | None) -> str:
    return "—" if value is None else f"{value * 100:.2f}%"


def _delta_points(value: float | None) -> str:
    return "—" if value is None else f"{value * 100:+.2f} pp"


def render_markdown(summary: Mapping[str, Any]) -> str:
    lines = [
        "# Writer 同模型配对领域验证结果",
        "",
        f"- 实验：`{summary['experiment_id']}`",
        f"- 模型：`{summary['model']}`（Original 与 Writer 相同）",
        f"- 四臂完整：`{str(summary['all_arms_complete']).lower()}`",
        "",
        "| Benchmark | 候选领域 | 任务数 | 主指标 | Original | Writer | 配对差值 | 95% CI | 胜/平/负 | 判定 | token 变化 | 时延变化 |",
        "|---|---|---:|---|---:|---:|---:|---:|---:|---|---:|---:|",
    ]
    for domain in summary["domain_results"]:
        primary = domain["metrics"][domain["primary_metric"]]
        interval = primary["bootstrap_95pct_ci"]
        tokens = domain["metrics"]["total_tokens"]["mean_delta_percent"]
        duration = domain["metrics"]["duration_seconds"]["mean_delta_percent"]
        lines.append(
            "| "
            + " | ".join(
                (
                    str(domain["dataset"]),
                    str(domain["domain_label"]),
                    str(domain["task_count"]),
                    f"`{domain['primary_metric']}`",
                    _percent(primary["original_mean"]),
                    _percent(primary["writer_mean"]),
                    _delta_points(primary["mean_delta"]),
                    (
                        f"[{_delta_points(interval['lower'])}, "
                        f"{_delta_points(interval['upper'])}]"
                    ),
                    f"{primary['wins']}/{primary['ties']}/{primary['losses']}",
                    f"`{domain['confirmation']['status']}`",
                    "—" if tokens is None else f"{tokens:+.1f}%",
                    "—" if duration is None else f"{duration:+.1f}%",
                )
            )
            + " |"
        )
    lines.extend(
        [
            "",
            "## 判定汇总",
            "",
            f"- 已确认领域：{', '.join(summary['confirmed_domain_ids']) or '无'}",
            (
                "- 仅方向性为正："
                + (", ".join(summary["directionally_positive_domain_ids"]) or "无")
            ),
            f"- 回归领域：{', '.join(summary['regression_domain_ids']) or '无'}",
            "",
            "确认标准：主指标平均差值大于 0、任务级 bootstrap 95% 区间下界大于 0，且胜题数多于负题数。",
            "",
            "## 限制",
            "",
            *[f"- {value}" for value in summary["limitations"]],
            "",
        ]
    )
    return "\n".join(lines)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)
    preflight = subparsers.add_parser("preflight")
    preflight.add_argument("--workspace-root", type=Path, required=True)
    preflight.add_argument("--env-file", type=Path, required=True)
    preflight.add_argument("--config", type=Path, required=True)
    preflight.add_argument("--result-root", type=Path, required=True)
    preflight.add_argument("--output", type=Path, required=True)
    summarize = subparsers.add_parser("summarize")
    summarize.add_argument("--config", type=Path, required=True)
    summarize.add_argument("--result-root", type=Path, required=True)
    summarize.add_argument("--output-json", type=Path, required=True)
    summarize.add_argument("--output-markdown", type=Path, required=True)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    if args.command == "preflight":
        result = build_preflight(
            workspace_root=args.workspace_root,
            env_file=args.env_file,
            config_path=args.config,
            result_root=args.result_root,
        )
        _write_json(args.output, result)
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0
    summary = build_summary(config_path=args.config, result_root=args.result_root)
    _write_json(args.output_json, summary)
    args.output_markdown.parent.mkdir(parents=True, exist_ok=True)
    args.output_markdown.write_text(render_markdown(summary), encoding="utf-8")
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return 0 if summary["all_arms_complete"] else 2


if __name__ == "__main__":
    raise SystemExit(main())
