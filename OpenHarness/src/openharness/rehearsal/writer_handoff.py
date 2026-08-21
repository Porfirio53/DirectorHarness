"""Thin handoff bridge to the externally maintained ``writer_harness`` package.

The Writer implementation belongs to a sibling workspace module.  This bridge
loads the live package, records source provenance, and adapts its public report
to the current OpenHarness tool registry without freezing Writer development.
"""

from __future__ import annotations

import copy
import hashlib
import importlib
import importlib.util
import json
import sys
import time
import zipfile
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Mapping, Sequence

from openharness.api.client import (
    ApiMessageCompleteEvent,
    ApiMessageRequest,
    ApiRetryEvent,
    SupportsStreamingMessages,
)
from openharness.engine.messages import ConversationMessage


def _load_execution_contract_builder() -> Any:
    """Load the workspace-local Director contract from script or test entrypoints."""

    try:
        from director_harness.contract import build_execution_contract
    except ModuleNotFoundError as exc:
        if exc.name != "director_harness":
            raise
        workspace_root = next(
            (
                parent
                for parent in Path(__file__).resolve().parents
                if (parent / "director_harness").is_dir()
            ),
            None,
        )
        if workspace_root is None:
            raise
        root_text = str(workspace_root)
        if root_text not in sys.path:
            sys.path.insert(0, root_text)
        from director_harness.contract import build_execution_contract

    return build_execution_contract


WRITER_ARCHIVE_PREFIX = "writer_director_0812/writer_harness_demo/"
WRITER_CORE_FILES = (
    "writer_harness/__init__.py",
    "writer_harness/__main__.py",
    "writer_harness/actor_harness.py",
    "writer_harness/capability_matching.py",
    "writer_harness/cli.py",
    "writer_harness/llm_clients.py",
    "writer_harness/models.py",
    "writer_harness/orchestrator.py",
    "writer_harness/prompts.py",
    "writer_harness/writer_harness.py",
    "writer_harness/test_capability_matching.py",
    "writer_excute.py",
    "writer_excute_multiturn.py",
)
WRITER_SUPPORT_FILES = (
    "DIRECTOR_HARNESS_GUIDE.md",
    "MULTITURN_WRITER_EXECUTE_GUIDE.md",
    "WRITER_DIRECTOR_HARNESS_V1_ 说明文档.md",
    "docs/architecture.md",
    "docs/output_fields.md",
    "examples/run_writer_harness.ps1",
    "examples/run_writer_harness.sh",
)
REQUIRED_REPORT_KEYS = (
    "task_profile",
    "difficulty_profile",
    "execution_plan",
    "difficulty_judgment",
    "judgment_rationale",
    "execution_suggestion",
)
BLOCKING_SUGGESTIONS = {"ask_user", "reject", "defer"}


@dataclass(frozen=True)
class WriterDeploymentVerification:
    """Byte-level deployment audit against the team Writer v1 archive."""

    ok: bool
    workspace_root: str
    package_root: str
    archive_path: str
    archive_sha256: str | None
    archive_available: bool
    archive_match: bool | None
    checked_files: int
    file_sha256: dict[str, str]
    writer_source_version: str = "group_writer_v1"
    missing_files: tuple[str, ...] = ()
    mismatched_files: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        value = asdict(self)
        # Run configurations are persisted as JSON.  Keep the in-memory
        # representation identical to the value loaded back from JSON so a
        # resumed run does not mistake tuples for changed deployment data.
        value["missing_files"] = list(self.missing_files)
        value["mismatched_files"] = list(self.mismatched_files)
        return value


@dataclass(frozen=True)
class WriterCoreBindings:
    """Public call sites loaded from the live external package."""

    module_alias: str
    package_file: str
    get_generated_scripts_template: Any
    get_user_task_label: Any
    detect_language: Any
    extract_report_metadata_from_stdout: Any
    writer_harness_class: Any
    match_openharness_capabilities: Any


