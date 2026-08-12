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


WRITER_ARCHIVE_PREFIX = "writer_harness_demo/"
WRITER_CORE_FILES = (
    "writer_harness/__init__.py",
    "writer_harness/__main__.py",
    "writer_harness/actor_harness.py",
    "writer_harness/capability_matching.py",
    "writer_harness/cli.py",
    "writer_harness/contract.py",
    "writer_harness/llm_clients.py",
    "writer_harness/models.py",
    "writer_harness/orchestrator.py",
    "writer_harness/prompts.py",
    "writer_harness/review.py",
    "writer_harness/writer_harness.py",
    "writer_excute.py",
)
WRITER_SUPPORT_FILES = (
    "Writer_Harness_Overview.md",
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
    """Non-blocking provenance audit against the historical team archive."""

    ok: bool
    workspace_root: str
    package_root: str
    archive_path: str
    archive_sha256: str | None
    archive_available: bool
    archive_match: bool | None
    checked_files: int
    file_sha256: dict[str, str]
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
    prepare_actor_handoff: Any
    prepare_actor_prompt_handoff: Any
    audit_writer_report: Any
    build_writer_review_request: Any
    parse_writer_review: Any
    writer_review_requires_revision: Any
    apply_writer_review_patch: Any


@dataclass(frozen=True)
class WriterHandoffResult:
    """One no-tool Writer report and its OpenHarness-side alignment metadata."""

    schema_version: int
    writer_mode: str
    writer_model: str
    source_report: dict[str, Any]
    final_report: dict[str, Any]
    actor_contract: dict[str, Any]
    raw_response: str
    core_online_completeness: dict[str, Any]
    core_capability_match: dict[str, Any]
    aligned_capability_match: dict[str, Any]
    live_tool_names: tuple[str, ...]
    usage: dict[str, int]
    retry_events: tuple[dict[str, Any], ...]
    duration_seconds: float
    request_sha256: str
    response_sha256: str
    tools_exposed_to_writer: int = 0
    semantic_review: dict[str, Any] = field(default_factory=dict)
    review_raw_response: str = ""
    revision_raw_response: str | None = None
    model_request_count: int = 1

    def to_dict(self, *, include_raw_response: bool = True) -> dict[str, Any]:
        value = asdict(self)
        if not include_raw_response:
            for key in (
                "raw_response",
                "review_raw_response",
                "revision_raw_response",
            ):
                value.pop(key, None)
        return value

    def prompt_appendix(self) -> str:
        """Return the reviewed report and execution guardrails for the actor."""

        payload = {
            "writer_mode": self.writer_mode,
            "writer_policy": "reviewed_pre_execution_contract_v3_observe_only",
            "task_contract": self.actor_contract,
            "exact_recommended_tools": self.aligned_capability_match.get("available_tools", []),
        }
        return (
            "\n\n# Writer Harness pre-execution handoff\n\n"
            "The original user request and live tool schemas remain authoritative. "
            "Use this reviewed contract as a concise checklist. Values discovered "
            "from files or tools, including IDs, tokens, enum values, field names, "
            "and paths, must be copied verbatim rather than renamed or normalized. "
            "Resolve required parameters before mutation, verify observable state "
            "afterward, and replan on a listed trigger.\n\n"
            + json.dumps(payload, ensure_ascii=False, indent=2)
        )


@dataclass
class WriterEventRecorder:
    """Record honest v1 handoff and observe-only per-tool lifecycle events."""

    handoff: WriterHandoffResult
    events: list[dict[str, Any]] = field(default_factory=list)
    _next_step: int = 1
    _open_steps: list[tuple[str, str]] = field(default_factory=list)

    def __post_init__(self) -> None:
        self.events.append(
            {
                "type": "global_plan_created",
                "writer_mode": self.handoff.writer_mode,
                "request_sha256": self.handoff.request_sha256,
                "response_sha256": self.handoff.response_sha256,
                "tools_exposed_to_writer": self.handoff.tools_exposed_to_writer,
                "model_request_count": self.handoff.model_request_count,
                "semantic_review_performed": self.handoff.semantic_review.get("performed", False),
                "revision_performed": self.handoff.semantic_review.get("revision_performed", False),
                "final_contract_review_passed": self.handoff.semantic_review.get(
                    "final_contract_audit", {}
                ).get("passed"),
                "core_completeness_level": self.handoff.core_online_completeness.get(
                    "completeness_level"
                ),
            }
        )

    def action_proposed(self, tool_name: str, tool_input: Mapping[str, Any]) -> str:
        step_id = f"writer-step-{self._next_step:04d}"
        self._next_step += 1
        recommended = set(
            str(value) for value in self.handoff.aligned_capability_match.get("available_tools", [])
        )
        matched = tool_name in recommended or tool_name.startswith("mcp__")
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
                    "policy": "writer_v3_observe_only",
                    "recommended_tool_match": matched,
                    "decision": "allow",
                },
                {
                    "type": "action_allowed",
                    "step_id": step_id,
                    "tool_name": tool_name,
                    "reason": "Writer v3 has no per-step blocking interface",
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
                "policy": "transport_completion_only",
                "tool_is_error": bool(is_error),
            }
        )
        return step_id

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
        }


