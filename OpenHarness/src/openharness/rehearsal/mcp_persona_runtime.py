"""Local runtime adapter and transparent evaluator for MCP-Persona.

The public MCP-Persona repository does not ship a complete benchmark runner.
This module builds a deliberately local compatibility layer around the released
simulator functions.  Results produced by it are OpenHarness-compatible local
results and must not be reported as official MCP-Persona scores.
"""

from __future__ import annotations

import hashlib
import importlib.metadata
import importlib.util
import difflib
import json
import os
import platform
import re
import shutil
import subprocess
import sys
import unicodedata
import uuid
from collections import Counter
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence
from urllib.parse import urlparse

from openharness.rehearsal.mcp_persona import _git_head, load_release_tasks


_SERVER_SCHEMA_FILES = {
    "instagram": "instagram.json",
    "lark_mcp": "lark-mcp.json",
    "notion": "notion.json",
    "obsidian": "obsidian.json",
    "reddit": "reddit.json",
    "slack": "slack.json",
    "universal_email": "universal-email.json",
    "wecome": "wecome.json",
    "xiaohongshu": "xiaohongshu.json",
}
_STATEFUL_SERVER_TYPES = {
    "instagram": "json_context",
    "lark_mcp": "json_context",
    "notion": "notion_jsonl",
    "obsidian": "obsidian_jsonl",
    "reddit": "json_context",
    "slack": "json_context",
    "universal_email": "json_context",
    "wecome": "json_context",
    "xiaohongshu": "json_context",
}
_TOOL_PREFIXES = {
    "instagram": "",
    "lark_mcp": "lark_mcp_",
    "notion": "notion_",
    "obsidian": "obsidian_",
    "slack": "slack_",
    "universal_email": "universal_email_",
    "wecome": "wecome_",
    "xiaohongshu": "xiaohongshu_",
}
_WRAP_ARGUMENTS_IN_DATA = {"notion", "obsidian"}
_ERROR_MARKERS = (
    '"success": false',
    "missing required argument",
    "missing required parameter",
    "object_not_found",
    "user not found",
    "context id '",
    "traceback (most recent call last)",
)
_ID_TOKEN = re.compile(
    r"(?:[A-Za-z0-9_-]{8,}|[0-9a-f]{8}-[0-9a-f-]{27,}|feishu\.cn_[^\s\"']+)"
)
EXECUTION_CORE_SOURCE_SUFFIXES = (
    "scripts/mcp_persona_stdio_server.py",
    "src/openharness/api/openai_client.py",
    "src/openharness/engine/query.py",
    "src/openharness/mcp/client.py",
    "src/openharness/tools/mcp_tool.py",
    "src/openharness/rehearsal/mcp_persona.py",
    "src/openharness/rehearsal/mcp_persona_runtime.py",
    "MCP-Persona/requirements.txt",
    "MCP-Persona/data/tasks/en_release_data.json",
    "MCP-Persona/eval/checkpoint_eval.py",
    "MCP-Persona/eval/execution_eval.py",
)


@dataclass(frozen=True)
class LocalScores:
    """Deterministic local scores for one OpenHarness task trial."""

    expected_call_count: int
    actual_call_count: int
    successful_call_count: int
    expected_tool_recall: float
    sequence_score: float
    checkpoint_score: float
    checkpoint_pass_count: int
    checkpoint_fail_count: int
    checkpoint_needs_judge_count: int
    checkpoint_details: tuple[dict[str, Any], ...]
    execution_score: float
    dependency_link_count: int
    state_mutation_required: bool
    state_changed: bool
    state_mutation_ok: bool
    exact_reset: bool
    minimum_three_calls: bool
    server_coverage_ok: bool
    local_pass: bool


def task_by_id(root: Path, task_id: int, language: str = "en") -> dict[str, Any]:
    """Return one released task by canonical ID."""

    for task in load_release_tasks(root, language):
        if task.get("id") == task_id:
            return task
    raise KeyError(f"MCP-Persona task {task_id} was not found in {language}")


def released_tool_schemas(root: Path, server: str) -> dict[str, dict[str, Any]]:
    """Load released real-tool schemas and normalize their server prefix."""

    schema_file = _SERVER_SCHEMA_FILES.get(server)
    if schema_file is None:
        return {}
    path = root / "data" / "real_tools" / schema_file
    if not path.is_file():
        return {}
    raw = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(raw, Mapping):
        return {}
    result: dict[str, dict[str, Any]] = {}
    for qualified_name, value in raw.items():
        if not isinstance(value, Mapping):
            continue
        name = str(value.get("name") or str(qualified_name).split(":", 1)[-1])
        result[name] = dict(value)
    return result