@dataclass(frozen=True)
class WriterHandoffResult:
    """One team Writer v1 Actor-to-Writer handoff and its execution decision."""

    schema_version: int
    writer_mode: str
    writer_source_version: str
    actor_model: str
    writer_model: str
    query: str
    source_report: dict[str, Any]
    final_report: dict[str, Any]
    actor_contract: dict[str, Any]
    actor_harness_output: str
    raw_response: str
    core_online_completeness: dict[str, Any]
    judge_completeness_evaluation: dict[str, Any]
    judge_overall_score: int
    core_capability_match: dict[str, Any]
    aligned_capability_match: dict[str, Any]
    execution_decision: dict[str, Any]
    live_tool_names: tuple[str, ...]
    usage: dict[str, int]
    retry_events: tuple[dict[str, Any], ...]
    duration_seconds: float
    request_sha256: str
    response_sha256: str
    tools_exposed_to_writer: int = 0
    regeneration_performed: bool = False
    initial_actor_harness_output: str = ""
    regeneration_raw_response: str | None = None
    phase_metrics: dict[str, dict[str, Any]] = field(default_factory=dict)
    model_request_count: int = 2

    def to_dict(self, *, include_raw_response: bool = True) -> dict[str, Any]:
        value = asdict(self)
        value["execution_contract"] = self.execution_contract
        if not include_raw_response:
            for key in (
                "actor_harness_output",
                "raw_response",
                "initial_actor_harness_output",
                "regeneration_raw_response",
            ):
                value.pop(key, None)
        return value

    @property
    def execution_contract(self) -> dict[str, Any]:
        """Expose the adapter contract without changing the legacy result shape."""
        plan = self.final_report.get("execution_plan")
        milestones = plan.get("milestones") if isinstance(plan, Mapping) else []
        milestones = milestones if isinstance(milestones, list) else []
        source = (
            "writer_legacy_fallback"
            if any(
                isinstance(item, Mapping)
                and str(item.get("id") or "").startswith("fallback-")
                for item in milestones
            )
            else "writer_milestones"
        )
        return {
            "schema_version": 1,
            "source": source,
            "live_tool_names": list(self.live_tool_names),
            "milestones": copy.deepcopy(milestones),
        }

    def prompt_appendix(self) -> str:
        """Return the team Writer v1 final_scripts execution prompt."""

        payload = {
            "writer_mode": self.writer_mode,
            "writer_source_version": self.writer_source_version,
            "writer_policy": "team_writer_v1_scored_final_scripts",
            "user_original_query": self.query,
            "writer_overall_score": self.judge_overall_score,
            "score_band": self.execution_decision.get("score_band"),
            "final_scripts": self.actor_contract,
            "execution_contract": self.execution_contract,
            "capability_match": self.core_capability_match,
            "exact_recommended_tools": self.aligned_capability_match.get("available_tools", []),
        }
        band = str(self.execution_decision.get("score_band") or "low")
        instruction = {
            "high": "Treat final_scripts as approved. Follow its goals, steps, and validation, and prioritize the final content requested by the user.",
            "medium": "Treat final_scripts as basically sufficient. Confirm key preconditions, preserve its cautious strategy, and prioritize the final content requested by the user.",
            "low": "The Writer score is low. Verify key paths, inputs, and assumptions with available tools before relying on final_scripts; state any unresolved blocker clearly.",
        }.get(band, "Use final_scripts as the current execution plan.")
        return (
            "\n\n# Writer Harness pre-execution handoff\n\n"
            + instruction
            + " The original user request and live tool schemas remain authoritative. "
            "Do not regenerate the script. Use final_scripts as the current execution "
            "basis, preserve its risk and validation guidance, and deliver the user-facing "
            "result without forcing a fixed section template. Each mandatory milestone "
            "in execution_contract must be represented by the corresponding exact live "
            "tool when applicable; do not silently drop a required read, write, verify, "
            "pagination, or fan-out step.\n\n"
            + json.dumps(payload, ensure_ascii=False, indent=2)
        )


@dataclass
class WriterEventRecorder:
    """Record the team Writer v1 handoff and observe-only tool lifecycle events."""

    handoff: WriterHandoffResult
    events: list[dict[str, Any]] = field(default_factory=list)
    _next_step: int = 1
    _open_steps: list[tuple[str, str]] = field(default_factory=list)
    _milestone_calls: dict[str, int] = field(default_factory=dict)
    _open_milestones: dict[str, str] = field(default_factory=dict)

    def __post_init__(self) -> None:
        self.events.append(
            {
                "type": "global_plan_created",
                "writer_mode": self.handoff.writer_mode,
                "writer_source_version": self.handoff.writer_source_version,
                "request_sha256": self.handoff.request_sha256,
                "response_sha256": self.handoff.response_sha256,
                "tools_exposed_to_writer": self.handoff.tools_exposed_to_writer,
                "model_request_count": self.handoff.model_request_count,
                "writer_judge_completed": True,
                "regeneration_performed": self.handoff.regeneration_performed,
                "judge_overall_score": self.handoff.judge_overall_score,
                "execution_score_band": self.handoff.execution_decision.get("score_band"),
                    "core_completeness_level": self.handoff.core_online_completeness.get(
                    "completeness_level"
                    ),
                    "execution_contract_source": self.handoff.execution_contract.get("source"),
                    "execution_milestone_count": len(self.handoff.execution_contract.get("milestones", [])),
                }
        )

    def action_proposed(self, tool_name: str, tool_input: Mapping[str, Any]) -> str:
        step_id = f"writer-step-{self._next_step:04d}"
        self._next_step += 1
        recommended = set(
            str(value) for value in self.handoff.aligned_capability_match.get("available_tools", [])
        )
        matched = tool_name in recommended or tool_name.startswith("mcp__")
        milestone_id = self._match_milestone(tool_name)
        if milestone_id:
            self._milestone_calls[milestone_id] = self._milestone_calls.get(milestone_id, 0) + 1
            self._open_milestones[step_id] = milestone_id
        self.events.extend(
            [
                {
                    "type": "step_action_proposed",
                    "step_id": step_id,
                    "tool_name": tool_name,
                    "tool_input": dict(tool_input),
                },
                {
                    "type": "step_check_completed",
                    "step_id": step_id,
                    "tool_name": tool_name,
                    "policy": "team_writer_v1_observe_only",
                    "recommended_tool_match": matched,
                    "milestone_id": milestone_id,
                    "contract_tool_match": bool(milestone_id),
                    "decision": "allow",
                },
                {
                    "type": "action_allowed",
                    "step_id": step_id,
                    "tool_name": tool_name,
                    "reason": "Team Writer v1 has no per-step blocking interface",
                },
            ]
        )
        self._open_steps.append((step_id, tool_name))
        return step_id

    def action_completed(self, tool_name: str, *, is_error: bool) -> str | None:
        match_index = next(
            (
                index
                for index, (_, pending_tool) in enumerate(self._open_steps)
                if pending_tool == tool_name
            ),
            None,
        )
        if match_index is None:
            return None
        step_id, _ = self._open_steps.pop(match_index)
        self.events.append(
            {
                "type": "postcondition_checked",
                "step_id": step_id,
                "tool_name": tool_name,
                "milestone_id": self._open_milestones.pop(step_id, None),
                "policy": "transport_completion_only",
                "tool_is_error": bool(is_error),
            }
        )
        return step_id

    def _match_milestone(self, tool_name: str) -> str | None:
        milestones = self.handoff.execution_contract.get("milestones", [])
        if not isinstance(milestones, list):
            return None
        for item in milestones:
            if not isinstance(item, Mapping):
                continue
            if str(item.get("tool_name") or "").casefold() != tool_name.casefold():
                continue
            milestone_id = str(item.get("id") or "").strip()
            if milestone_id:
                max_calls = item.get("max_calls")
                calls = self._milestone_calls.get(milestone_id, 0)
                if isinstance(max_calls, int) and max_calls > 0 and calls >= max_calls:
                    continue
                return milestone_id
        return None

    def validation(self) -> dict[str, Any]:
        """Check the event linkage needed by the paired smoke gate."""

        by_step: dict[str, set[str]] = {}
        for event in self.events:
            step_id = event.get("step_id")
            if isinstance(step_id, str):
                by_step.setdefault(step_id, set()).add(str(event.get("type")))
        required = {
            "step_action_proposed",
            "step_check_completed",
            "action_allowed",
            "postcondition_checked",
        }
        incomplete = {
            step_id: sorted(required - event_types)
            for step_id, event_types in by_step.items()
            if required - event_types
        }
        global_count = sum(event.get("type") == "global_plan_created" for event in self.events)
        return {
            "writer_mandatory_passed": global_count == 1,
            "event_count": len(self.events),
            "tool_step_count": len(by_step),
            "event_types": sorted({str(event.get("type")) for event in self.events}),
            "incomplete_steps": incomplete,
            "open_steps": [
                {"step_id": step_id, "tool_name": tool_name}
                for step_id, tool_name in self._open_steps
            ],
            "events_complete": not incomplete and not self._open_steps,
            "contract_milestones_started": dict(self._milestone_calls),
        }