class _NoLLM:
    def complete(self, system_prompt: str, user_prompt: str) -> str:
        del system_prompt, user_prompt
        raise RuntimeError("The deterministic completeness check must not call an LLM")


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
    """Record live hashes and compare with the historical zip for provenance only."""

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
        # ``ok`` means the live Writer source is complete. Historical archive
        # identity is provenance only and never locks an experiment run.
        ok=not missing,
        workspace_root=str(workspace_root),
        package_root=str(workspace_root / "writer_harness"),
        archive_path=str(archive_path),
        archive_sha256=_sha256_file(archive_path) if archive_available else None,
        archive_available=archive_available,
        archive_match=archive_match,
        checked_files=len(relative_files),
        file_sha256=hashes,
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
    try:
        contract = importlib.import_module(f"{alias}.contract")
    except ModuleNotFoundError as exc:
        if exc.name != f"{alias}.contract":
            raise
        contract = None
    try:
        review = importlib.import_module(f"{alias}.review")
    except ModuleNotFoundError as exc:
        if exc.name != f"{alias}.review":
            raise
        review = None
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
        prepare_actor_handoff=getattr(writer, "prepare_actor_handoff", None),
        prepare_actor_prompt_handoff=getattr(writer, "prepare_actor_prompt_handoff", None),
        audit_writer_report=(getattr(contract, "audit_writer_report", None) if contract else None),
        build_writer_review_request=(
            getattr(review, "build_writer_review_request", None) if review else None
        ),
        parse_writer_review=(getattr(review, "parse_writer_review", None) if review else None),
        writer_review_requires_revision=(
            getattr(review, "writer_review_requires_revision", None) if review else None
        ),
        apply_writer_review_patch=(
            getattr(review, "apply_writer_review_patch", None) if review else None
        ),
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
    """Compose the original Writer prompt plus a current tool-name handoff."""

    language = str(bindings.detect_language(query))
    try:
        template = str(
            bindings.get_generated_scripts_template(
                language,
                include_static_tools=False,
            )
        )
    except TypeError:
        # Backward compatibility for archived Writer implementations.
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
    return (
        f"{template}\n\n{alignment}"
        f"{json.dumps(tools, ensure_ascii=False, indent=2)}"
        f"{context_text}\n\n{label}\n{query}"
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
        "apply_patch": ("edit_file",),
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
    candidates = [(candidate, "writer_report") for candidate in report_tools] + [
        (candidate, "capability_fallback") for candidate in core_match.get("available_tools", [])
    ]
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
    return {
        "available_tools": selected,
        "missing_tools": list(
            dict.fromkeys(
                str(value)
                for value in list(report_missing) + list(core_match.get("missing_tools", []))
                if str(value).strip()
            )
        ),
        "required_capabilities": list(core_match.get("required_capabilities", [])),
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
    raw_response: str,
    live_tool_names: Sequence[str],
) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any]]:
    core_match = bindings.match_openharness_capabilities(
        query,
        report,
        raw_response,
    )
    aligned = align_capability_match(
        core_match,
        report=report,
        live_tool_names=live_tool_names,
    )
    prepared = copy.deepcopy(dict(report))
    difficulty = prepared.get("difficulty_profile")
    if isinstance(difficulty, dict):
        difficulty["available_tools"] = aligned["available_tools"]
        difficulty["missing_tools"] = aligned["missing_tools"]
        difficulty["required_capabilities"] = aligned["required_capabilities"]
    if callable(bindings.prepare_actor_handoff):
        prepared = bindings.prepare_actor_handoff(
            prepared,
            query=query,
            live_tool_names=live_tool_names,
        )
    return prepared, dict(core_match), aligned


def _audit_report(
    bindings: WriterCoreBindings,
    report: Mapping[str, Any],
    *,
    query: str,
) -> dict[str, Any]:
    if not callable(bindings.audit_writer_report):
        audit = report.get("contract_audit")
        return dict(audit) if isinstance(audit, Mapping) else {"passed": True}
    try:
        value = bindings.audit_writer_report(report, query=query)
    except TypeError:
        value = bindings.audit_writer_report(report)
    if not isinstance(value, Mapping):
        raise TypeError("external Writer contract audit is not serializable")
    return dict(value)