def simulator_source(root: Path, server: str, tool_name: str) -> Path:
    """Resolve a released simulator source file for one server tool."""

    prefix = _TOOL_PREFIXES.get(server, f"{server}_")
    path = root / "data" / "simulated_tools" / server / "pycode" / f"{prefix}{tool_name}.py"
    if not path.is_file():
        raise FileNotFoundError(f"Released simulator source not found: {path}")
    return path


def load_simulator(source: Path) -> Any:
    """Load one simulator function without modifying MCP-Persona source."""

    module_name = f"openharness_mcp_persona_runtime_{source.stem}_{uuid.uuid4().hex}"
    spec = importlib.util.spec_from_file_location(module_name, source)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"Cannot import simulator source: {source}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    function = getattr(module, "analyze_response_patterns", None)
    if function is None:
        raise RuntimeError(f"Simulator has no analyze_response_patterns: {source}")
    return function


def normalize_simulator_arguments(server: str, arguments: Mapping[str, Any]) -> dict[str, Any]:
    """Bridge released schema/simulator shape mismatches.

    Notion and Obsidian real-tool schemas expose arguments at the top level,
    while the released simulator implementations read them from ``data``.
    """

    value = dict(arguments)
    if server in _WRAP_ARGUMENTS_IN_DATA and "data" not in value:
        return {"data": value}
    return value


def _write_jsonl(path: Path, values: Iterable[Mapping[str, Any]]) -> None:
    path.write_text(
        "".join(json.dumps(value, ensure_ascii=False) + "\n" for value in values),
        encoding="utf-8",
    )


def prepare_task_state(task: Mapping[str, Any], state_dir: Path) -> dict[str, list[str]]:
    """Materialize sampled contexts in files accepted by released simulators."""

    state_dir.mkdir(parents=True, exist_ok=True)
    contexts = task.get("sampled_contexts")
    if not isinstance(contexts, Mapping):
        raise ValueError("Task has no sampled_contexts mapping")
    files: dict[str, list[str]] = {}
    for server, raw_items in contexts.items():
        if server not in _STATEFUL_SERVER_TYPES:
            continue
        items = [item for item in raw_items if isinstance(item, Mapping)] if isinstance(raw_items, list) else []
        state_type = _STATEFUL_SERVER_TYPES[server]
        if state_type == "json_context":
            output = state_dir / f"{server}.json"
            value = {
                str(item["id"]): item["data"]
                for item in items
                if "id" in item and "data" in item
            }
            output.write_text(
                json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
                encoding="utf-8",
            )
            files[server] = [str(output)]
        elif state_type == "obsidian_jsonl":
            output = state_dir / "obsidian.jsonl"
            _write_jsonl(
                output,
                [item["data"] for item in items if isinstance(item.get("data"), Mapping)],
            )
            files[server] = [str(output)]
        elif state_type == "notion_jsonl":
            users: list[Mapping[str, Any]] = []
            pages: list[Mapping[str, Any]] = []
            blocks: list[Mapping[str, Any]] = []
            comments: list[Mapping[str, Any]] = []
            for item in items:
                data = item.get("data")
                if not isinstance(data, Mapping):
                    continue
                if isinstance(data.get("user"), Mapping):
                    users.append(data["user"])
                if isinstance(data.get("page"), Mapping):
                    pages.append(data["page"])
                raw_blocks = data.get("blocks")
                if isinstance(raw_blocks, Mapping):
                    blocks.extend(value for value in raw_blocks.values() if isinstance(value, Mapping))
                raw_comments = data.get("comments")
                if isinstance(raw_comments, list):
                    comments.extend(value for value in raw_comments if isinstance(value, Mapping))
            notion_paths = [
                state_dir / "notion-users.jsonl",
                state_dir / "notion-pages.jsonl",
                state_dir / "notion-blocks.jsonl",
                state_dir / "notion-comments.jsonl",
            ]
            for path, values in zip(notion_paths, (users, pages, blocks, comments)):
                _write_jsonl(path, values)
            files[server] = [str(path) for path in notion_paths]
    return files


