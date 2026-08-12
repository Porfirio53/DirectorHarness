#!/usr/bin/env python3
"""Review MCP-Persona checkpoints with deterministic rules plus an LLM judge."""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import os
import re
import subprocess
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping, Sequence
from urllib.parse import urlparse

from dotenv import load_dotenv
from openai import AsyncOpenAI

from openharness.rehearsal.mcp_persona_rehearsal import (
    VERIFIED52_PROTOCOL_ID,
    VERIFIED52_WRITER_ARM,
    VERIFIED_TASK_IDS,
    verified52_experiment_arm,
)
from openharness.rehearsal.mcp_persona_runtime import (
    directory_diff_summary,
    evaluate_checkpoint_rules,
    task_by_id,
)

RESULT_LABEL = (
    "OpenHarness local semantic checkpoint review; "
    "not an official MCP-Persona score"
)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--mcp-persona-root", type=Path, required=True)
    parser.add_argument("--results", type=Path, required=True)
    parser.add_argument("--summary", type=Path)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--summary-output", type=Path, required=True)
    parser.add_argument("--env-file", type=Path)
    parser.add_argument("--language", choices=("en", "zh"), default="en")
    parser.add_argument("--model", default="qwen3.6-plus")
    parser.add_argument("--task-ids", type=int, nargs="*")
    parser.add_argument("--repeats", type=int, default=2)
    parser.add_argument(
        "--require-verified52",
        action="store_true",
        help="Require the canonical 52 task IDs, two trials each, before judging",
    )
    parser.add_argument("--api-timeout", type=float, default=120.0)
    parser.add_argument("--judge-attempts", type=int, default=3)
    parser.add_argument("--judge-retry-delay", type=float, default=1.0)
    parser.add_argument("--max-evidence-chars", type=int, default=24_000)
    parser.add_argument("--resume", action="store_true")
    parser.add_argument(
        "--reuse-judge-from",
        type=Path,
        help="Re-evaluate current rules and reuse prior per-checkpoint LLM Judge decisions",
    )
    parser.add_argument(
        "--rules-only",
        action="store_true",
        help="Run deterministic checkpoint rules without accessing an LLM API",
    )
    return parser.parse_args()


