"""MCP-Persona release auditing and local state smoke helpers.

The public MCP-Persona release contains task data and individual simulated
tool implementations, but it does not currently provide a complete benchmark
runner.  This module deliberately limits itself to release inspection and a
small, deterministic state-mutation smoke test.  It must not be described as
an official MCP-Persona baseline runner.
"""

from __future__ import annotations

import csv
import hashlib
import importlib.util
import json
import os
import re
import sys
import tempfile
import uuid
from collections import Counter
from contextlib import contextmanager
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Iterator, Mapping, Sequence


_HELPER_MODULES = {
    "context_manager",
    "dynamic_context_handler",
    "shared_utils",
}
_ANNOTATION_TOOL_PATTERN = re.compile(
    r"(?m)^\s*\d+\.\s+([A-Za-z0-9_-]+:[A-Za-z0-9_.-]+)(?=\s|:|$)"
)


@dataclass(frozen=True)
class TaskAudit:
    """One task's release-coverage and consistency result."""

    task_id: int
    language: str
    query_type: str
    task_scope: str
    chain_length: int
    server_count: int
    servers: tuple[str, ...]
    chains: tuple[str, ...]
    annotation_tools: tuple[str, ...]
    missing_tools: tuple[str, ...]
    missing_context_servers: tuple[str, ...]
    checkpoint_count: int
    operate_checkpoint_count: int
    has_state_modification: bool
    all_tools_implemented: bool
    all_contexts_present: bool
    has_checkpoints: bool
    annotation_matches_chain: bool
    coverage_candidate: bool
    strict_candidate: bool
    static_classification: str


def _read_json(path: Path) -> Any:
    with path.open("r", encoding="utf-8") as stream:
        return json.load(stream)


def _release_file(root: Path, language: str) -> Path:
    if language not in {"en", "zh"}:
        raise ValueError("language must be 'en' or 'zh'")
    return root / "data" / "tasks" / f"{language}_release_data.json"


def load_release_tasks(root: Path, language: str = "en") -> list[dict[str, Any]]:
    """Load one MCP-Persona language release and validate its top-level shape."""

    path = _release_file(root, language)
    if not path.is_file():
        raise FileNotFoundError(f"MCP-Persona task file not found: {path}")
    value = _read_json(path)
    if not isinstance(value, list) or not all(isinstance(item, dict) for item in value):
        raise ValueError(f"Expected a list of task objects in {path}")
    return value


def discover_simulated_tools(root: Path) -> dict[str, set[str]]:
    """Discover tool names backed by released Python simulator files."""

    simulated_root = root / "data" / "simulated_tools"
    if not simulated_root.is_dir():
        raise FileNotFoundError(f"Simulated tool directory not found: {simulated_root}")

    discovered: dict[str, set[str]] = {}
    for server_dir in sorted(path for path in simulated_root.iterdir() if path.is_dir()):
        server = server_dir.name
        tools: set[str] = set()
        prefix = f"{server}_"
        for source in sorted(server_dir.rglob("*.py")):
            if source.stem in _HELPER_MODULES:
                continue
            tool = source.stem[len(prefix) :] if source.stem.startswith(prefix) else source.stem
            tools.add(f"{server}:{tool}")
        discovered[server] = tools
    return discovered


def extract_annotation_tools(task: Mapping[str, Any]) -> tuple[str, ...]:
    """Extract the numbered tool sequence from a task's free-form annotation."""

    annotation = task.get("gt_annotation")
    if not isinstance(annotation, Mapping):
        return ()
    sequence = annotation.get("tool_calling_sequence")
    if not isinstance(sequence, str):
        return ()
    return tuple(_ANNOTATION_TOOL_PATTERN.findall(sequence))