def configure_simulator_environment(
    server: str,
    task: Mapping[str, Any],
    state_dir: Path,
) -> dict[str, str]:
    """Set and return the environment variables used by one simulator server."""

    state_dir.mkdir(parents=True, exist_ok=True)
    files = prepare_task_state(task, state_dir) if not any(state_dir.iterdir()) else state_files(state_dir)
    server_files = files.get(server)
    if not server_files:
        raise RuntimeError(f"No local state adapter for MCP-Persona server {server}")
    raw_items = task.get("sampled_contexts", {}).get(server, [])
    context_id = str(raw_items[0]["id"]) if raw_items and "id" in raw_items[0] else "all"
    updates: dict[str, str] = {}
    if server == "instagram":
        updates = {"INSTAGRAM_CONTEXT_ID": context_id}
    elif server == "lark_mcp":
        updates = {"LARK_MCP_SANDBOX_PATHS": json.dumps(server_files), "LARK_MCP_CONTEXT_ID": context_id}
    elif server == "xiaohongshu":
        updates = {"XIAOHONGSHU_SANDBOX_PATHS": json.dumps(server_files), "XIAOHONGSHU_CONTEXT_ID": context_id}
    elif server == "slack":
        updates = {"SLACK_SANDBOX_PATHS": json.dumps(server_files), "SLACK_CONTEXT_ID": context_id}
    elif server == "wecome":
        updates = {"WECOM_SANDBOX_PATHS": json.dumps(server_files), "WECOME_CONTEXT_ID": context_id}
    elif server == "obsidian":
        updates = {"OBSIDIAN_SANDBOX_PATHS": json.dumps(server_files)}
    elif server == "notion":
        updates = {"NOTION_SANDBOX_PATHS": json.dumps(server_files)}
    elif server == "reddit":
        updates = {"REDDIT_CONTEXT_ID": context_id, "CONTEXT_ID": context_id}
    elif server == "universal_email":
        updates = {
            "UNIVERSAL_EMAIL_SANDBOX_PATHS": json.dumps(server_files),
            "UNIVERSAL_EMAIL_CONTEXT_ID": context_id,
            "UNIVERSAL_EMAIL_CONTEXT_PATH": server_files[0],
            "MCP_PERSONA_STATE_DIR": str(state_dir),
        }
    for key, value in updates.items():
        os.environ[key] = value
    return updates


def state_files(state_dir: Path) -> dict[str, list[str]]:
    """Discover already-materialized state files by server."""

    result: dict[str, list[str]] = {}
    for server in (
        "instagram",
        "lark_mcp",
        "reddit",
        "xiaohongshu",
        "slack",
        "wecome",
        "universal_email",
    ):
        path = state_dir / f"{server}.json"
        if path.is_file():
            result[server] = [str(path)]
    obsidian = state_dir / "obsidian.jsonl"
    if obsidian.is_file():
        result["obsidian"] = [str(obsidian)]
    notion = [state_dir / f"notion-{name}.jsonl" for name in ("users", "pages", "blocks", "comments")]
    if all(path.is_file() for path in notion):
        result["notion"] = [str(path) for path in notion]
    return result


def directory_hash(root: Path) -> str:
    """Hash relative paths and file bytes for exact reset checks."""

    digest = hashlib.sha256()
    for path in sorted(
        value
        for value in root.rglob("*")
        if value.is_file()
        and not any(part.startswith("_openharness-") for part in value.relative_to(root).parts)
    ):
        digest.update(str(path.relative_to(root)).encode("utf-8"))
        digest.update(b"\0")
        digest.update(path.read_bytes())
        digest.update(b"\0")
    return digest.hexdigest()


def directory_diff_summary(source: Path, target: Path, *, max_chars: int = 20_000) -> str:
    """Return a bounded, human-readable diff of benchmark state files."""

    def included_files(root: Path) -> dict[str, Path]:
        return {
            str(path.relative_to(root)): path
            for path in root.rglob("*")
            if path.is_file()
            and not any(
                part.startswith("_openharness-")
                for part in path.relative_to(root).parts
            )
        }

    before = included_files(source)
    after = included_files(target)
    chunks: list[str] = []
    for relative in sorted(set(before) | set(after)):
        left = before.get(relative)
        right = after.get(relative)
        left_text = left.read_text(encoding="utf-8", errors="replace") if left else ""
        right_text = right.read_text(encoding="utf-8", errors="replace") if right else ""
        if left_text == right_text:
            continue
        diff = "".join(
            difflib.unified_diff(
                left_text.splitlines(keepends=True),
                right_text.splitlines(keepends=True),
                fromfile=f"before/{relative}",
                tofile=f"after/{relative}",
                n=2,
            )
        )
        chunks.append(diff or f"Changed binary/unreadable file: {relative}\n")
        if sum(len(value) for value in chunks) >= max_chars:
            break
    value = "\n".join(chunks)
    if len(value) > max_chars:
        return value[:max_chars] + "\n... [state diff truncated]"
    return value


def exact_copy(source: Path, target: Path) -> None:
    """Replace a task state tree with an exact copy."""

    if target.exists():
        shutil.rmtree(target)
    shutil.copytree(source, target)


def qualified_tool_name(adapter_name: str) -> str:
    """Convert an OpenHarness MCP adapter name to dataset notation."""

    if not adapter_name.startswith("mcp__"):
        return adapter_name
    parts = adapter_name.split("__", 2)
    if len(parts) != 3:
        return adapter_name
    return f"{parts[1]}:{parts[2]}"