class _StaticLLM:
    def __init__(self, response: str) -> None:
        self.response = response

    def complete(self, system_prompt: str, user_prompt: str) -> str:
        del system_prompt, user_prompt
        return self.response


class _PromptCaptureLLM:
    """Capture the exact team Writer v1 judge request without an API call."""

    def __init__(self) -> None:
        self.system_prompt = ""
        self.user_prompt = ""

    def complete(self, system_prompt: str, user_prompt: str) -> str:
        self.system_prompt = system_prompt
        self.user_prompt = user_prompt
        return json.dumps(
            {
                "overall_score": 0,
                "planning_score": 0,
                "structure_score": 0,
                "risk_score": 0,
                "clarification_score": 0,
                "overall_sufficiency": "insufficient",
                "next_action": "re_generate_scripts",
                "section_scores": {},
                "check_scores": {},
                "strengths": [],
                "weaknesses": [],
                "rationale": "prompt capture",
            }
        )


@dataclass(frozen=True)
class _WriterModelCall:
    phase: str
    system_prompt: str
    user_prompt: str
    text: str
    usage: dict[str, int]
    retry_events: tuple[dict[str, Any], ...]
    duration_seconds: float
    stop_reason: str | None


async def _run_writer_model_call(
    *,
    api_client: SupportsStreamingMessages,
    model: str,
    phase: str,
    system_prompt: str,
    user_prompt: str,
    max_tokens: int,
    effort: str | None = None,
    extra_body: Mapping[str, Any] | None = None,
) -> _WriterModelCall:
    """Run one planning-only model phase with callable tools disabled."""

    request = ApiMessageRequest(
        model=model,
        messages=[ConversationMessage.from_user_text(user_prompt)],
        system_prompt=system_prompt,
        max_tokens=max_tokens,
        tools=[],
        effort=effort,
        extra_body=dict(extra_body) if extra_body else None,
    )
    started = time.perf_counter()
    text_parts: list[str] = []
    final_text = ""
    stop_reason: str | None = None
    usage = {"input_tokens": 0, "output_tokens": 0}
    retries: list[dict[str, Any]] = []
    async for event in api_client.stream_message(request):
        if isinstance(event, ApiRetryEvent):
            retries.append(
                {
                    "phase": phase,
                    "attempt": event.attempt,
                    "max_attempts": event.max_attempts,
                    "delay_seconds": event.delay_seconds,
                    "message": event.message,
                }
            )
        elif isinstance(event, ApiMessageCompleteEvent):
            final_text = event.message.text
            stop_reason = event.stop_reason
            usage = {
                "input_tokens": event.usage.input_tokens,
                "output_tokens": event.usage.output_tokens,
            }
        else:
            text = str(getattr(event, "text", ""))
            if text:
                text_parts.append(text)
    return _WriterModelCall(
        phase=phase,
        system_prompt=system_prompt,
        user_prompt=user_prompt,
        text=final_text or "".join(text_parts),
        usage=usage,
        retry_events=tuple(retries),
        duration_seconds=round(time.perf_counter() - started, 6),
        stop_reason=stop_reason,
    )