def _selected_tool_schemas(
    report: Mapping[str, Any],
    live_tool_schemas: Sequence[Mapping[str, Any]],
) -> list[dict[str, Any]]:
    """Expose complete schemas only for exact tools selected in the draft."""

    selected_names: list[str] = []
    difficulty = report.get("difficulty_profile")
    if isinstance(difficulty, Mapping) and isinstance(difficulty.get("available_tools"), list):
        selected_names.extend(str(name) for name in difficulty["available_tools"])
    plan = report.get("execution_plan")
    steps = plan.get("recommended_steps") if isinstance(plan, Mapping) else None
    for step in steps if isinstance(steps, list) else []:
        if not isinstance(step, Mapping) or not isinstance(step.get("tools"), list):
            continue
        selected_names.extend(str(name) for name in step["tools"])
    selected = set(name for name in selected_names if name)
    return [
        copy.deepcopy(dict(schema))
        for schema in live_tool_schemas
        if _tool_name_and_description(schema)[0] in selected
    ]


def _supports_v3_review(bindings: WriterCoreBindings, report: Mapping[str, Any]) -> bool:
    try:
        report_version = int(report.get("report_version", 0) or 0)
    except (TypeError, ValueError):
        return False
    return report_version == 3 and all(
        callable(value)
        for value in (
            bindings.build_writer_review_request,
            bindings.parse_writer_review,
            bindings.writer_review_requires_revision,
            bindings.apply_writer_review_patch,
        )
    )


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


def _validated_report(
    bindings: WriterCoreBindings,
    raw_response: str,
) -> dict[str, Any]:
    report, _, _, _ = bindings.extract_report_metadata_from_stdout(raw_response)
    if not isinstance(report, dict):
        raise ValueError("Writer report is not a parseable JSON object")
    missing = [key for key in REQUIRED_REPORT_KEYS if key not in report]
    if missing:
        raise ValueError(
            "Writer report is missing required top-level fields: " + ", ".join(missing)
        )
    return report