def output_failed(output: str, is_error: bool = False) -> bool:
    """Recognize released-simulator failures hidden inside JSON text."""

    lowered = output.lower()
    return is_error or any(marker in lowered for marker in _ERROR_MARKERS)


def _lcs_length(left: Sequence[str], right: Sequence[str]) -> int:
    previous = [0] * (len(right) + 1)
    for left_item in left:
        current = [0]
        for index, right_item in enumerate(right, 1):
            if left_item == right_item:
                current.append(previous[index - 1] + 1)
            else:
                current.append(max(current[-1], previous[index]))
        previous = current
    return previous[-1]


def _dependency_links(tool_calls: Sequence[Mapping[str, Any]]) -> int:
    links = 0
    previous_tokens: set[str] = set()
    for call in tool_calls:
        input_text = json.dumps(call.get("input", {}), ensure_ascii=False)
        input_tokens = set(_ID_TOKEN.findall(input_text))
        if previous_tokens & input_tokens:
            links += 1
        output_text = str(call.get("output", ""))
        previous_tokens.update(_ID_TOKEN.findall(output_text))
    return links


def _normalized_match_text(value: Any) -> str:
    text = unicodedata.normalize("NFKC", str(value)).casefold()
    return re.sub(r"[\s\"'`，,。；;：:（）()\[\]{}]+", "", text)


def _decode_nested_json(value: Any) -> Any:
    """Decode JSON strings nested inside simulator envelopes."""

    if isinstance(value, Mapping):
        return {str(key): _decode_nested_json(child) for key, child in value.items()}
    if isinstance(value, list):
        return [_decode_nested_json(child) for child in value]
    if not isinstance(value, str):
        return value
    try:
        decoded = json.loads(value)
    except (json.JSONDecodeError, TypeError):
        return value
    if decoded == value:
        return value
    return _decode_nested_json(decoded)


def _flatten_scalar_values(value: Any) -> list[str]:
    value = _decode_nested_json(value)
    if isinstance(value, Mapping):
        return [item for child in value.values() for item in _flatten_scalar_values(child)]
    if isinstance(value, list):
        return [item for child in value for item in _flatten_scalar_values(child)]
    if value is None or isinstance(value, bool):
        return []
    text = str(value).strip()
    return [text] if text else []


def _structured_subset(expected: Any, actual: Any) -> bool:
    expected = _decode_nested_json(expected)
    actual = _decode_nested_json(actual)
    if isinstance(expected, Mapping):
        return isinstance(actual, Mapping) and all(
            key in actual and _structured_subset(value, actual[key])
            for key, value in expected.items()
        )
    if isinstance(expected, list):
        if not isinstance(actual, list):
            return False
        remaining = list(actual)
        for expected_item in expected:
            match = next(
                (
                    index
                    for index, actual_item in enumerate(remaining)
                    if _structured_subset(expected_item, actual_item)
                ),
                None,
            )
            if match is None:
                return False
            remaining.pop(match)
        return True
    return _normalized_match_text(expected) == _normalized_match_text(actual)


def _contains_structured_value(evidence: Any, expected: Any) -> bool:
    evidence = _decode_nested_json(evidence)
    if _structured_subset(expected, evidence):
        return True
    if isinstance(evidence, Mapping):
        return any(
            _contains_structured_value(child, expected) for child in evidence.values()
        )
    if isinstance(evidence, list):
        return any(_contains_structured_value(child, expected) for child in evidence)
    return False


def _identifier_targets(value: str) -> list[str]:
    """Return exact-looking IDs, excluding ordinary long words."""

    values = [token for token in _ID_TOKEN.findall(value) if any(char.isdigit() for char in token)]
    values.extend(re.findall(r"\b\d{9,}\.\d+\b", value))
    unique = list(dict.fromkeys(values))
    return [item for item in unique if not any(item != other and item in other for other in unique)]


def _parameter_expectations(summary: str) -> dict[str, list[str]]:
    """Extract only explicit backtick-key/double-quoted-value requirements."""

    expectations: dict[str, list[str]] = {}
    for line in summary.splitlines():
        match = re.match(r"\s*-\s*`([^`]+)`\s*:\s*(.*)", line)
        if match is None:
            continue
        values = re.findall(r'"([^"\n]+)"', match.group(2))
        if values:
            expectations.setdefault(match.group(1), []).extend(values)
    sentence_pattern = re.compile(
        r"(?:Set|Use)\s+the\s+`([^`]+)`\s+parameter\b(.*?)"
        r"(?=(?:\.\s+(?:Set|Use)\s+the\s+`)|$)",
        flags=re.DOTALL,
    )
    for match in sentence_pattern.finditer(summary):
        values = re.findall(r'"([^"\n]+)"', match.group(2))
        if values:
            expectations.setdefault(match.group(1), []).extend(values)
    return {
        key: list(dict.fromkeys(values)) for key, values in expectations.items()
    }