def _sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def verify_writer_deployment(
    workspace_root: Path,
    archive_path: Path,
    *,
    include_support_files: bool = True,
) -> WriterDeploymentVerification:
    """Require the deployed team Writer v1 files to match the supplied archive."""

    workspace_root = workspace_root.expanduser().resolve()
    archive_path = archive_path.expanduser().resolve()
    relative_files = list(WRITER_CORE_FILES)
    if include_support_files:
        relative_files.extend(WRITER_SUPPORT_FILES)
    missing: list[str] = []
    mismatched: list[str] = []
    hashes: dict[str, str] = {}
    deployed_bytes_by_name: dict[str, bytes] = {}
    for relative in relative_files:
        deployed = workspace_root / relative
        if not deployed.is_file():
            missing.append(relative)
            continue
        deployed_bytes = deployed.read_bytes()
        deployed_bytes_by_name[relative] = deployed_bytes
        hashes[relative] = _sha256_bytes(deployed_bytes)

    archive_available = archive_path.is_file()
    archive_complete = True
    if archive_available:
        with zipfile.ZipFile(archive_path) as archive:
            archive_names = set(archive.namelist())
            for relative, deployed_bytes in deployed_bytes_by_name.items():
                archived_name = WRITER_ARCHIVE_PREFIX + relative
                if archived_name not in archive_names:
                    archive_complete = False
                    continue
                if deployed_bytes != archive.read(archived_name):
                    mismatched.append(relative)
    archive_match = (
        not missing and archive_complete and not mismatched if archive_available else None
    )
    return WriterDeploymentVerification(
        ok=bool(archive_available and archive_match is True),
        workspace_root=str(workspace_root),
        package_root=str(workspace_root / "writer_harness"),
        archive_path=str(archive_path),
        archive_sha256=_sha256_file(archive_path) if archive_available else None,
        archive_available=archive_available,
        archive_match=archive_match,
        checked_files=len(relative_files),
        file_sha256=hashes,
        writer_source_version="group_writer_v1",
        missing_files=tuple(missing),
        mismatched_files=tuple(mismatched),
    )


def load_writer_core(workspace_root: Path) -> WriterCoreBindings:
    """Load the sibling package under a private alias, without copying its code."""

    workspace_root = workspace_root.expanduser().resolve()
    package_root = workspace_root / "writer_harness"
    package_init = package_root / "__init__.py"
    if not package_init.is_file():
        raise FileNotFoundError(f"writer_harness package not found: {package_root}")
    suffix = hashlib.sha256(str(package_root).encode()).hexdigest()[:12]
    alias = f"_openharness_external_writer_{suffix}"
    if alias not in sys.modules:
        spec = importlib.util.spec_from_file_location(
            alias,
            package_init,
            submodule_search_locations=[str(package_root)],
        )
        if spec is None or spec.loader is None:
            raise ImportError(f"could not load external writer package: {package_root}")
        module = importlib.util.module_from_spec(spec)
        sys.modules[alias] = module
        spec.loader.exec_module(module)
    prompts = importlib.import_module(f"{alias}.prompts")
    actor = importlib.import_module(f"{alias}.actor_harness")
    writer = importlib.import_module(f"{alias}.writer_harness")
    matching = importlib.import_module(f"{alias}.capability_matching")
    loaded_path = Path(str(sys.modules[alias].__file__)).resolve()
    if loaded_path != package_init.resolve():
        raise ImportError(
            f"writer_harness resolved outside the requested deployment: {loaded_path}"
        )
    return WriterCoreBindings(
        module_alias=alias,
        package_file=str(loaded_path),
        get_generated_scripts_template=prompts.get_generated_scripts_template,
        get_user_task_label=prompts.get_user_task_label,
        detect_language=prompts.detect_language,
        extract_report_metadata_from_stdout=actor.extract_report_metadata_from_stdout,
        writer_harness_class=writer.WriterHarness,
        match_openharness_capabilities=matching.match_openharness_capabilities,
    )


def _tool_name_and_description(schema: Mapping[str, Any]) -> tuple[str, str]:
    name = str(schema.get("name") or "").strip()
    description = str(schema.get("description") or "").strip()
    return name, description


def _required_parameter_metadata(schema: Mapping[str, Any]) -> dict[str, Any]:
    """Return a compact required-path and enum view of one live tool schema."""

    root = schema.get("input_schema")
    if not isinstance(root, Mapping):
        return {}
    required_paths: list[str] = []
    enum_constraints: dict[str, list[Any]] = {}

    def visit(node: Mapping[str, Any], prefix: str = "") -> None:
        properties = node.get("properties")
        required = node.get("required")
        property_map = properties if isinstance(properties, Mapping) else {}
        required_names = required if isinstance(required, list) else []
        for raw_name in required_names:
            name = str(raw_name)
            path = f"{prefix}.{name}" if prefix else name
            if path not in required_paths and len(required_paths) < 16:
                required_paths.append(path)
            child = property_map.get(name)
            if not isinstance(child, Mapping):
                continue
            enum = child.get("enum")
            if isinstance(enum, list) and enum:
                enum_constraints[path] = list(enum[:12])
            visit(child, path)

    visit(root)
    metadata: dict[str, Any] = {}
    if required_paths:
        metadata["required_parameters"] = required_paths
    if enum_constraints:
        metadata["enum_constraints"] = enum_constraints
    return metadata