def audit_task(
    task: Mapping[str, Any],
    implemented_tools: set[str],
    language: str = "en",
) -> TaskAudit:
    """Audit one task without claiming that file coverage proves executability."""

    chains_value = task.get("chains", [])
    chains = tuple(str(item) for item in chains_value if isinstance(item, str))
    servers = tuple(sorted({item.split(":", 1)[0] for item in chains if ":" in item}))
    missing_tools = tuple(item for item in chains if item not in implemented_tools)

    contexts = task.get("sampled_contexts")
    context_servers = set(contexts) if isinstance(contexts, Mapping) else set()
    simulated_servers = {item.split(":", 1)[0] for item in chains if item in implemented_tools}
    missing_context_servers = tuple(sorted(simulated_servers - context_servers))

    checkpoints_value = task.get("gt")
    checkpoints = (
        [item for item in checkpoints_value if isinstance(item, Mapping)]
        if isinstance(checkpoints_value, list)
        else []
    )
    operate_count = sum(item.get("checkpoint_type") == "operate" for item in checkpoints)
    has_state_modification = any(
        item.get("checkpoint_type") == "operate"
        and str(item.get("operate_type", "")).lower() not in {"", "other", "read", "query"}
        for item in checkpoints
    )
    annotation_tools = extract_annotation_tools(task)
    annotation_matches_chain = annotation_tools == chains
    all_tools_implemented = not missing_tools and bool(chains)
    all_contexts_present = not missing_context_servers
    has_checkpoints = bool(checkpoints)
    coverage_candidate = all_tools_implemented and all_contexts_present
    strict_candidate = coverage_candidate and has_checkpoints and annotation_matches_chain

    if missing_tools:
        static_classification = "缺少部分工具"
    elif missing_context_servers:
        static_classification = "缺少初始状态"
    elif not has_checkpoints:
        static_classification = "无法评价"
    else:
        static_classification = "暂时无法判断"

    task_id = task.get("id")
    if not isinstance(task_id, int):
        raise ValueError(f"Task has a non-integer id: {task_id!r}")
    return TaskAudit(
        task_id=task_id,
        language=language,
        query_type=str(task.get("query_type", "")),
        task_scope="cross_server" if len(servers) > 1 else "single_server",
        chain_length=len(chains),
        server_count=len(servers),
        servers=servers,
        chains=chains,
        annotation_tools=annotation_tools,
        missing_tools=missing_tools,
        missing_context_servers=missing_context_servers,
        checkpoint_count=len(checkpoints),
        operate_checkpoint_count=operate_count,
        has_state_modification=has_state_modification,
        all_tools_implemented=all_tools_implemented,
        all_contexts_present=all_contexts_present,
        has_checkpoints=has_checkpoints,
        annotation_matches_chain=annotation_matches_chain,
        coverage_candidate=coverage_candidate,
        strict_candidate=strict_candidate,
        static_classification=static_classification,
    )


def _counter_dict(values: Sequence[str] | Iterator[str]) -> dict[str, int]:
    return dict(sorted(Counter(values).items()))


def _git_head(root: Path) -> str | None:
    head_path = root / ".git" / "HEAD"
    if not head_path.is_file():
        return None
    head = head_path.read_text(encoding="utf-8").strip()
    if head.startswith("ref: "):
        ref_path = root / ".git" / head.removeprefix("ref: ")
        if ref_path.is_file():
            return ref_path.read_text(encoding="utf-8").strip()
        return None
    return head


def audit_evaluator_release(root: Path) -> dict[str, Any]:
    """Report known portability blockers in the released evaluator scripts."""

    files = [root / "eval" / "checkpoint_eval.py", root / "eval" / "execution_eval.py"]
    combined = "\n".join(path.read_text(encoding="utf-8") for path in files if path.is_file())
    agentoolkit_available = importlib.util.find_spec("agentoolkit") is not None
    return {
        "scripts_present": all(path.is_file() for path in files),
        "hardcoded_author_path_present": "/data/JohnDoe/MCP-Persona" in combined,
        "imports_agentoolkit": "from agentoolkit" in combined,
        "agentoolkit_importable": agentoolkit_available,
        "official_evaluator_runnable_from_requirements_only": (
            all(path.is_file() for path in files)
            and "/data/JohnDoe/MCP-Persona" not in combined
            and ("from agentoolkit" not in combined or agentoolkit_available)
        ),
    }


def audit_language_alignment(root: Path) -> dict[str, Any]:
    """Check whether English and Chinese releases represent the same canonical tasks."""

    english = {item["id"]: item for item in load_release_tasks(root, "en")}
    chinese = {item["id"]: item for item in load_release_tasks(root, "zh")}
    shared = sorted(set(english) & set(chinese))
    mismatched_query_types = [
        task_id
        for task_id in shared
        if english[task_id].get("query_type") != chinese[task_id].get("query_type")
    ]
    mismatched_chains = [
        task_id for task_id in shared if english[task_id].get("chains") != chinese[task_id].get("chains")
    ]
    return {
        "english_task_count": len(english),
        "chinese_task_count": len(chinese),
        "shared_task_ids": len(shared),
        "english_only_ids": sorted(set(english) - set(chinese)),
        "chinese_only_ids": sorted(set(chinese) - set(english)),
        "query_type_mismatch_ids": mismatched_query_types,
        "chain_mismatch_ids": mismatched_chains,
        "canonical_id_alignment": (
            len(shared) == len(english) == len(chinese)
            and not mismatched_query_types
            and not mismatched_chains
        ),
    }