def _values_for_key(value: Any, expected_key: str) -> list[Any]:
    value = _decode_nested_json(value)
    if isinstance(value, Mapping):
        matches = [
            child
            for key, child in value.items()
            if str(key).casefold() == expected_key.casefold()
        ]
        for child in value.values():
            matches.extend(_values_for_key(child, expected_key))
        return matches
    if isinstance(value, list):
        return [
            match
            for child in value
            for match in _values_for_key(child, expected_key)
        ]
    return []


def _missing_scalar_values(expected: Sequence[str], actual: Any) -> list[str]:
    actual_values = {
        _normalized_match_text(value) for value in _flatten_scalar_values(actual)
    }
    return [
        value for value in expected if _normalized_match_text(value) not in actual_values
    ]


def evaluate_checkpoint_rules(
    task: Mapping[str, Any],
    tool_calls: Sequence[Mapping[str, Any]],
    final_answer: str,
    *,
    state_changed: bool,
    state_evidence: str = "",
) -> tuple[dict[str, Any], ...]:
    """Evaluate released checkpoints with transparent deterministic rules.

    A result is ``pass`` only when the public trace contains enough direct
    evidence.  Ambiguous semantic cases are labelled ``needs_judge`` rather
    than being silently counted as failures or successes.
    """

    checkpoints = [value for value in task.get("gt", []) if isinstance(value, Mapping)]
    search_evidence = [
        call.get("raw_output") or call.get("output", "") for call in tool_calls
    ] + [final_answer]
    successful_by_name: dict[str, list[Mapping[str, Any]]] = {}
    for call in tool_calls:
        if output_failed(str(call.get("output", "")), bool(call.get("is_error"))):
            continue
        name = qualified_tool_name(str(call.get("tool_name", "")))
        successful_by_name.setdefault(name, []).append(call)

    details: list[dict[str, Any]] = []
    consumed: Counter[str] = Counter()
    for index, checkpoint in enumerate(checkpoints):
        checkpoint_type = str(checkpoint.get("checkpoint_type", ""))
        detail: dict[str, Any] = {
            "index": index,
            "checkpoint_type": checkpoint_type,
            "status": "needs_judge",
            "score": None,
            "reason": "The public checkpoint requires semantic review.",
        }
        if checkpoint_type == "personalized_search":
            target = str(checkpoint.get("GT_value", "")).strip()
            try:
                structured_target = json.loads(target)
            except (json.JSONDecodeError, TypeError):
                structured_target = None
            identifiers = _identifier_targets(target)
            exact_match = bool(target) and any(
                _normalized_match_text(target) in _normalized_match_text(value)
                for value in search_evidence
            )
            if isinstance(structured_target, (Mapping, list)):
                matched = any(
                    _contains_structured_value(value, structured_target)
                    for value in search_evidence
                )
                detail.update(
                    comparison_mode="structured_json_subset",
                    structured_target=structured_target,
                    structured_match=matched,
                )
            elif identifiers:
                matched = any(
                    all(
                        _normalized_match_text(identifier)
                        in _normalized_match_text(value)
                        for identifier in identifiers
                    )
                    for value in search_evidence
                )
                detail.update(
                    comparison_mode="single_evidence_identifier_set",
                    targets=identifiers,
                    matched_identifiers=matched,
                )
            else:
                matched = exact_match
                detail.update(
                    comparison_mode="normalized_exact_text",
                    targets=[target] if target else [],
                    exact_text_match=matched,
                )
            if target and matched:
                detail.update(
                    status="pass",
                    score=1.0,
                    reason=(
                        "The checkpoint target matches one structured tool result or final answer."
                    ),
                )
            elif not target:
                detail.update(
                    status="fail",
                    score=0.0,
                    reason="The released search checkpoint has no usable target value.",
                )
            else:
                detail["reason"] = (
                    "No single structured result directly matches the released search target; semantic comparison is required."
                )
        elif checkpoint_type == "operate":
            expected_tool = str(checkpoint.get("tool", ""))
            matches = successful_by_name.get(expected_tool, [])
            occurrence = consumed[expected_tool]
            matched_call: Mapping[str, Any] | None = (
                matches[occurrence] if occurrence < len(matches) else None
            )
            if matched_call is None:
                detail.update(
                    status="fail",
                    score=0.0,
                    expected_tool=expected_tool,
                    reason="The required operation tool did not complete successfully.",
                )
            else:
                consumed[expected_tool] += 1
                operate_type = str(checkpoint.get("operate_type", "")).casefold()
                mutation_required = operate_type not in {"", "other", "read", "query"}
                summary = str(checkpoint.get("summary", ""))
                parameter_expectations = _parameter_expectations(summary)
                expected_values = list(
                    dict.fromkeys(
                        value
                        for values in parameter_expectations.values()
                        for value in values
                    )
                )
                summary_without_tool_refs = re.sub(r"`[^`]*:[^`]*`", "", summary)
                expected_values.extend(
                    value
                    for value in _identifier_targets(summary_without_tool_refs)
                    if value not in expected_values
                )
                matched_input = matched_call.get("input", {})
                parameter_mismatches: dict[str, list[str]] = {}
                for parameter, values in parameter_expectations.items():
                    actual_values = _values_for_key(matched_input, parameter)
                    missing = _missing_scalar_values(values, actual_values)
                    if not actual_values or missing:
                        parameter_mismatches[parameter] = missing or values
                call_evidence = "\n".join(
                    [
                        json.dumps(
                            matched_input,
                            ensure_ascii=False,
                            default=str,
                        ),
                        str(
                            matched_call.get("raw_output")
                            or matched_call.get("output", "")
                        ),
                        state_evidence,
                    ]
                )
                normalized_evidence = _normalized_match_text(call_evidence)
                missing = [
                    value
                    for value in expected_values
                    if _normalized_match_text(value) not in normalized_evidence
                ]
                detail.update(
                    expected_tool=expected_tool,
                    operate_type=operate_type,
                    parameter_expectations=parameter_expectations,
                    parameter_mismatches=parameter_mismatches,
                    expected_values=expected_values,
                    missing_expected_values=missing,
                    observed_state_diff=bool(state_evidence),
                )
                if mutation_required and not state_changed:
                    detail.update(
                        status="fail",
                        score=0.0,
                        reason="A state-changing operation succeeded at the tool layer but no state change was observed.",
                    )
                elif parameter_mismatches or missing:
                    detail.update(
                        status="fail",
                        score=0.0,
                        reason=(
                            "The required tool succeeded, but one or more explicit checkpoint parameters or values do not match the call/state evidence."
                        ),
                    )
                elif mutation_required and not state_evidence:
                    detail["reason"] = (
                        "A task-level mutation was reported, but no state diff is available to verify the expected operation."
                    )
                elif expected_values:
                    detail.update(
                        status="pass",
                        score=1.0,
                        reason=(
                            "The required tool, explicit parameters/values, and any required state change are present in direct evidence."
                        ),
                    )
                else:
                    detail["reason"] = (
                        "The required tool succeeded, but the checkpoint does not expose enough exact parameter/state values for deterministic verification."
                    )
        else:
            detail.update(
                status="fail",
                score=0.0,
                reason=f"Unsupported released checkpoint type: {checkpoint_type or '(empty)'}.",
            )
        details.append(detail)
    return tuple(details)