def _tool_prompt_record(schema: Mapping[str, Any]) -> dict[str, Any] | None:
    name, description = _tool_name_and_description(schema)
    if not name:
        return None
    record: dict[str, Any] = {"name": name}
    if description:
        record["description"] = description[:240]
    record.update(_required_parameter_metadata(schema))
    return record


def build_writer_request_text(
    bindings: WriterCoreBindings,
    *,
    query: str,
    live_tool_schemas: Sequence[Mapping[str, Any]],
    context: Mapping[str, Any] | None = None,
) -> str:
    """Compose the team Writer v1 script prompt plus live OpenHarness schemas."""

    language = str(bindings.detect_language(query))
    try:
        template = str(
            bindings.get_generated_scripts_template(
                language,
                include_static_tools=False,
            )
        )
    except TypeError:
        # The team Writer v1 prompt has the original one-argument interface.
        template = str(bindings.get_generated_scripts_template(language))
    label = str(bindings.get_user_task_label(language))
    tools = [record for schema in live_tool_schemas if (record := _tool_prompt_record(schema))]
    alignment = (
        "OpenHarness interface alignment (authoritative exact live tool names):\n"
        if language != "zh"
        else "OpenHarness 接口对齐信息（以下为当前运行时权威工具名）：\n"
    )
    context_text = ""
    if context:
        context_text = (
            "\n\nOffline trajectory context (evidence only; do not execute):\n"
            + json.dumps(context, ensure_ascii=False, indent=2)
        )
    contract_instruction = (
        "\n\nIntegration extension: execution_plan may include a milestones array. "
        "Create one milestone for every required business action and preserve fan-out, "
        "pagination, write-then-verify, and multi-stage dependencies. Each milestone "
        "uses this schema: {id, goal, tool_name, required, depends_on, "
        "required_parameters, min_calls, max_calls, max_retries, parameter_bindings, "
        "postcondition}. tool_name must be one exact name from the authoritative live "
        "inventory. Use depends_on milestone IDs. Set bounded call counts only when the "
        "task itself makes the bound explicit; otherwise omit max_calls. A parameter "
        "binding maps a target input name to {from_milestone, output_path}. Keep the "
        "existing Writer v1 fields unchanged."
        if language != "zh"
        else "\n\n接入扩展：execution_plan 可增加 milestones 数组。每个必需业务动作都应有独立 "
        "milestone，并保留 fan-out、分页、写后验证和多阶段依赖。每项采用字段 "
        "{id, goal, tool_name, required, depends_on, required_parameters, min_calls, "
        "max_calls, max_retries, parameter_bindings, postcondition}。tool_name 必须逐字使用"
        "上方权威运行时清单中的名称，depends_on 使用 milestone id。只有任务本身明确"
        "调用上限时才填写 max_calls，否则省略。parameter_bindings 将目标参数映射为 "
        "{from_milestone, output_path}。保留 Writer v1 原有字段不变。"
    )
    return (
        f"{template}\n\n{alignment}"
        f"{json.dumps(tools, ensure_ascii=False, indent=2)}"
        f"{contract_instruction}{context_text}\n\n{label}\n{query}"
    )


def _extract_tool_label(value: Any) -> str:
    return str(value).split("｜", 1)[0].strip()


def align_capability_match(
    core_match: Mapping[str, Any],
    *,
    report: Mapping[str, Any],
    live_tool_names: Sequence[str],
) -> dict[str, Any]:
    """Preserve precise Writer choices and map legacy labels to live names."""

    live = tuple(dict.fromkeys(str(name) for name in live_tool_names if str(name)))
    live_by_fold = {name.casefold(): name for name in live}
    legacy = {
        "read": ("read_file",),
        "grep": ("grep",),
        "glob": ("glob",),
        "ls": ("glob",),
        "apply_patch": ("edit_file", "write_file"),
        "write_file": ("write_file", "edit_file"),
        "edit_file": ("edit_file", "write_file"),
        "deletefile": ("bash",),
        "runcommand": ("bash",),
        "checkcommandstatus": ("bash",),
        "stopcommand": ("bash",),
        "websearch": ("web_search",),
        "webfetch": ("web_fetch",),
        "askuserquestion": ("ask_user_question",),
        "task": ("agent",),
        "integrated_browser(browser tools)": (),
    }
    selected: list[str] = []
    details: list[dict[str, Any]] = []
    unmapped: list[str] = []

    difficulty = report.get("difficulty_profile")
    raw_report_tools = (
        difficulty.get("available_tools", []) if isinstance(difficulty, Mapping) else []
    )
    report_tools = raw_report_tools if isinstance(raw_report_tools, list) else []
    raw_report_missing = (
        difficulty.get("missing_tools", []) if isinstance(difficulty, Mapping) else []
    )
    report_missing = raw_report_missing if isinstance(raw_report_missing, list) else []
    raw_report_requirements = (
        difficulty.get("missing_tool_requirements", [])
        if isinstance(difficulty, Mapping)
        else []
    )
    report_requirement_missing = [
        str(item.get("missing_tool") or item.get("capability") or "").strip()
        for item in raw_report_requirements
        if isinstance(item, Mapping)
        and str(item.get("missing_tool") or item.get("capability") or "").strip()
    ] if isinstance(raw_report_requirements, list) else []
    fallback_tools = list(core_match.get("available_tools", []))

    def add_candidates(candidates: Sequence[tuple[Any, str]]) -> None:
        nonlocal selected
        for candidate, source in candidates:
            label = _extract_tool_label(candidate)
            exact = live_by_fold.get(label.casefold())
            mapped: tuple[str, ...]
            if exact:
                mapped = (exact,)
            elif label.casefold() in {"run_mcp", "mcp"}:
                # The Writer already sees the exact live inventory.  Expanding one
                # generic MCP label to every server tool destroyed its precise
                # selection and greatly enlarged the actor's search space.
                mapped = ()
            else:
                mapped = tuple(
                    live_by_fold[name.casefold()]
                    for name in legacy.get(label.casefold(), ())
                    if name.casefold() in live_by_fold
                )
            if not mapped:
                unmapped.append(label)
                continue
            for name in mapped:
                if name not in selected:
                    selected.append(name)
                details.append(
                    {
                        "source_label": label,
                        "live_tool_name": name,
                        "mapping": "exact" if exact else "openharness_adapter_alias",
                        "source": source,
                    }
                )

    add_candidates([(candidate, "writer_report") for candidate in report_tools])
    # Capability matching is a legacy recovery path.  Once Writer selected at
    # least one exact live tool, broad keyword categories must not add unrelated
    # tools back into the Actor search space.
    if not selected:
        add_candidates(
            [(candidate, "capability_fallback") for candidate in fallback_tools]
        )
    return {
        "available_tools": selected,
        "missing_tools": list(
            dict.fromkeys(
                str(value)
                for value in (
                    list(report_missing)
                    + report_requirement_missing
                    + (
                        list(core_match.get("missing_tools", []))
                        if not report_tools
                        and not report_missing
                        and not report_requirement_missing
                        else []
                    )
                )
                if str(value).strip()
            )
        ),
        "required_capabilities": list(core_match.get("required_capabilities", [])),
        "missing_tool_requirements": list(
            core_match.get("missing_tool_requirements", [])
        ),
        "alignment_details": details,
        "unmapped_source_labels": list(dict.fromkeys(unmapped)),
        "live_tool_count": len(live),
        "policy": (
            "Prefer the Writer's exact live-tool selection; use capability "
            "matching only as a narrow legacy fallback"
        ),
    }