async def generate_writer_handoff(
    *,
    api_client: SupportsStreamingMessages,
    model: str,
    workspace_root: Path,
    query: str,
    live_tool_schemas: Sequence[Mapping[str, Any]],
    context: Mapping[str, Any] | None = None,
    max_tokens: int = 4096,
) -> WriterHandoffResult:
    """Draft, review, locally repair, and compile one Writer v3 handoff."""

    bindings = load_writer_core(workspace_root)
    request_text = build_writer_request_text(
        bindings,
        query=query,
        live_tool_schemas=live_tool_schemas,
        context=context,
    )
    qwen_no_thinking = {"enable_thinking": False} if "qwen" in model.casefold() else None
    qwen_json_review = (
        {
            "enable_thinking": False,
            "response_format": {"type": "json_object"},
        }
        if "qwen" in model.casefold()
        else None
    )
    started = time.perf_counter()
    draft_call = await _run_writer_model_call(
        api_client=api_client,
        model=model,
        phase="draft",
        system_prompt=(
            "Generate only the requested Writer Harness JSON report. "
            "Do not execute the task and do not claim to have called tools."
        ),
        user_prompt=request_text,
        max_tokens=max_tokens,
        extra_body=qwen_no_thinking,
    )
    calls = [draft_call]
    raw_response = draft_call.text
    report = _validated_report(bindings, raw_response)
    live_names = tuple(
        name
        for name, _ in (_tool_name_and_description(schema) for schema in live_tool_schemas)
        if name
    )
    final_report, core_match, aligned = _prepare_report_for_handoff(
        bindings,
        query=query,
        report=report,
        live_tool_names=live_names,
        raw_response=raw_response,
    )
    initial_audit = _audit_report(bindings, final_report, query=query)
    final_audit = initial_audit
    review_raw_response = ""
    revision_raw_response: str | None = None
    semantic_result: dict[str, Any] = {
        "passed": bool(initial_audit.get("passed", True)),
        "defects": [],
        "summary": "Legacy report path: independent Writer v3 review was not requested.",
    }
    revision_performed = False
    revision_accepted = False
    selected_schemas: list[dict[str, Any]] = []
    live_tool_catalog = [
        record for schema in live_tool_schemas if (record := _tool_prompt_record(schema))
    ]
    review_enabled = _supports_v3_review(bindings, report)
    if review_enabled:
        language = str(bindings.detect_language(query))
        selected_schemas = _selected_tool_schemas(final_report, live_tool_schemas)
        review_system, review_user = bindings.build_writer_review_request(
            query=query,
            report=final_report,
            deterministic_audit=initial_audit,
            selected_tool_schemas=selected_schemas,
            live_tool_catalog=live_tool_catalog,
            language=language,
        )
        review_call = await _run_writer_model_call(
            api_client=api_client,
            model=model,
            phase="review",
            system_prompt=str(review_system),
            user_prompt=str(review_user),
            max_tokens=max(512, min(max_tokens, 2048)),
            effort="low",
            extra_body=qwen_json_review,
        )
        calls.append(review_call)
        review_raw_response = review_call.text
        try:
            semantic_result = dict(bindings.parse_writer_review(review_raw_response))
        except (TypeError, ValueError) as exc:
            semantic_result = {
                "passed": bool(initial_audit.get("passed")),
                "defects": [],
                "summary": "Semantic review response could not be parsed.",
                "parse_error": str(exc),
            }
        should_revise = bool(
            bindings.writer_review_requires_revision(
                initial_audit,
                semantic_result,
            )
        )
        if should_revise:
            patch_errors = semantic_result.get("patch_errors")
            patch_operations = semantic_result.get("patch_operations")
            if patch_errors:
                semantic_result["revision_error"] = (
                    "Semantic review returned invalid patch operations: "
                    + "; ".join(str(error) for error in patch_errors)
                )
            elif not isinstance(patch_operations, list) or not patch_operations:
                semantic_result["revision_error"] = (
                    "Semantic review found contract defects without an applicable patch"
                )
            else:
                revision_performed = True
                try:
                    patched_report = bindings.apply_writer_review_patch(
                        final_report,
                        patch_operations,
                    )
                    candidate_report, candidate_core, candidate_aligned = (
                        _prepare_report_for_handoff(
                            bindings,
                            query=query,
                            report=patched_report,
                            raw_response=review_raw_response,
                            live_tool_names=live_names,
                        )
                    )
                    candidate_audit = _audit_report(
                        bindings,
                        candidate_report,
                        query=query,
                    )
                    semantic_result["candidate_contract_audit"] = candidate_audit
                    if not bool(candidate_audit.get("passed")):
                        raise ValueError(
                            "Locally patched Writer report did not pass the "
                            "deterministic contract audit"
                        )
                    final_report = candidate_report
                    core_match = candidate_core
                    aligned = candidate_aligned
                    final_audit = candidate_audit
                    revision_accepted = True
                except (TypeError, ValueError) as exc:
                    semantic_result["revision_error"] = str(exc)

    writer = bindings.writer_harness_class(_NoLLM())
    judgment = _judgment_to_dict(
        writer.judge_online_completeness(
            json.dumps(final_report, ensure_ascii=False),
            round_index=2 if revision_performed else 1,
        )
    )
    actor_contract = (
        bindings.prepare_actor_prompt_handoff(final_report)
        if callable(bindings.prepare_actor_prompt_handoff)
        else copy.deepcopy(final_report)
    )
    semantic_review = {
        "performed": review_enabled,
        "selected_tool_names": [
            _tool_name_and_description(schema)[0] for schema in selected_schemas
        ],
        "initial_contract_audit": initial_audit,
        "result": semantic_result,
        "revision_performed": revision_performed,
        "revision_accepted": revision_accepted,
        "revision_mode": "review_patch_local" if review_enabled else "none",
        "final_contract_audit": final_audit,
        "usage_by_phase": {call.phase: dict(call.usage) for call in calls},
        "phase_metrics": {
            call.phase: {
                **dict(call.usage),
                "duration_seconds": call.duration_seconds,
                "stop_reason": call.stop_reason,
            }
            for call in calls
        },
    }
    retries = tuple(event for call in calls for event in call.retry_events)
    writer_mode = "external_writer_harness_v3" if review_enabled else "external_writer_harness_v2"
    return WriterHandoffResult(
        schema_version=3 if review_enabled else 2,
        writer_mode=writer_mode,
        writer_model=model,
        source_report=report,
        final_report=final_report,
        actor_contract=actor_contract,
        raw_response=raw_response,
        core_online_completeness=judgment,
        core_capability_match=core_match,
        aligned_capability_match=aligned,
        live_tool_names=live_names,
        usage=_aggregate_usage(calls),
        retry_events=retries,
        duration_seconds=round(time.perf_counter() - started, 6),
        request_sha256=_combined_call_hash(calls, include_responses=False),
        response_sha256=_combined_call_hash(calls, include_responses=True),
        semantic_review=semantic_review,
        review_raw_response=review_raw_response,
        revision_raw_response=revision_raw_response,
        model_request_count=len(calls),
    )


def writer_predicted_block(report: Mapping[str, Any]) -> bool:
    """Map the v1 report's global suggestion to the offline binary metric."""

    return str(report.get("execution_suggestion") or "").strip().casefold() in BLOCKING_SUGGESTIONS