def evaluate_local_trial(
    task: Mapping[str, Any],
    tool_calls: Sequence[Mapping[str, Any]],
    final_answer: str,
    *,
    state_changed: bool,
    exact_reset: bool,
    state_evidence: str = "",
) -> LocalScores:
    """Score one trace with auditable rules and no hidden LLM judge."""

    expected = [str(value) for value in task.get("chains", [])]
    actual = [qualified_tool_name(str(call.get("tool_name", ""))) for call in tool_calls]
    successful = [
        call for call in tool_calls if not output_failed(str(call.get("output", "")), bool(call.get("is_error")))
    ]
    expected_counts = Counter(expected)
    actual_counts = Counter(actual)
    matched = sum(min(count, actual_counts[name]) for name, count in expected_counts.items())
    recall = matched / len(expected) if expected else 0.0
    sequence = _lcs_length(expected, actual) / len(expected) if expected else 0.0

    checkpoints = [value for value in task.get("gt", []) if isinstance(value, Mapping)]
    successful_names = [
        qualified_tool_name(str(call.get("tool_name", ""))) for call in successful
    ]
    checkpoint_details = evaluate_checkpoint_rules(
        task,
        tool_calls,
        final_answer,
        state_changed=state_changed,
        state_evidence=state_evidence,
    )
    checkpoint_pass_count = sum(value["status"] == "pass" for value in checkpoint_details)
    checkpoint_fail_count = sum(value["status"] == "fail" for value in checkpoint_details)
    checkpoint_needs_judge_count = sum(
        value["status"] == "needs_judge" for value in checkpoint_details
    )
    checkpoint_score = (
        checkpoint_pass_count / len(checkpoints) if checkpoints else 0.0
    )
    successful_counts = Counter(successful_names)
    successful_expected = sum(
        min(count, successful_counts[name]) for name, count in expected_counts.items()
    )
    execution = successful_expected / len(expected) if expected else 0.0
    required_servers = {name.split(":", 1)[0] for name in expected if ":" in name}
    actual_servers = {name.split(":", 1)[0] for name in actual if ":" in name}
    server_coverage_ok = required_servers.issubset(actual_servers)
    minimum_three = len(tool_calls) >= 3
    mutation_required = any(
        checkpoint.get("checkpoint_type") == "operate"
        and str(checkpoint.get("operate_type", "")).lower()
        not in {"", "other", "read", "query"}
        for checkpoint in checkpoints
    )
    mutation_ok = not mutation_required or state_changed
    local_pass = (
        len(tool_calls) >= len(expected)
        and server_coverage_ok
        and recall == 1.0
        and sequence == 1.0
        and execution == 1.0
        and mutation_ok
        and exact_reset
    )
    return LocalScores(
        expected_call_count=len(expected),
        actual_call_count=len(tool_calls),
        successful_call_count=len(successful),
        expected_tool_recall=recall,
        sequence_score=sequence,
        checkpoint_score=checkpoint_score,
        checkpoint_pass_count=checkpoint_pass_count,
        checkpoint_fail_count=checkpoint_fail_count,
        checkpoint_needs_judge_count=checkpoint_needs_judge_count,
        checkpoint_details=checkpoint_details,
        execution_score=execution,
        dependency_link_count=_dependency_links(tool_calls),
        state_mutation_required=mutation_required,
        state_changed=state_changed,
        state_mutation_ok=mutation_ok,
        exact_reset=exact_reset,
        minimum_three_calls=minimum_three,
        server_coverage_ok=server_coverage_ok,
        local_pass=local_pass,
    )