def _prepare_report_for_handoff(
    bindings: WriterCoreBindings,
    *,
    query: str,
    report: Mapping[str, Any],
    live_tool_schemas: Sequence[Mapping[str, Any]],
) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any]]:
    live_tool_names = tuple(
        name
        for name, _ in (_tool_name_and_description(schema) for schema in live_tool_schemas)
        if name
    )
    # Raw model output and Writer-generated tool-list fields used to feed back
    # into keyword matching.  Match task semantics only; exact report tool names
    # are handled separately by ``align_capability_match`` below.
    matching_report = copy.deepcopy(dict(report))
    matching_difficulty = matching_report.get("difficulty_profile")
    if isinstance(matching_difficulty, dict):
        matching_difficulty["available_tools"] = []
        matching_difficulty["missing_tools"] = []
    core_match = bindings.match_openharness_capabilities(
        query,
        matching_report,
        "",
    )
    aligned = align_capability_match(
        core_match,
        report=report,
        live_tool_names=live_tool_names,
    )
    core_match = dict(core_match)
    core_match["available_tools"] = list(aligned.get("available_tools", []))
    core_match["missing_tools"] = list(aligned.get("missing_tools", []))
    prepared = copy.deepcopy(dict(report))
    difficulty = prepared.get("difficulty_profile")
    if isinstance(difficulty, dict):
        report_tools = difficulty.get("available_tools", [])
        report_tools = report_tools if isinstance(report_tools, list) else []
        difficulty["available_tools"] = list(aligned.get("available_tools", []))
        difficulty["missing_tools"] = list(aligned.get("missing_tools", []))
        raw_requirements = difficulty.get("missing_tool_requirements", [])
        if isinstance(raw_requirements, list) and raw_requirements:
            requirements = raw_requirements
        elif report_tools:
            # Do not reintroduce broad keyword-inferred requirements when the
            # Writer already named exact live tools.
            requirements = []
        else:
            requirements = list(core_match.get("missing_tool_requirements", []))
        difficulty["missing_tool_requirements"] = requirements
        core_match["missing_tool_requirements"] = copy.deepcopy(requirements)
        difficulty["required_capabilities"] = list(
            core_match.get("required_capabilities", [])
        )
    return prepared, core_match, aligned


def _attach_explicit_execution_contract(
    report: Mapping[str, Any],
    *,
    live_tool_schemas: Sequence[Mapping[str, Any]],
) -> dict[str, Any]:
    """Expose explicit Writer milestones to the sufficiency judge when present."""
    prepared = copy.deepcopy(dict(report))
    contract = _load_execution_contract_builder()(
        prepared,
        live_tool_schemas=live_tool_schemas,
    )
    if contract.get("source") == "writer_milestones":
        plan = prepared.get("execution_plan")
        if isinstance(plan, dict):
            plan["milestones"] = copy.deepcopy(contract["milestones"])
    return prepared


def _build_judge_request(
    bindings: WriterCoreBindings,
    *,
    query: str,
    actor_output: str,
    report: Mapping[str, Any],
    online_judgment: Any,
) -> tuple[str, str]:
    """Capture the exact request produced by the team Writer v1 judge."""

    capture = _PromptCaptureLLM()
    writer = bindings.writer_harness_class(capture)
    writer.judge_scripts_content(
        query,
        actor_output,
        dict(report),
        list(online_judgment.matched_sections),
        list(online_judgment.missing_sections),
        list(online_judgment.matched_checks),
        list(online_judgment.missing_checks),
    )
    return capture.system_prompt, capture.user_prompt