def build_audit_report(root: Path, language: str = "en") -> dict[str, Any]:
    """Build a JSON-serializable release audit report."""

    root = root.resolve()
    tasks = load_release_tasks(root, language)
    simulated = discover_simulated_tools(root)
    implemented_tools = set().union(*simulated.values()) if simulated else set()
    audits = [audit_task(task, implemented_tools, language) for task in tasks]
    referenced_tools = {tool for audit in audits for tool in audit.chains}
    referenced_servers = {server for audit in audits for server in audit.servers}
    missing_tool_counts = Counter(tool for audit in audits for tool in audit.missing_tools)

    inventory_paths = {
        "tasks": root / "data" / "tasks",
        "simulated_tools": root / "data" / "simulated_tools",
        "context_schema": root / "data" / "context_schema",
        "mcp_configs": root / "src" / "mcp_configs",
        "evaluation": root / "eval",
    }
    repository_inventory = {
        name: {
            "present": path.is_dir(),
            "file_count": sum(value.is_file() for value in path.rglob("*"))
            if path.is_dir()
            else 0,
            "relative_path": str(path.relative_to(root)),
        }
        for name, path in inventory_paths.items()
    }

    return {
        "schema_version": 1,
        "benchmark": "MCP-Persona",
        "upstream_root": root.name,
        "upstream_commit": _git_head(root),
        "language": language,
        "repository_inventory": repository_inventory,
        "summary": {
            "task_count": len(audits),
            "referenced_server_count": len(referenced_servers),
            "referenced_tool_count": len(referenced_tools),
            "released_simulated_server_count": len(simulated),
            "released_simulated_tool_file_count": len(implemented_tools),
            "referenced_tools_with_simulator_count": len(referenced_tools & implemented_tools),
            "all_tools_implemented_task_count": sum(a.all_tools_implemented for a in audits),
            "coverage_candidate_task_count": sum(a.coverage_candidate for a in audits),
            "strict_candidate_task_count": sum(a.strict_candidate for a in audits),
            "cross_server_coverage_candidate_count": sum(
                a.coverage_candidate and a.server_count > 1 for a in audits
            ),
            "tasks_with_checkpoints": sum(a.has_checkpoints for a in audits),
            "tasks_with_operate_checkpoints": sum(a.operate_checkpoint_count > 0 for a in audits),
            "annotation_chain_match_count": sum(a.annotation_matches_chain for a in audits),
        },
        "distributions": {
            "query_type": _counter_dict(a.query_type for a in audits),
            "chain_length": _counter_dict(str(a.chain_length) for a in audits),
            "coverage_candidate_query_type": _counter_dict(
                a.query_type for a in audits if a.coverage_candidate
            ),
        },
        "referenced_servers": sorted(referenced_servers),
        "released_simulated_servers": sorted(simulated),
        "missing_servers": sorted(referenced_servers - set(simulated)),
        "most_frequent_missing_tools": [
            {"tool": tool, "task_occurrences": count}
            for tool, count in missing_tool_counts.most_common()
        ],
        "language_alignment": audit_language_alignment(root),
        "evaluator_release": audit_evaluator_release(root),
        "interpretation": {
            "coverage_candidate": (
                "Every chain tool has a released Python simulator file and every simulated "
                "server has sampled task context. This is file-level coverage, not proof of execution."
            ),
            "strict_candidate": (
                "A coverage candidate that also has checkpoints and an annotation tool sequence "
                "exactly matching the released chains list. It still requires runtime validation."
            ),
        },
        "tasks": [asdict(audit) for audit in audits],
    }