def execution_runtime_fingerprint(
    version_manifest: Mapping[str, Any],
) -> dict[str, Any]:
    """Fingerprint the environment and execution core shared by both Actor arms.

    The optional MCP-Persona virtualenv is used only to probe the released
    evaluator scripts.  It is deliberately excluded so a missing probe
    environment cannot make an otherwise identical shared execution core look
    different.  Arm-specific orchestration, including the Writer handoff and
    runner source, is recorded separately as provenance.
    """

    file_hashes = version_manifest.get("file_sha256")
    hash_items = file_hashes.items() if isinstance(file_hashes, Mapping) else ()
    source_hashes: dict[str, str] = {}
    for path, digest in hash_items:
        normalized = str(path).replace("\\", "/")
        suffix = next(
            (
                candidate
                for candidate in EXECUTION_CORE_SOURCE_SUFFIXES
                if normalized.endswith(candidate)
            ),
            None,
        )
        if suffix is None:
            continue
        value = str(digest)
        existing = source_hashes.get(suffix)
        if existing is not None and existing != value:
            raise ValueError(
                f"conflicting execution-core hashes for canonical source {suffix}"
            )
        source_hashes[suffix] = value
    payload = {
        "schema_version": 2,
        "scope": "shared_actor_execution_core",
        "runtime": version_manifest.get("runtime"),
        "execution_core_source_sha256": dict(sorted(source_hashes.items())),
    }
    digest = hashlib.sha256(
        json.dumps(
            payload,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            default=str,
        ).encode("utf-8")
    ).hexdigest()
    return {**payload, "sha256": digest}