def _parse_judge_response(
    bindings: WriterCoreBindings,
    *,
    response: str,
    query: str,
    actor_output: str,
    report: Mapping[str, Any],
    online_judgment: Any,
) -> dict[str, Any]:
    writer = bindings.writer_harness_class(_StaticLLM(response))
    evaluation = writer.judge_scripts_content(
        query,
        actor_output,
        dict(report),
        list(online_judgment.matched_sections),
        list(online_judgment.missing_sections),
        list(online_judgment.matched_checks),
        list(online_judgment.missing_checks),
    )
    return dict(evaluation.to_dict())


def _retry_instruction(missing_sections: Sequence[str]) -> str:
    return (
        "\n\n---\nRevise the existing execution script so that it explicitly "
        "includes these core sections as named fields or labeled sections: "
        + ", ".join(str(value) for value in missing_sections)
        + ". Preserve the original user business task, task_profile.task_type, "
        "task_profile.task_goal, task_profile.expected_output, and all valid plan "
        "content. The revision instruction is not the user task and must never "
        "become a task field. Return the revised script JSON only."
    )


def _has_executable_final_script(report: Mapping[str, Any]) -> bool:
    task_profile = report.get("task_profile")
    execution_plan = report.get("execution_plan")
    if not isinstance(task_profile, Mapping) or not isinstance(execution_plan, Mapping):
        return False
    recommended_steps = execution_plan.get("recommended_steps")
    validation_steps = execution_plan.get("validation_steps")
    return (
        bool(str(task_profile.get("task_goal") or "").strip())
        and bool(str(task_profile.get("expected_output") or "").strip())
        and isinstance(recommended_steps, list)
        and any(isinstance(step, str) and step.strip() for step in recommended_steps)
        and isinstance(validation_steps, list)
        and any(isinstance(step, str) and step.strip() for step in validation_steps)
        and bool(str(report.get("execution_suggestion") or "").strip())
    )


def _execution_decision(
    report: Mapping[str, Any],
    evaluation: Mapping[str, Any],
) -> dict[str, Any]:
    if not _has_executable_final_script(report):
        return {
            "should_execute": False,
            "score_band": "blocked",
            "reason": "No content-complete final_script_report was generated.",
        }
    score = evaluation.get("overall_score")
    if isinstance(score, float):
        score = round(score)
    if not isinstance(score, int):
        return {
            "should_execute": False,
            "score_band": "blocked",
            "reason": "Writer judge did not return judge_overall_score.",
        }
    if score > 85:
        band = "high"
        reason = "Writer score is above 85; execute the approved final_scripts."
    elif score >= 70:
        band = "medium"
        reason = "Writer score is 70-85; execute final_scripts cautiously."
    else:
        band = "low"
        reason = (
            "Writer score is below 70; preserve the team implementation's "
            "low-score cautious execution policy."
        )
    return {"should_execute": True, "score_band": band, "reason": reason}


def _aggregate_usage(calls: Sequence[_WriterModelCall]) -> dict[str, int]:
    return {
        "input_tokens": sum(call.usage["input_tokens"] for call in calls),
        "output_tokens": sum(call.usage["output_tokens"] for call in calls),
    }


def _combined_call_hash(calls: Sequence[_WriterModelCall], *, include_responses: bool) -> str:
    values: list[dict[str, str]] = []
    for call in calls:
        value = {
            "phase": call.phase,
            "system_prompt": call.system_prompt,
            "user_prompt": call.user_prompt,
        }
        if include_responses:
            value["response"] = call.text
        values.append(value)
    return _sha256_bytes(json.dumps(values, ensure_ascii=False, sort_keys=True).encode("utf-8"))


def _judgment_to_dict(judgment: Any) -> dict[str, Any]:
    if hasattr(judgment, "__dataclass_fields__"):
        return asdict(judgment)
    if hasattr(judgment, "to_dict"):
        value = judgment.to_dict()
        if isinstance(value, dict):
            return value
    raise TypeError("external Writer completeness result is not serializable")


def _extract_report(
    bindings: WriterCoreBindings,
    raw_response: str,
) -> dict[str, Any]:
    report, _, _, _ = bindings.extract_report_metadata_from_stdout(raw_response)
    return dict(report) if isinstance(report, dict) else {}