def write_audit_json(report: Mapping[str, Any], output: Path) -> None:
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def write_task_csv(report: Mapping[str, Any], output: Path) -> None:
    output.parent.mkdir(parents=True, exist_ok=True)
    tasks_value = report.get("tasks")
    if not isinstance(tasks_value, list):
        raise ValueError("Audit report has no tasks list")
    fieldnames = [field.name for field in TaskAudit.__dataclass_fields__.values()]
    with output.open("w", encoding="utf-8", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=fieldnames, lineterminator="\n")
        writer.writeheader()
        for task in tasks_value:
            if not isinstance(task, Mapping):
                continue
            row = dict(task)
            for name in (
                "servers",
                "chains",
                "annotation_tools",
                "missing_tools",
                "missing_context_servers",
            ):
                row[name] = "|".join(str(item) for item in row.get(name, []))
            writer.writerow(row)


@contextmanager
def _temporary_environment(updates: Mapping[str, str | None]) -> Iterator[None]:
    original = {name: os.environ.get(name) for name in updates}
    try:
        for name, value in updates.items():
            if value is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = value
        yield
    finally:
        for name, value in original.items():
            if value is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = value


def _load_simulator(source: Path) -> Any:
    module_name = f"openharness_mcp_persona_smoke_{source.stem}_{uuid.uuid4().hex}"
    spec = importlib.util.spec_from_file_location(module_name, source)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"Cannot load simulator module: {source}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    function = getattr(module, "analyze_response_patterns", None)
    if function is None:
        raise RuntimeError(f"Simulator has no analyze_response_patterns function: {source}")
    return function


def _result_text(result: Mapping[str, Any]) -> dict[str, Any]:
    payload = result.get("result")
    if not isinstance(payload, Mapping):
        raise RuntimeError(f"Simulator returned no result payload: {result}")
    content = payload.get("content")
    if not isinstance(content, list) or not content or not isinstance(content[0], Mapping):
        raise RuntimeError(f"Simulator returned malformed content: {result}")
    text = content[0].get("text")
    if not isinstance(text, str):
        raise RuntimeError(f"Simulator returned non-text content: {result}")
    value = json.loads(text)
    if not isinstance(value, dict):
        raise RuntimeError(f"Expected a JSON object in simulator text: {text}")
    return value