def _external_environment_manifest(
    python_executable: Path | None,
) -> dict[str, Any]:
    if python_executable is None:
        return {
            "role": "optional_official_evaluator_probe",
            "status": "not_requested",
            "captured": False,
        }
    executable = python_executable.resolve()
    if not executable.is_file():
        return {
            "role": "optional_official_evaluator_probe",
            "status": "missing",
            "captured": False,
            "python": str(executable),
        }
    code = (
        "import importlib.metadata as m,json,platform;"
        "print(json.dumps({'python':platform.python_version(),"
        "'packages':dict(sorted({str(d.metadata.get('Name') or d.name):d.version "
        "for d in m.distributions()}.items()))}))"
    )
    try:
        completed = subprocess.run(
            [str(executable), "-c", code],
            text=True,
            capture_output=True,
            timeout=20,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return {
            "role": "optional_official_evaluator_probe",
            "status": "failed",
            "captured": False,
            "python": str(executable),
            "error": f"{type(exc).__name__}: {exc}",
        }
    if completed.returncode != 0:
        details = (completed.stderr or completed.stdout).strip()
        return {
            "role": "optional_official_evaluator_probe",
            "status": "failed",
            "captured": False,
            "python": str(executable),
            "returncode": completed.returncode,
            "error_tail": details[-4_000:],
        }
    try:
        details = json.loads(completed.stdout)
    except json.JSONDecodeError as exc:
        return {
            "role": "optional_official_evaluator_probe",
            "status": "failed",
            "captured": False,
            "python": str(executable),
            "returncode": completed.returncode,
            "error": f"JSONDecodeError: {exc}",
        }
    return {
        "role": "optional_official_evaluator_probe",
        "status": "captured",
        "captured": True,
        "python": str(executable),
        "python_version": details.get("python"),
        "packages": details.get("packages"),
    }


def build_version_manifest(
    openharness_root: Path,
    mcp_persona_root: Path,
    *,
    model: str,
    api_base: str,
    mcp_persona_python: Path | None = None,
) -> dict[str, Any]:
    """Build a secret-free experiment version manifest."""

    packages = dict(
        sorted(
            {
                str(
                    distribution.metadata["Name"]
                    if "Name" in distribution.metadata
                    else distribution.name
                ): distribution.version
                for distribution in importlib.metadata.distributions()
            }.items(),
            key=lambda item: item[0].casefold(),
        )
    )
    external = _external_environment_manifest(mcp_persona_python)
    release = platform.freedesktop_os_release() if sys.platform.startswith("linux") else {}
    tracked_files = [
        openharness_root / "scripts" / "run_mcp_persona_week1.py",
        openharness_root / "scripts" / "mcp_persona_stdio_server.py",
        openharness_root / "src" / "openharness" / "api" / "openai_client.py",
        openharness_root / "src" / "openharness" / "engine" / "query.py",
        openharness_root / "src" / "openharness" / "mcp" / "client.py",
        openharness_root / "src" / "openharness" / "tools" / "mcp_tool.py",
        openharness_root / "src" / "openharness" / "rehearsal" / "mcp_persona.py",
        openharness_root / "src" / "openharness" / "rehearsal" / "mcp_persona_runtime.py",
        mcp_persona_root / "requirements.txt",
        mcp_persona_root / "data" / "tasks" / "en_release_data.json",
        mcp_persona_root / "data" / "tasks" / "zh_release_data.json",
        mcp_persona_root / "eval" / "checkpoint_eval.py",
        mcp_persona_root / "eval" / "execution_eval.py",
    ]
    file_hashes = {
        str(path.resolve()): hashlib.sha256(path.read_bytes()).hexdigest()
        for path in tracked_files
        if path.is_file()
    }
    manifest = {
        "schema_version": 1,
        "benchmark": "MCP-Persona",
        "result_scope": "OpenHarness-compatible local experiment; not an official score",
        "openharness": {
            "root": str(openharness_root.resolve()),
            "commit": _git_head(openharness_root.resolve()),
        },
        "mcp_persona": {
            "repository": "https://github.com/wwh0411/MCP-Persona",
            "root": str(mcp_persona_root.resolve()),
            "commit": _git_head(mcp_persona_root.resolve()),
            "external_environment": external,
        },
        "runtime": {
            "python": platform.python_version(),
            "platform": platform.platform(),
            "kernel": platform.release(),
            "wsl": bool(os.environ.get("WSL_DISTRO_NAME") or "microsoft" in platform.release().lower()),
            "wsl_distribution": os.environ.get("WSL_DISTRO_NAME"),
            "os": {"name": release.get("NAME"), "version": release.get("VERSION_ID")},
            "packages": packages,
        },
        "model": {
            "name": model,
            "api_base_host": urlparse(api_base).hostname,
            "api_base_path": urlparse(api_base).path,
            "api_key_present": bool(os.environ.get("OPENAI_API_KEY")),
            "api_key_value_recorded": False,
        },
        "file_sha256": file_hashes,
        "provenance_policy": {
            "source_metadata_recorded": True,
            "source_lock_enforced": False,
            "optional_evaluator_environment_part_of_actor_runtime": False,
        },
    }
    manifest["execution_runtime"] = execution_runtime_fingerprint(manifest)
    return manifest


def probe_official_evaluators(
    root: Path,
    python_executable: Path | None = None,
) -> dict[str, Any]:
    """Attempt safe ``--help`` starts and capture public evaluator blockers."""

    results: dict[str, Any] = {}
    executable = (
        str(python_executable.resolve())
        if python_executable is not None and python_executable.is_file()
        else sys.executable
    )
    for name in ("checkpoint_eval.py", "execution_eval.py"):
        path = root / "eval" / name
        if not path.is_file():
            results[name] = {"present": False, "runnable": False, "error": "missing script"}
            continue
        completed = subprocess.run(
            [executable, str(path), "--help"],
            cwd=root,
            text=True,
            capture_output=True,
            timeout=30,
            check=False,
        )
        output = (completed.stdout + "\n" + completed.stderr).strip()
        results[name] = {
            "present": True,
            "python": executable,
            "returncode": completed.returncode,
            "runnable": completed.returncode == 0,
            "output_tail": output[-4000:],
            "hardcoded_author_path": "/data/JohnDoe/MCP-Persona" in path.read_text(encoding="utf-8"),
        }
    return results


def scores_to_dict(scores: LocalScores) -> dict[str, Any]:
    return asdict(scores)