async def generate_writer_handoff(
    *,
    api_client: SupportsStreamingMessages,
    model: str,
    actor_model: str | None = None,
    workspace_root: Path,
    query: str,
    live_tool_schemas: Sequence[Mapping[str, Any]],
    context: Mapping[str, Any] | None = None,
    max_tokens: int = 4096,
) -> WriterHandoffResult:
    """Run the team Writer v1 Actor -> Writer judge -> final_scripts protocol."""

    bindings = load_writer_core(workspace_root)
    resolved_actor_model = actor_model or model
    request_text = build_writer_request_text(
        bindings,
        query=query,
        live_tool_schemas=live_tool_schemas,
        context=context,
    )
    started = time.perf_counter()
    draft_call = await _run_writer_model_call(
        api_client=api_client,
        model=resolved_actor_model,
        phase="actor_script_generation",
        system_prompt=(
            "You are the Actor Harness script-generation stage. Generate only "
            "the strict JSON execution script requested by the user prompt. "
            "Do not execute the business task and do not call tools."
        ),
        user_prompt=request_text,
        max_tokens=max_tokens,
    )
    calls = [draft_call]
    initial_actor_output = draft_call.text
    report = _extract_report(bindings, initial_actor_output)
    report = _attach_explicit_execution_contract(
        report,
        live_tool_schemas=live_tool_schemas,
    )
    deterministic_writer = bindings.writer_harness_class(_StaticLLM(""))
    online_judgment = deterministic_writer.judge_online_completeness(
        initial_actor_output,
        round_index=1,
    )
    judge_system, judge_user = _build_judge_request(
        bindings,
        query=query,
        report=report,
        actor_output=initial_actor_output,
        online_judgment=online_judgment,
    )
    judge_call = await _run_writer_model_call(
        api_client=api_client,
        model=model,
        phase="writer_sufficiency_judge",
        system_prompt=judge_system,
        user_prompt=judge_user,
        max_tokens=max_tokens,
        extra_body={"thinking": {"type": "disabled"}},
    )
    calls.append(judge_call)
    evaluation = _parse_judge_response(
        bindings,
        response=judge_call.text,
        query=query,
        actor_output=initial_actor_output,
        report=report,
        online_judgment=online_judgment,
    )

    regeneration_performed = False
    regeneration_raw_response: str | None = None
    actor_output = initial_actor_output
    if not online_judgment.is_complete:
        regeneration_performed = True
        retry_call = await _run_writer_model_call(
            api_client=api_client,
            model=resolved_actor_model,
            phase="actor_script_regeneration",
            system_prompt=(
                "You are the Actor Harness script-generation stage. Revise only "
                "the existing execution script and return strict JSON. Do not "
                "execute the business task and do not call tools."
            ),
            user_prompt=request_text
            + _retry_instruction(online_judgment.missing_sections),
            max_tokens=max_tokens,
        )
        calls.append(retry_call)
        regeneration_raw_response = retry_call.text
        actor_output = retry_call.text
        report = _extract_report(bindings, actor_output)
        report = _attach_explicit_execution_contract(
            report,
            live_tool_schemas=live_tool_schemas,
        )
        online_judgment = deterministic_writer.judge_online_completeness(
            actor_output,
            round_index=2,
        )
        judge_system, judge_user = _build_judge_request(
            bindings,
            query=query,
            actor_output=actor_output,
            report=report,
            online_judgment=online_judgment,
        )
        retry_judge_call = await _run_writer_model_call(
            api_client=api_client,
            model=model,
            phase="writer_sufficiency_judge_after_regeneration",
            system_prompt=judge_system,
            user_prompt=judge_user,
            max_tokens=max_tokens,
            extra_body={"thinking": {"type": "disabled"}},
        )
        calls.append(retry_judge_call)
        evaluation = _parse_judge_response(
            bindings,
            response=retry_judge_call.text,
            query=query,
            actor_output=actor_output,
            report=report,
            online_judgment=online_judgment,
        )

    live_names = tuple(
        name
        for name, _ in (
            _tool_name_and_description(schema) for schema in live_tool_schemas
        )
        if name
    )
    final_report, core_match, aligned = _prepare_report_for_handoff(
        bindings,
        query=query,
        report=report,
        live_tool_schemas=live_tool_schemas,
    )
    execution_contract = _load_execution_contract_builder()(
        final_report,
        live_tool_schemas=live_tool_schemas,
    )
    final_plan = final_report.get("execution_plan")
    if not isinstance(final_plan, dict):
        final_plan = {}
        final_report["execution_plan"] = final_plan
    final_plan["milestones"] = copy.deepcopy(
        execution_contract.get("milestones", [])
    )
    execution_decision = _execution_decision(final_report, evaluation)
    judgment = _judgment_to_dict(online_judgment)
    actor_contract = copy.deepcopy(final_report)
    phase_metrics = {
        call.phase: {
            **dict(call.usage),
            "duration_seconds": call.duration_seconds,
            "stop_reason": call.stop_reason,
        }
        for call in calls
    }
    retries = tuple(event for call in calls for event in call.retry_events)
    score = int(evaluation.get("overall_score", 0) or 0)
    return WriterHandoffResult(
        schema_version=1,
        writer_mode="team_writer_harness_v1",
        writer_source_version="group_writer_v1",
        actor_model=resolved_actor_model,
        writer_model=model,
        query=query,
        source_report=report,
        final_report=final_report,
        actor_contract=actor_contract,
        actor_harness_output=actor_output,
        raw_response=actor_output,
        core_online_completeness=judgment,
        judge_completeness_evaluation=evaluation,
        judge_overall_score=score,
        core_capability_match=core_match,
        aligned_capability_match=aligned,
        execution_decision=execution_decision,
        live_tool_names=live_names,
        usage=_aggregate_usage(calls),
        retry_events=retries,
        duration_seconds=round(time.perf_counter() - started, 6),
        request_sha256=_combined_call_hash(calls, include_responses=False),
        response_sha256=_combined_call_hash(calls, include_responses=True),
        regeneration_performed=regeneration_performed,
        initial_actor_harness_output=initial_actor_output,
        regeneration_raw_response=regeneration_raw_response,
        phase_metrics=phase_metrics,
        model_request_count=len(calls),
    )


def writer_predicted_block(report: Mapping[str, Any]) -> bool:
    """Map the v1 report's global suggestion to the offline binary metric."""

    return str(report.get("execution_suggestion") or "").strip().casefold() in BLOCKING_SUGGESTIONS