def run_lark_task8_state_smoke(root: Path) -> dict[str, Any]:
    """Replay task 8's create→patch dependency and verify exact state reset."""

    root = root.resolve()
    tasks = {task["id"]: task for task in load_release_tasks(root, "en")}
    task = tasks.get(8)
    if task is None:
        raise RuntimeError("Expected MCP-Persona task 8 in the English release")
    sampled = task.get("sampled_contexts", {}).get("lark_mcp")
    if not isinstance(sampled, list) or not sampled:
        raise RuntimeError("Task 8 has no lark_mcp sampled context")
    contexts = {
        str(item["id"]): item["data"]
        for item in sampled
        if isinstance(item, Mapping) and "id" in item and "data" in item
    }
    context_id = next(iter(contexts))
    calendar_id = next(iter(contexts[context_id]["calendars"]))
    pycode = root / "data" / "simulated_tools" / "lark_mcp" / "pycode"
    simulator_parent = root / "data" / "simulated_tools"

    with tempfile.TemporaryDirectory(prefix="mcp-persona-task8-") as temp_dir:
        context_file = Path(temp_dir) / "lark-context.json"
        context_file.write_text(
            json.dumps(contexts, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        initial_bytes = context_file.read_bytes()
        initial_hash = hashlib.sha256(initial_bytes).hexdigest()
        old_path = list(sys.path)
        sys.path.insert(0, str(simulator_parent))
        try:
            with _temporary_environment(
                {
                    "LARK_MCP_SANDBOX_PATHS": json.dumps([str(context_file)]),
                    "LARK_MCP_CONTEXT_ID": context_id,
                }
            ):
                create = _load_simulator(
                    pycode / "lark_mcp_calendar_v4_calendarEvent_create.py"
                )
                create_result = create(
                    {
                        "path": {"calendar_id": calendar_id},
                        "data": {
                            "summary": "MCP项目启动会议",
                            "start_time": {
                                "timestamp": "1776141600",
                                "timezone": "Asia/Shanghai",
                            },
                            "end_time": {
                                "timestamp": "1776147000",
                                "timezone": "Asia/Shanghai",
                            },
                            "location": {"name": "会议室A"},
                            "vchat": {
                                "vc_type": "vc",
                                "meeting_settings": {
                                    "join_meeting_permission": "only_organization_employees"
                                },
                            },
                            "visibility": "public",
                            "reminders": [{"minutes": 15}],
                        },
                    }
                )
                if not create_result.get("success"):
                    raise RuntimeError(f"Task 8 create step failed: {create_result}")
                event_id = str(_result_text(create_result)["event_id"])

                patch = _load_simulator(
                    pycode / "lark_mcp_calendar_v4_calendarEvent_patch.py"
                )
                description = "1.项目背景介绍 2.团队成员分工 3.时间节点规划"
                patch_result = patch(
                    {
                        "path": {"calendar_id": calendar_id, "event_id": event_id},
                        "data": {"description": description},
                    }
                )
                if not patch_result.get("success"):
                    raise RuntimeError(f"Task 8 patch step failed: {patch_result}")

                mutated_bytes = context_file.read_bytes()
                mutated_hash = hashlib.sha256(mutated_bytes).hexdigest()
                state = json.loads(mutated_bytes)
                event = state[context_id]["calendars"][calendar_id]["events"][event_id]
                state_verified = event.get("description") == description
        finally:
            sys.path[:] = old_path

        context_file.write_bytes(initial_bytes)
        reset_hash = hashlib.sha256(context_file.read_bytes()).hexdigest()
        reset_verified = reset_hash == initial_hash
        mutation_verified = mutated_hash != initial_hash
        success = mutation_verified and state_verified and reset_verified
        if not success:
            raise RuntimeError(
                "Task 8 smoke failed: "
                f"mutation={mutation_verified}, state={state_verified}, reset={reset_verified}"
            )
        return {
            "schema_version": 1,
            "benchmark": "MCP-Persona",
            "upstream_commit": _git_head(root),
            "task_id": 8,
            "query_type": task.get("query_type"),
            "tool_sequence": [
                "lark_mcp:calendar_v4_calendarEvent_create",
                "lark_mcp:calendar_v4_calendarEvent_patch",
            ],
            "dependency_verified": True,
            "created_event_id": event_id,
            "state_mutation_verified": mutation_verified,
            "patched_state_verified": state_verified,
            "exact_reset_verified": reset_verified,
            "success": success,
            "scope": (
                "Direct released-simulator smoke only; this is not an OpenHarness model run "
                "or an official MCP-Persona benchmark score."
            ),
        }


def run_lark_task1_dependency_probe(root: Path) -> dict[str, Any]:
    """Replay task 1's released contact lookup and record its data mismatch."""

    root = root.resolve()
    tasks = {task["id"]: task for task in load_release_tasks(root, "en")}
    task = tasks.get(1)
    if task is None:
        raise RuntimeError("Expected MCP-Persona task 1 in the English release")
    sampled = task.get("sampled_contexts", {}).get("lark_mcp")
    if not isinstance(sampled, list) or not sampled:
        raise RuntimeError("Task 1 has no lark_mcp sampled context")
    contexts = {
        str(item["id"]): item["data"]
        for item in sampled
        if isinstance(item, Mapping) and "id" in item and "data" in item
    }
    pycode = root / "data" / "simulated_tools" / "lark_mcp" / "pycode"
    simulator_parent = root / "data" / "simulated_tools"

    with tempfile.TemporaryDirectory(prefix="mcp-persona-task1-") as temp_dir:
        context_file = Path(temp_dir) / "lark-context.json"
        context_file.write_text(
            json.dumps(contexts, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        old_path = list(sys.path)
        sys.path.insert(0, str(simulator_parent))
        try:
            with _temporary_environment(
                {"LARK_MCP_SANDBOX_PATHS": json.dumps([str(context_file)])}
            ):
                lookup = _load_simulator(pycode / "lark_mcp_contact_v3_user_get.py")
                result = lookup(
                    {
                        "path": {"user_id": "+8613800138000"},
                        "params": {"user_id_type": "user_id"},
                    }
                )
        finally:
            sys.path[:] = old_path

    dependency_satisfied = bool(result.get("success"))
    return {
        "schema_version": 1,
        "benchmark": "MCP-Persona",
        "upstream_commit": _git_head(root),
        "task_id": 1,
        "query_type": task.get("query_type"),
        "tool": "lark_mcp:contact_v3_user_get",
        "released_annotation_arguments": {
            "path": {"user_id": "+8613800138000"},
            "params": {"user_id_type": "user_id"},
        },
        "dependency_satisfied": dependency_satisfied,
        "observed_error": result.get("error"),
        "finding": (
            "PASS: released annotation resolves in sampled context"
            if dependency_satisfied
            else "BLOCKED: released annotation does not resolve in sampled context"
        ),
    }