def _write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.tmp")
    temporary.write_text(
        json.dumps(value, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    temporary.replace(path)


def _write_jsonl(path: Path, values: Sequence[Mapping[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(
        "".join(
            json.dumps(value, ensure_ascii=False, default=str) + "\n"
            for value in values
        ),
        encoding="utf-8",
    )
    temporary.replace(path)


def _load_rows(path: Path) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for line_number, line in enumerate(
        path.read_text(encoding="utf-8").splitlines(),
        1,
    ):
        if not line.strip():
            continue
        value = json.loads(line)
        if not isinstance(value, dict):
            raise ValueError(f"Invalid JSON object at {path}:{line_number}")
        rows.append(value)
    return rows


def _selected_task_ids(
    args: argparse.Namespace,
    rows: Sequence[Mapping[str, Any]],
) -> tuple[int, ...]:
    if args.task_ids:
        task_ids = tuple(int(value) for value in args.task_ids)
    else:
        task_ids = tuple(
            sorted(
                {
                    int(row["task_id"])
                    for row in rows
                    if isinstance(row.get("task_id"), int)
                }
            )
        )
    if not task_ids:
        raise ValueError("No task IDs were selected")
    if len(task_ids) != len(set(task_ids)):
        raise ValueError("Selected task IDs contain duplicates")
    return task_ids


def _index_rows(
    rows: Sequence[Mapping[str, Any]],
    *,
    task_ids: Sequence[int],
    repeats: int,
    source: Path,
    require_complete: bool,
    reject_unselected: bool = False,
) -> dict[tuple[int, int], dict[str, Any]]:
    allowed_tasks = set(task_ids)
    indexed: dict[tuple[int, int], dict[str, Any]] = {}
    for row in rows:
        if not isinstance(row.get("task_id"), int) or not isinstance(
            row.get("trial"), int
        ):
            raise ValueError(f"{source} has a row without integer task_id/trial")
        task_id = int(row["task_id"])
        trial = int(row["trial"])
        if task_id not in allowed_tasks:
            if reject_unselected:
                raise ValueError(f"{source} contains unselected task {task_id}")
            continue
        if trial < 1 or trial > repeats:
            raise ValueError(
                f"{source} contains task {task_id} trial {trial}, "
                f"outside repeats={repeats}"
            )
        key = (task_id, trial)
        if key in indexed:
            raise ValueError(
                f"{source} contains duplicate task {task_id} trial {trial}"
            )
        indexed[key] = dict(row)
    expected = {
        (task_id, trial)
        for task_id in task_ids
        for trial in range(1, repeats + 1)
    }
    missing = sorted(expected - set(indexed))
    if require_complete and missing:
        preview = ", ".join(f"{task_id}:{trial}" for task_id, trial in missing[:10])
        raise ValueError(
            f"{source} is incomplete for the locked semantic review; "
            f"missing {len(missing)} slots ({preview})"
        )
    return indexed


def _sha256_file(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _git_commit(root: Path) -> str | None:
    completed = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=root,
        text=True,
        capture_output=True,
        check=False,
    )
    return completed.stdout.strip() if completed.returncode == 0 else None


def _review_config(
    args: argparse.Namespace,
    *,
    task_ids: Sequence[int],
    api_base: str | None,
    source_arm: str | None,
) -> dict[str, Any]:
    endpoint = urlparse(api_base) if api_base else None
    root = args.mcp_persona_root.resolve()
    results = args.results.resolve()
    runtime_source = (
        Path(__file__).resolve().parents[1]
        / "src/openharness/rehearsal/mcp_persona_runtime.py"
    )
    task_file = root / "data/tasks" / f"{args.language}_release_data.json"
    reuse = args.reuse_judge_from.resolve() if args.reuse_judge_from else None
    return {
        "schema_version": 1,
        "result_label": RESULT_LABEL,
        "dataset_id": (
            "mcp-persona-verified52-writer-full"
            if source_arm == VERIFIED52_WRITER_ARM
            else "mcp-persona-verified52"
            if source_arm is not None
            else "mcp-persona-development-selection"
        ),
        "protocol_id": (
            VERIFIED52_PROTOCOL_ID if source_arm is not None else None
        ),
        "experiment_arm": source_arm,
        "require_verified52": args.require_verified52,
        "tasks": list(task_ids),
        "repeats": args.repeats,
        "language": args.language,
        "model": args.model,
        "rules_only": args.rules_only,
        "api_timeout_seconds": args.api_timeout,
        "judge_attempts": args.judge_attempts,
        "judge_retry_delay_seconds": args.judge_retry_delay,
        "max_evidence_chars": args.max_evidence_chars,
        "results": str(results),
        "results_sha256": _sha256_file(results),
        "reuse_judge_from": str(reuse) if reuse else None,
        "reuse_judge_sha256": _sha256_file(reuse) if reuse else None,
        "mcp_persona_root": str(root),
        "mcp_persona_commit": _git_commit(root),
        "openharness_commit": _git_commit(Path(__file__).resolve().parents[1]),
        "model_endpoint": (
            {
                "scheme": endpoint.scheme,
                "api_base_host": endpoint.hostname,
                "api_base_path": endpoint.path,
            }
            if endpoint
            else None
        ),
        "source_sha256": {
            "review_script": _sha256_file(Path(__file__).resolve()),
            "runtime": _sha256_file(runtime_source),
            "task_file": _sha256_file(task_file),
            "run_config": (
                _sha256_file(results.with_name("run-config.json"))
                if results.with_name("run-config.json").is_file()
                else None
            ),
            "baseline_summary": (
                _sha256_file(results.with_name("baseline-summary.json"))
                if results.with_name("baseline-summary.json").is_file()
                else None
            ),
        },
    }


def _prepare_review_config(
    output: Path,
    requested: dict[str, Any],
    *,
    resume: bool,
) -> dict[str, Any]:
    config_path = output.with_name("semantic-run-config.json")
    if config_path.is_file():
        existing = json.loads(config_path.read_text(encoding="utf-8"))
        if not isinstance(existing, dict):
            raise ValueError(f"Invalid semantic review config: {config_path}")
        comparable = sorted((set(existing) | set(requested)) - {"created_at"})
        mismatches = [
            key for key in comparable if existing.get(key) != requested.get(key)
        ]
        if mismatches:
            raise ValueError(
                "semantic output belongs to a different locked review; "
                "mismatched fields: " + ", ".join(mismatches)
            )
        if not resume:
            raise ValueError(
                f"semantic review already exists; pass --resume: {config_path}"
            )
        return existing
    if output.is_file() and output.stat().st_size:
        raise ValueError(
            f"refusing to resume semantic rows without a config lock: {output}"
        )
    requested["created_at"] = datetime.now(timezone.utc).isoformat()
    _write_json(config_path, requested)
    return requested


def _canonical_sha256(value: Any) -> str:
    return hashlib.sha256(
        json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            default=str,
        ).encode("utf-8")
    ).hexdigest()


def _config_sha256(config: Mapping[str, Any]) -> str:
    return _canonical_sha256(
        {key: value for key, value in config.items() if key != "created_at"}
    )


def _checkpoint_sha256(task: Mapping[str, Any]) -> str:
    checkpoints = task.get("gt")
    return _canonical_sha256(checkpoints if isinstance(checkpoints, list) else [])


def _validate_verified52_source(
    args: argparse.Namespace,
    task_ids: Sequence[int],
) -> str | None:
    if not args.require_verified52:
        return None
    run_config_path = args.results.with_name("run-config.json")
    summary_path = args.results.with_name("baseline-summary.json")
    if not run_config_path.is_file() or not summary_path.is_file():
        raise ValueError(
            "Verified52 semantic review requires run-config.json and "
            "baseline-summary.json next to results.jsonl"
        )
    run_config = json.loads(run_config_path.read_text(encoding="utf-8"))
    baseline_summary = json.loads(summary_path.read_text(encoding="utf-8"))
    if not isinstance(run_config, Mapping) or not isinstance(
        baseline_summary, Mapping
    ):
        raise ValueError("Verified52 source metadata is invalid")
    arm = verified52_experiment_arm(run_config)
    if arm is None or run_config.get("tasks") != list(task_ids):
        raise ValueError(
            "Verified52 source run is not an exact Original or Writer 52x2 arm"
        )
    if (
        run_config.get("protocol_id") != VERIFIED52_PROTOCOL_ID
        or run_config.get("experiment_arm") != arm
        or baseline_summary.get("protocol_id") != VERIFIED52_PROTOCOL_ID
        or baseline_summary.get("experiment_arm") != arm
        or baseline_summary.get("verified52_ready") is not True
        or baseline_summary.get("baseline_ready") is not True
        or baseline_summary.get("expected_results") != 104
        or baseline_summary.get("valid_result_count") != 104
    ):
        raise ValueError(
            "Verified52 source baseline is incomplete; resume the Agent run first"
        )
    if arm == VERIFIED52_WRITER_ARM:
        writer_gate = baseline_summary.get("writer_smoke_gate")
        if not isinstance(writer_gate, Mapping) or writer_gate.get("passed") is not True:
            raise ValueError("Writer Verified52 source did not pass the Writer gate")
    return str(arm)


def _state_evidence(row: Mapping[str, Any]) -> str:
    state = row.get("state")
    if not isinstance(state, Mapping):
        return ""
    existing = state.get("diff_summary")
    if isinstance(existing, str):
        return existing
    initial_value = state.get("initial_dir")
    final_value = state.get("final_dir")
    if not initial_value or not final_value:
        return ""
    initial = Path(str(initial_value))
    final = Path(str(final_value))
    if initial.is_dir() and final.is_dir():
        return str(directory_diff_summary(initial, final))
    return ""


def _compact_calls(calls: Sequence[Mapping[str, Any]], max_chars: int) -> str:
    values = []
    for index, call in enumerate(calls, 1):
        values.append(
            {
                "step": index,
                "tool_name": call.get("tool_name"),
                "input": call.get("input"),
                "is_error": call.get("is_error"),
                "simulator_error": call.get("simulator_error"),
                "output": str(call.get("raw_output") or call.get("output", ""))[:6_000],
            }
        )
    text = json.dumps(values, ensure_ascii=False, indent=2, default=str)
    return text[:max_chars]


def _extract_json_object(text: str) -> dict[str, Any]:
    value = text.strip()
    if value.startswith("```"):
        value = re.sub(r"^```(?:json)?\s*|\s*```$", "", value, flags=re.DOTALL)
    try:
        parsed = json.loads(value)
    except json.JSONDecodeError:
        match = re.search(r"\{.*\}", value, flags=re.DOTALL)
        if match is None:
            raise
        parsed = json.loads(match.group(0))
    if not isinstance(parsed, dict):
        raise ValueError("Judge response is not a JSON object")
    return parsed


def _normalize_judge_decision_indices(
    decisions: Mapping[int, Mapping[str, Any]],
    requested_indices: Sequence[int],
) -> dict[int, dict[str, Any]]:
    """Accept exact checkpoint indices or an unambiguous zero-based renumbering."""

    requested = list(requested_indices)
    if len(requested) != len(set(requested)):
        raise ValueError("Requested checkpoint indices contain duplicates")
    returned = set(decisions)
    requested_set = set(requested)
    if returned == requested_set:
        return {index: dict(decisions[index]) for index in requested}
    relative = set(range(len(requested)))
    if returned == relative and len(decisions) == len(requested):
        return {
            requested[position]: {
                **dict(decisions[position]),
                "index_normalization": (
                    "zero_based_response_position_to_checkpoint_index"
                ),
            }
            for position in range(len(requested))
        }
    missing = sorted(requested_set - returned)
    unexpected = sorted(returned - requested_set)
    raise ValueError(
        "Judge decision indices do not match requested checkpoints; "
        f"missing={missing}, unexpected={unexpected}"
    )


async def _judge_unresolved(
    client: AsyncOpenAI,
    *,
    model: str,
    task: Mapping[str, Any],
    row: Mapping[str, Any],
    unresolved: Sequence[Mapping[str, Any]],
    state_evidence: str,
    max_evidence_chars: int,
) -> dict[int, dict[str, Any]]:
    checkpoints = [value for value in task.get("gt", []) if isinstance(value, Mapping)]
    requested_indices = [int(detail["index"]) for detail in unresolved]
    requested = [
        {"index": index, "checkpoint": checkpoints[index]}
        for index in requested_indices
    ]
    payload = {
        "task_instruction": task.get("instruction"),
        "checkpoints_to_review": requested,
        "tool_calls": _compact_calls(row.get("tool_calls", []), max_evidence_chars),
        "final_answer": str(row.get("final_answer", ""))[:6_000],
        "state_changed": bool(row.get("state", {}).get("state_changed")),
        "state_diff": state_evidence[:max_evidence_chars],
    }
    response_shape = {
        "decisions": [
            {
                "index": index,
                "pass": True,
                "confidence": 0.0,
                "reason": "brief evidence-based reason",
            }
            for index in requested_indices
        ]
    }
    system_prompt = (
        "You are a strict, evidence-only evaluator for an isolated MCP task. "
        "For each requested checkpoint, decide whether the recorded tool calls and resulting state "
        "prove that checkpoint was satisfied. A successful tool name alone is insufficient when the "
        "checkpoint requires particular arguments, content, recipients, objects, or search facts. "
        "Do not infer missing evidence. Return exactly one decision for every requested checkpoint. "
        f"The required checkpoint indices are {requested_indices}; copy those exact integers and "
        "do not renumber them. Return JSON only, following this exact index layout: "
        f"{json.dumps(response_shape, ensure_ascii=False)}"
    )
    response = await client.chat.completions.create(
        model=model,
        messages=[
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": json.dumps(payload, ensure_ascii=False, default=str)},
        ],
        temperature=0,
        seed=42,
        max_tokens=2_048,
        extra_body={"enable_thinking": False},
    )
    content = response.choices[0].message.content or ""
    parsed = _extract_json_object(content)
    decisions = parsed.get("decisions")
    if not isinstance(decisions, list):
        raise ValueError("Judge response has no decisions list")
    result: dict[int, dict[str, Any]] = {}
    for value in decisions:
        if not isinstance(value, Mapping):
            raise ValueError("Judge decision has no integer index")
        raw_index = value.get("index")
        if not isinstance(raw_index, int):
            raise ValueError("Judge decision has no integer index")
        index = raw_index
        if index in result:
            raise ValueError(f"Judge returned duplicate checkpoint index {index}")
        passed = value.get("pass")
        confidence = value.get("confidence")
        reason = value.get("reason")
        if not isinstance(passed, bool):
            raise ValueError(f"Judge pass for checkpoint {index} is not boolean")
        if (
            not isinstance(confidence, (int, float))
            or isinstance(confidence, bool)
            or not 0 <= float(confidence) <= 1
        ):
            raise ValueError(f"Judge confidence for checkpoint {index} is invalid")
        if not isinstance(reason, str) or not reason.strip():
            raise ValueError(f"Judge reason for checkpoint {index} is empty")
        result[index] = {
            "pass": passed,
            "confidence": float(confidence),
            "reason": reason.strip(),
        }
    return _normalize_judge_decision_indices(result, requested_indices)


async def _judge_with_retries(
    client: AsyncOpenAI,
    *,
    attempts: int,
    retry_delay: float,
    model: str,
    task: Mapping[str, Any],
    row: Mapping[str, Any],
    unresolved: Sequence[Mapping[str, Any]],
    state_evidence: str,
    max_evidence_chars: int,
) -> tuple[dict[int, dict[str, Any]], str | None, int]:
    last_error: str | None = None
    for attempt in range(1, attempts + 1):
        try:
            decisions = await _judge_unresolved(
                client,
                model=model,
                task=task,
                row=row,
                unresolved=unresolved,
                state_evidence=state_evidence,
                max_evidence_chars=max_evidence_chars,
            )
            return decisions, None, attempt
        except Exception as exc:
            last_error = f"{type(exc).__name__}: {exc}"
            if attempt < attempts and retry_delay > 0:
                await asyncio.sleep(retry_delay * (2 ** (attempt - 1)))
    return {}, last_error, attempts


async def async_main(args: argparse.Namespace) -> int:
    if args.repeats < 1:
        raise ValueError("--repeats must be positive")
    if args.judge_attempts < 1:
        raise ValueError("--judge-attempts must be positive")
    if args.judge_retry_delay < 0:
        raise ValueError("--judge-retry-delay cannot be negative")
    if args.env_file and args.env_file.is_file():
        load_dotenv(args.env_file, override=True)
    else:
        load_dotenv()
    api_key = os.environ.get("OPENAI_API_KEY")
    api_base = os.environ.get("OPENAI_API_BASE")
    if not args.rules_only and (not api_key or not api_base):
        raise SystemExit("OPENAI_API_KEY and OPENAI_API_BASE must be configured")

    raw_rows = _load_rows(args.results)
    selected = _selected_task_ids(args, raw_rows)
    if args.require_verified52 and (
        selected != VERIFIED_TASK_IDS or args.repeats != 2
    ):
        raise ValueError(
            "--require-verified52 requires the canonical 52 task IDs in order "
            "and --repeats 2"
        )
    source_arm = _validate_verified52_source(args, selected)
    source_index = _index_rows(
        raw_rows,
        task_ids=selected,
        repeats=args.repeats,
        source=args.results,
        require_complete=True,
    )
    rows = [
        source_index[(task_id, trial)]
        for task_id in selected
        for trial in range(1, args.repeats + 1)
    ]
    for row in rows:
        run_status = row.get("run_status")
        if args.require_verified52 and (
            not isinstance(run_status, Mapping)
            or run_status.get("baseline_valid") is not True
        ):
            raise ValueError(
                f"source result task {row['task_id']} trial {row['trial']} "
                "is not marked as a valid baseline outcome"
            )
        if isinstance(run_status, Mapping) and run_status.get("baseline_valid") is False:
            raise ValueError(
                f"source result task {row['task_id']} trial {row['trial']} "
                "is an infrastructure failure, not a baseline outcome"
            )
        state = row.get("state")
        if args.require_verified52 and (
            not isinstance(state, Mapping) or state.get("exact_reset") is not True
        ):
            raise ValueError(
                f"source result task {row['task_id']} trial {row['trial']} "
                "is missing an exact state reset"
            )
        if isinstance(state, Mapping) and state.get("exact_reset") is False:
            raise ValueError(
                f"source result task {row['task_id']} trial {row['trial']} "
                "does not have an exact state reset"
            )
    requested_config = _review_config(
        args,
        task_ids=selected,
        api_base=api_base,
        source_arm=source_arm,
    )
    locked_config = _prepare_review_config(
        args.output,
        requested_config,
        resume=args.resume,
    )
    review_config_sha256 = _config_sha256(locked_config)
    tasks_by_id = {
        task_id: task_by_id(
            args.mcp_persona_root,
            task_id,
            args.language,
        )
        for task_id in selected
    }
    source_fingerprints = {
        key: _canonical_sha256(row) for key, row in source_index.items()
    }
    checkpoint_fingerprints = {
        task_id: _checkpoint_sha256(task)
        for task_id, task in tasks_by_id.items()
    }

    existing: dict[tuple[int, int], dict[str, Any]] = {}
    if args.resume and args.output.is_file():
        existing = _index_rows(
            _load_rows(args.output),
            task_ids=selected,
            repeats=args.repeats,
            source=args.output,
            require_complete=False,
            reject_unselected=True,
        )
    persisted = dict(existing)
    reusable_judgments: dict[tuple[int, int], dict[int, dict[str, Any]]] = {}
    if args.reuse_judge_from:
        reusable_rows = _index_rows(
            _load_rows(args.reuse_judge_from),
            task_ids=selected,
            repeats=args.repeats,
            source=args.reuse_judge_from,
            require_complete=False,
        )
        for key, value in reusable_rows.items():
            task_id = key[0]
            if (
                value.get("source_trial_sha256") != source_fingerprints[key]
                or value.get("checkpoint_spec_sha256")
                != checkpoint_fingerprints[task_id]
            ):
                raise ValueError(
                    f"reusable Judge row task {key[0]} trial {key[1]} "
                    "does not match the current trajectory/checkpoints"
                )
            details = value.get("checkpoint_details")
            if not isinstance(details, list):
                raise ValueError(
                    f"reusable Judge row task {key[0]} trial {key[1]} "
                    "has no checkpoint details"
                )
            reusable: dict[int, dict[str, Any]] = {}
            for detail in details:
                if (
                    not isinstance(detail, Mapping)
                    or detail.get("source")
                    not in {"llm_judge", "reused_llm_judge"}
                ):
                    continue
                if not isinstance(detail.get("index"), int):
                    raise ValueError(
                        f"reusable Judge row task {key[0]} trial {key[1]} "
                        "has an invalid checkpoint index"
                    )
                index = int(detail["index"])
                if index in reusable:
                    raise ValueError(
                        f"reusable Judge row task {key[0]} trial {key[1]} "
                        f"duplicates checkpoint {index}"
                    )
                reusable[index] = {
                    "pass": detail.get("status") == "pass",
                    "confidence": float(detail.get("judge_confidence", 0.0)),
                    "reason": str(detail.get("reason", "")),
                    "reused": True,
                }
            reusable_judgments[key] = reusable
    reviewed: list[dict[str, Any]] = []
    if not args.resume or not args.output.is_file():
        _write_jsonl(args.output, [])
    client = (
        None
        if args.rules_only
        else AsyncOpenAI(api_key=api_key, base_url=api_base, timeout=args.api_timeout)
    )
    try:
        for position, row in enumerate(rows, 1):
            task_id = int(row["task_id"])
            key = (task_id, int(row["trial"]))
            task = tasks_by_id[task_id]
            source_trial_sha256 = source_fingerprints[key]
            checkpoint_spec_sha256 = checkpoint_fingerprints[task_id]
            existing_value = existing.get(key)
            if existing_value is not None and (
                existing_value.get("source_trial_sha256")
                != source_trial_sha256
                or existing_value.get("checkpoint_spec_sha256")
                != checkpoint_spec_sha256
                or existing_value.get("review_config_sha256")
                != review_config_sha256
                or existing_value.get("judge_model") != args.model
            ):
                raise ValueError(
                    f"existing review task {task_id} trial {row['trial']} "
                    "does not match the same trajectory and review configuration"
                )
            if (
                existing_value is not None
                and existing_value.get("fully_resolved") is True
                and not existing_value.get("judge_error")
            ):
                value = dict(existing_value)
                if value.get("checkpoint_details"):
                    value["checkpoint_available"] = True
                else:
                    value.update(
                        checkpoint_available=False,
                        semantic_task_complete=None,
                        fully_resolved=True,
                    )
                reviewed.append(value)
                print(
                    f"[{position}/{len(rows)}] task={task_id} trial={row['trial']} resumed"
                )
                continue
            state_evidence = _state_evidence(row)
            rules = list(
                evaluate_checkpoint_rules(
                    task,
                    row.get("tool_calls", []),
                    str(row.get("final_answer", "")),
                    state_changed=bool(row.get("state", {}).get("state_changed")),
                    state_evidence=state_evidence,
                )
            )
            unresolved = [value for value in rules if value["status"] == "needs_judge"]
            judge_error: str | None = None
            judge_attempts = 0
            decisions = dict(
                reusable_judgments.get((task_id, int(row["trial"])), {})
            )
            still_unresolved = [
                value for value in unresolved if int(value["index"]) not in decisions
            ]
            if still_unresolved and client is not None:
                new_decisions, judge_error, judge_attempts = (
                    await _judge_with_retries(
                        client,
                        attempts=args.judge_attempts,
                        retry_delay=args.judge_retry_delay,
                        model=args.model,
                        task=task,
                        row=row,
                        unresolved=still_unresolved,
                        state_evidence=state_evidence,
                        max_evidence_chars=args.max_evidence_chars,
                    )
                )
                decisions.update(new_decisions)

            final_details: list[dict[str, Any]] = []
            for detail in rules:
                value = dict(detail)
                if value["status"] == "needs_judge" and value["index"] in decisions:
                    decision = decisions[value["index"]]
                    value.update(
                        status="pass" if decision["pass"] else "fail",
                        score=1.0 if decision["pass"] else 0.0,
                        source=(
                            "reused_llm_judge"
                            if decision.get("reused")
                            else "llm_judge"
                        ),
                        judge_confidence=decision["confidence"],
                        reason=decision["reason"],
                    )
                    if decision.get("index_normalization"):
                        value["judge_index_normalization"] = decision[
                            "index_normalization"
                        ]
                else:
                    value["source"] = (
                        "deterministic_rule"
                        if value["status"] != "needs_judge"
                        else "unresolved"
                    )
                final_details.append(value)
            resolved = [value for value in final_details if value["score"] is not None]
            score = (
                sum(float(value["score"]) for value in resolved) / len(final_details)
                if final_details and len(resolved) == len(final_details)
                else None
            )
            fully_resolved = score is not None or not final_details
            review_status = (
                "no_public_checkpoint"
                if not final_details
                else "resolved"
                if fully_resolved
                else "judge_failed"
                if judge_error
                else "unresolved_rules_only"
            )
            output = {
                "schema_version": 1,
                "result_scope": RESULT_LABEL,
                "task_id": task_id,
                "trial": int(row["trial"]),
                "source_trial_sha256": source_trial_sha256,
                "checkpoint_spec_sha256": checkpoint_spec_sha256,
                "review_config_sha256": review_config_sha256,
                "judge_model": args.model,
                "checkpoint_details": final_details,
                "checkpoint_available": bool(final_details),
                "semantic_checkpoint_score": score,
                "semantic_task_complete": score == 1.0 if final_details else None,
                "fully_resolved": fully_resolved,
                "judge_error": judge_error,
                "judge_attempts": judge_attempts,
                "review_status": review_status,
                "retry_eligible": bool(judge_error),
            }
            reviewed.append(output)
            persisted[key] = output
            _write_jsonl(
                args.output,
                [
                    persisted[row_key]
                    for source_row in rows
                    if (
                        row_key := (
                            int(source_row["task_id"]),
                            int(source_row["trial"]),
                        )
                    )
                    in persisted
                ],
            )
            print(
                f"[{position}/{len(rows)}] task={task_id} trial={row['trial']} "
                f"score={score} unresolved={sum(v['score'] is None for v in final_details)}"
            )
    finally:
        if client is not None:
            await client.close()

    review_index = _index_rows(
        reviewed,
        task_ids=selected,
        repeats=args.repeats,
        source=args.output,
        require_complete=True,
        reject_unselected=True,
    )
    ordered_reviews = [
        review_index[(task_id, trial)]
        for task_id in selected
        for trial in range(1, args.repeats + 1)
    ]
    _write_jsonl(args.output, ordered_reviews)

    grouped: dict[int, list[dict[str, Any]]] = defaultdict(list)
    for value in ordered_reviews:
        grouped[int(value["task_id"])].append(value)
    no_checkpoint_task_ids = sorted(
        task_id
        for task_id, values in grouped.items()
        if len(values) == args.repeats
        and all(not value.get("checkpoint_available") for value in values)
    )
    fully_reviewed_task_ids = sorted(
        task_id
        for task_id, values in grouped.items()
        if len(values) == args.repeats
        and task_id not in no_checkpoint_task_ids
        and all(value["fully_resolved"] for value in values)
    )
    semantic_pass_task_ids = sorted(
        task_id
        for task_id, values in grouped.items()
        if len(values) == args.repeats
        and task_id not in no_checkpoint_task_ids
        and all(value["semantic_task_complete"] is True for value in values)
    )
    unresolved_task_ids = sorted(
        set(grouped) - set(fully_reviewed_task_ids) - set(no_checkpoint_task_ids)
    )
    unresolved_slots = [
        {"task_id": int(value["task_id"]), "trial": int(value["trial"])}
        for value in ordered_reviews
        if value.get("fully_resolved") is not True
    ]
    judge_failed_slots = [
        {
            "task_id": int(value["task_id"]),
            "trial": int(value["trial"]),
            "error": value.get("judge_error"),
        }
        for value in ordered_reviews
        if value.get("judge_error")
    ]
    unresolved_checkpoint_count = sum(
        detail.get("score") is None
        for value in ordered_reviews
        for detail in value.get("checkpoint_details", [])
        if isinstance(detail, Mapping)
    )
    resolved_trial_scores = [
        float(value["semantic_checkpoint_score"])
        for value in ordered_reviews
        if isinstance(value.get("semantic_checkpoint_score"), (int, float))
        and not isinstance(value.get("semantic_checkpoint_score"), bool)
    ]
    resolved_checkpoint_scores = [
        float(detail["score"])
        for value in ordered_reviews
        for detail in value.get("checkpoint_details", [])
        if isinstance(detail, Mapping)
        and isinstance(detail.get("score"), (int, float))
        and not isinstance(detail.get("score"), bool)
    ]
    task_means = [
        sum(task_scores) / len(task_scores)
        for task_id in selected
        if (
            task_scores := [
                float(value["semantic_checkpoint_score"])
                for value in grouped[task_id]
                if isinstance(
                    value.get("semantic_checkpoint_score"),
                    (int, float),
                )
                and not isinstance(value.get("semantic_checkpoint_score"), bool)
            ]
        )
    ]
    expected_trial_count = len(selected) * args.repeats
    no_checkpoint_trial_count = sum(
        not value.get("checkpoint_available") for value in ordered_reviews
    )
    semantic_review_ready = (
        len(ordered_reviews) == expected_trial_count
        and not unresolved_slots
        and not judge_failed_slots
    )
    summary = {
        "schema_version": 1,
        "result_scope": RESULT_LABEL,
        "dataset_id": (
            "mcp-persona-verified52-writer-full"
            if source_arm == VERIFIED52_WRITER_ARM
            else "mcp-persona-verified52"
            if source_arm is not None
            else "mcp-persona-development-selection"
        ),
        "protocol_id": (
            VERIFIED52_PROTOCOL_ID if source_arm is not None else None
        ),
        "experiment_arm": source_arm,
        "source_results": str(args.results),
        "source_results_sha256": requested_config["results_sha256"],
        "review_config_sha256": review_config_sha256,
        "expected_task_count": len(selected),
        "expected_trial_count": expected_trial_count,
        "reviewed_task_count": len(grouped),
        "unique_reviewed_trial_count": len(ordered_reviews),
        "resolved_trial_count": sum(
            value.get("fully_resolved") is True for value in ordered_reviews
        ),
        "fully_reviewed_task_count": len(fully_reviewed_task_ids),
        "fully_reviewed_task_ids": fully_reviewed_task_ids,
        "semantic_pass_task_count": len(semantic_pass_task_ids),
        "semantic_pass_task_ids": semantic_pass_task_ids,
        "no_public_checkpoint_task_count": len(no_checkpoint_task_ids),
        "no_public_checkpoint_task_ids": no_checkpoint_task_ids,
        "no_public_checkpoint_trial_count": no_checkpoint_trial_count,
        "semantic_checkpoint_trial_count": (
            expected_trial_count - no_checkpoint_trial_count
        ),
        "resolved_semantic_trial_count": len(resolved_trial_scores),
        "unresolved_checkpoint_count": unresolved_checkpoint_count,
        "unresolved_slots": unresolved_slots,
        "judge_failed_slots": judge_failed_slots,
        "unresolved_task_ids": unresolved_task_ids,
        "semantic_score_means": {
            "checkpoint_micro": (
                round(
                    sum(resolved_checkpoint_scores)
                    / len(resolved_checkpoint_scores),
                    6,
                )
                if resolved_checkpoint_scores
                else None
            ),
            "trial_macro": (
                round(sum(resolved_trial_scores) / len(resolved_trial_scores), 6)
                if resolved_trial_scores
                else None
            ),
            "task_macro": (
                round(sum(task_means) / len(task_means), 6)
                if task_means
                else None
            ),
        },
        "policy_note": (
            "All selected tasks remain in one dataset. Model failure changes the score, "
            "not dataset membership; tasks without public checkpoints remain present "
            "and are excluded only from semantic-score denominators."
        ),
        "rules_only": args.rules_only,
        "judge_mode": (
            "reused_existing_llm_judgments"
            if args.reuse_judge_from and args.rules_only
            else "deterministic_rules_only"
            if args.rules_only
            else "live_llm_judge"
        ),
        "reused_judge_source": (
            str(args.reuse_judge_from) if args.reuse_judge_from else None
        ),
        "semantic_review_ready": semantic_review_ready,
    }
    _write_json(args.summary_output, summary)
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return 0 if args.rules_only or semantic_review_ready else 2


def main() -> int:
    return asyncio.run(async_main(parse_args()))


if __name__ == "__main__":
    raise SystemExit(main())
