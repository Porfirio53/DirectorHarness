"""Thin OpenHarness runtime bridge for HarnessBench's ``generic_cli`` adapter."""

from __future__ import annotations

import hashlib
import json
import os
import re
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any, Mapping, Sequence

if TYPE_CHECKING:
    from openharness.api.client import SupportsStreamingMessages  # type: ignore[import-untyped]
    from openharness.engine.stream_events import StreamEvent  # type: ignore[import-untyped]

_SAFE_SEGMENT = re.compile(r"[^A-Za-z0-9_.-]+")
_SENSITIVE_ENV_NAME = re.compile(
    r"(?:API_?KEY|TOKEN|SECRET|PASSWORD|CREDENTIAL)",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class HarnessBenchRoundConfig:
    """Inputs supplied by one HarnessBench ``generic_cli`` adapter round."""

    workspace: Path
    sandbox: Path
    prompt_file: Path
    session_id: str
    task_id: str
    model: str | None = None
    active_profile: str | None = None
    api_format: str | None = None
    temperature: float | None = None
    seed: int | None = None
    max_turns: int | None = None
    api_timeout_sec: float = 300.0
    permission_mode: str = "full_auto"
    openharness_mode: str = "original"
    writer_workspace_root: Path | None = None
    writer_model: str | None = None
    writer_max_tokens: int = 4096
    director_harness_enabled: bool = False
    director_mcp_catalog: Path | None = None


@dataclass(frozen=True)
class HarnessBenchRoundResult:
    """Non-secret status emitted by the bridge and persisted beside the sandbox."""

    status: str
    task_id: str
    session_id: str
    workspace: str
    session_restored: bool
    assistant_text: str
    error_events: tuple[str, ...]
    tool_calls: int
    tool_errors: int
    session_file: str
    trace_file: str
    writer_required: bool
    writer_mandatory_passed: bool
    writer_state_unchanged: bool | None
    writer_event_validation: dict[str, Any] | None
    writer_usage: dict[str, int]
    writer_report_file: str | None
    director_enabled: bool
    director_event_validation: dict[str, Any] | None
    director_events: tuple[dict[str, Any], ...]
    director_log_file: str | None


def _safe_segment(value: str, fallback: str) -> str:
    candidate = _SAFE_SEGMENT.sub("-", value).strip("-._")
    return candidate[:80] or fallback


def validate_round_config(config: HarnessBenchRoundConfig) -> None:
    """Reject mismatched or unsafe adapter inputs before starting OpenHarness."""

    workspace = config.workspace.resolve()
    sandbox = config.sandbox.resolve()
    prompt_file = config.prompt_file.resolve()
    if not workspace.is_dir():
        raise ValueError(f"HarnessBench workspace is not a directory: {workspace}")
    if not prompt_file.is_file():
        raise ValueError(f"HarnessBench prompt file is missing: {prompt_file}")
    if not config.session_id.strip():
        raise ValueError("HarnessBench session ID must not be empty")
    if not config.task_id.strip():
        raise ValueError("HarnessBench task ID must not be empty")
    if config.api_timeout_sec <= 0:
        raise ValueError("OpenHarness API timeout must be positive")
    if config.openharness_mode not in {"original", "writer_harness"}:
        raise ValueError(
            f"unsupported OpenHarness mode: {config.openharness_mode}"
        )
    if config.writer_max_tokens < 1:
        raise ValueError("Writer max tokens must be positive")
    if (
        config.openharness_mode == "writer_harness"
        and config.writer_workspace_root is None
    ):
        raise ValueError(
            "writer_workspace_root is required in writer_harness mode"
        )
    if config.director_harness_enabled:
        if config.director_mcp_catalog is None:
            raise ValueError(
                "director_mcp_catalog is required when Director is enabled"
            )
        if not config.director_mcp_catalog.resolve().is_file():
            raise ValueError(
                "Director MCP catalog is missing: "
                f"{config.director_mcp_catalog.resolve()}"
            )
    if workspace != sandbox and sandbox not in workspace.parents:
        raise ValueError(f"workspace must be inside sandbox: {workspace} not under {sandbox}")

    expected = {
        "HARNESSBENCH_WORKSPACE": workspace,
        "HARNESSBENCH_SANDBOX": sandbox,
        "HARNESSBENCH_PROMPT_FILE": prompt_file,
    }
    for name, actual in expected.items():
        raw = os.environ.get(name, "").strip()
        if raw and Path(raw).resolve() != actual:
            raise ValueError(f"{name} does not match the wrapper argument")
    expected_text = {
        "HARNESSBENCH_SESSION_ID": config.session_id,
        "HARNESSBENCH_TASK_ID": config.task_id,
    }
    for name, actual_text in expected_text.items():
        raw = os.environ.get(name, "").strip()
        if raw and raw != actual_text:
            raise ValueError(f"{name} does not match the wrapper argument")


def configure_isolated_runtime(config: HarnessBenchRoundConfig) -> Path:
    """Put session state and logs inside this HarnessBench sandbox."""

    state_root = config.sandbox.resolve() / "openharness-harnessbench"
    data_dir = state_root / "data"
    logs_dir = state_root / "logs"
    data_dir.mkdir(parents=True, exist_ok=True)
    logs_dir.mkdir(parents=True, exist_ok=True)
    os.environ["OPENHARNESS_DATA_DIR"] = str(data_dir)
    os.environ["OPENHARNESS_LOGS_DIR"] = str(logs_dir)
    if config.director_harness_enabled:
        director_log = state_root / "director" / "events.jsonl"
        os.environ["DIRECTOR_HARNESS_ENABLED"] = "true"
        os.environ["DIRECTOR_MCP_CATALOG"] = str(
            config.director_mcp_catalog.resolve()
        )
        os.environ["DIRECTOR_LOG_PATH"] = str(director_log)
    else:
        os.environ["DIRECTOR_HARNESS_ENABLED"] = "false"
        os.environ.pop("DIRECTOR_MCP_CATALOG", None)
        os.environ.pop("DIRECTOR_LOG_PATH", None)
    return state_root


def _remove_sensitive_tool_environment() -> dict[str, str]:
    """Hide credentials from workspace tools after the API client has captured auth."""

    removed: dict[str, str] = {}
    for name in tuple(os.environ):
        if not _SENSITIVE_ENV_NAME.search(name):
            continue
        removed[name] = os.environ.pop(name)
    return removed


def _restore_environment(removed: dict[str, str]) -> None:
    os.environ.update(removed)


def _default_upstream(api_format: str) -> str:
    if api_format in {"openai", "openai_compat"}:
        return "https://api.openai.com/v1"
    return "https://api.anthropic.com"


def register_usage_proxy_route(config: HarnessBenchRoundConfig) -> str | None:
    """Register OpenHarness's upstream and return its HarnessBench proxy URL."""

    proxy_base = os.environ.get("HARNESSBENCH_LLM_PROXY_URL", "").strip().rstrip("/")
    routes_raw = os.environ.get("HARNESSBENCH_LLM_PROXY_ROUTES", "").strip()
    if not proxy_base or not routes_raw:
        return None

    from openharness.config import load_settings  # type: ignore[import-untyped]

    settings = load_settings().merge_cli_overrides(
        model=config.model,
        active_profile=config.active_profile,
        api_format=config.api_format,
    )
    upstream = str(settings.base_url or "").strip().rstrip("/")
    api_format = str(settings.api_format or config.api_format or "anthropic")
    if not upstream:
        upstream = _default_upstream(api_format)

    prefix = (
        "/openharness/"
        f"{_safe_segment(config.task_id, 'task')}/"
        f"{_safe_segment(config.session_id, 'session')}"
    )
    routes_file = Path(routes_raw).expanduser().resolve()
    routes_file.parent.mkdir(parents=True, exist_ok=True)
    routes: dict[str, Any] = {}
    if routes_file.is_file():
        try:
            loaded = json.loads(routes_file.read_text(encoding="utf-8"))
            if isinstance(loaded, dict):
                routes = loaded
        except json.JSONDecodeError:
            routes = {}
    routes[prefix] = {
        "upstream": upstream,
        "framework": "openharness",
        "provider": str(settings.provider or api_format),
    }
    routes_file.write_text(
        json.dumps(routes, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    return f"{proxy_base}{prefix}"


def _build_tuned_openai_client(
    config: HarnessBenchRoundConfig,
    *,
    proxy_url: str | None,
) -> SupportsStreamingMessages | None:
    if config.temperature is None and config.seed is None:
        return None

    from openharness.api.openai_client import OpenAICompatibleClient  # type: ignore[import-untyped]
    from openharness.config import load_settings

    settings = load_settings().merge_cli_overrides(
        model=config.model,
        active_profile=config.active_profile,
        api_format=config.api_format,
        base_url=proxy_url,
    )
    if settings.api_format not in {"openai", "openai_compat"}:
        raise ValueError("temperature/seed are supported only for OpenAI-compatible profiles")
    auth = settings.resolve_auth()
    return OpenAICompatibleClient(
        api_key=auth.value,
        base_url=proxy_url or settings.base_url,
        timeout=max(float(settings.timeout), config.api_timeout_sec),
        temperature=config.temperature,
        seed=config.seed,
    )


def _append_trace(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as stream:
        stream.write(json.dumps(payload, ensure_ascii=False) + "\n")


def _write_json(path: Path, payload: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.tmp")
    temporary.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, default=str) + "\n",
        encoding="utf-8",
    )
    temporary.replace(path)


def _pre_director_input_rejection(
    *,
    tool_name: str,
    output: str,
    is_error: bool,
) -> bool:
    """Return whether OpenHarness rejected a call before Director preflight."""

    return is_error and output.lstrip().startswith(
        f"Invalid input for {tool_name}:"
    )


def _director_event_validation(
    *,
    enabled: bool,
    tool_calls: int,
    input_validation_rejected_calls: int,
    events: Sequence[Mapping[str, Any]],
    stream_order: Sequence[str],
) -> dict[str, Any] | None:
    if not enabled:
        return None
    tool_use_ids = {
        str(event.get("tool_use_id") or "")
        for event in events
        if str(event.get("tool_use_id") or "")
    }
    eligible_tool_call_count = tool_calls - input_validation_rejected_calls
    director_before_completion = True
    director_seen = 0
    completed_seen = 0
    for event_type in stream_order:
        if event_type == "director_event":
            director_seen += 1
        elif event_type == "tool_completed":
            completed_seen += 1
            if director_seen < completed_seen:
                director_before_completion = False
    return {
        "enabled": True,
        "event_count": len(events),
        "checked_tool_use_count": len(tool_use_ids),
        "tool_call_count": tool_calls,
        "all_tool_calls_checked": len(tool_use_ids) == eligible_tool_call_count,
        "director_before_tool_completion": director_before_completion,
        "event_types": sorted(
            {str(event.get("event") or "") for event in events if event.get("event")}
        ),
        "statuses": sorted(
            {str(event.get("status") or "") for event in events if event.get("status")}
        ),
    }


def _workspace_file_snapshot(root: Path) -> dict[str, str]:
    """Return content hashes for the workspace files visible to Writer."""

    snapshot: dict[str, str] = {}
    for path in sorted(value for value in root.rglob("*") if value.is_file()):
        relative = path.relative_to(root).as_posix()
        snapshot[relative] = hashlib.sha256(path.read_bytes()).hexdigest()
    return snapshot


def _writer_planning_state_unchanged(
    before: Mapping[str, str],
    after: Mapping[str, str],
) -> bool:
    """Allow only Benchmark-owned delayed additions beneath ``in/``."""

    for relative in set(before) | set(after):
        if before.get(relative) == after.get(relative):
            continue
        if (
            relative.startswith("in/")
            and relative not in before
            and relative in after
        ):
            continue
        return False
    return True


async def execute_harnessbench_round(
    config: HarnessBenchRoundConfig,
    *,
    api_client: SupportsStreamingMessages | None = None,
) -> HarnessBenchRoundResult:
    """Execute one prompt, restoring the exact HarnessBench session when present."""

    validate_round_config(config)
    state_root = configure_isolated_runtime(config)
    prompt = config.prompt_file.read_text(encoding="utf-8")

    from openharness.services.session_storage import load_session_by_id  # type: ignore[import-untyped]
    from openharness.engine.stream_events import (
        AssistantTextDelta,
        DirectorEventEmitted,
        ErrorEvent,
        ToolExecutionCompleted,
        ToolExecutionStarted,
    )
    from openharness.ui.runtime import (  # type: ignore[import-untyped]
        build_runtime,
        close_runtime,
        handle_line,
        start_runtime,
    )
    from openharness.rehearsal.writer_handoff import (
        WriterEventRecorder,
        generate_writer_handoff,
    )

    restored = load_session_by_id(config.workspace, config.session_id)
    proxy_url = register_usage_proxy_route(config) if api_client is None else None
    resolved_client = api_client or _build_tuned_openai_client(config, proxy_url=proxy_url)
    restored_messages = restored.get("messages") if restored else None
    restored_metadata = restored.get("tool_metadata") if restored else None
    restored_model = str(restored.get("model") or "").strip() if restored else ""
    removed_environment = (
        _remove_sensitive_tool_environment() if resolved_client is not None else {}
    )
    try:
        bundle = await build_runtime(
            prompt=prompt,
            cwd=str(config.workspace.resolve()),
            model=config.model or restored_model or None,
            active_profile=config.active_profile,
            api_format=config.api_format,
            base_url=proxy_url,
            max_turns=config.max_turns,
            permission_mode=config.permission_mode,
            restore_messages=restored_messages if isinstance(restored_messages, list) else None,
            restore_tool_metadata=restored_metadata if isinstance(restored_metadata, dict) else None,
            api_client=resolved_client,
            enforce_max_turns=True,
        )
        bundle.session_id = config.session_id
        bundle.engine.tool_metadata["session_id"] = config.session_id
        writer_required = config.openharness_mode == "writer_harness"
        writer_handoff = None
        writer_recorder: WriterEventRecorder | None = None
        writer_state_before: dict[str, str] | None = None
        writer_state_after: dict[str, str] | None = None
        writer_state_unchanged: bool | None = None
        writer_report_file: Path | None = None
        if writer_required:
            if config.writer_workspace_root is None:
                raise ValueError(
                    "writer_workspace_root is required in writer_harness mode"
                )
            writer_state_before = _workspace_file_snapshot(
                config.workspace.resolve()
            )
            writer_handoff = await generate_writer_handoff(
                api_client=bundle.api_client,
                model=config.writer_model or bundle.engine.model,
                actor_model=bundle.engine.model,
                workspace_root=config.writer_workspace_root,
                query=prompt,
                live_tool_schemas=bundle.tool_registry.to_api_schema(),
                max_tokens=config.writer_max_tokens,
            )
            writer_state_after = _workspace_file_snapshot(
                config.workspace.resolve()
            )
            writer_state_unchanged = _writer_planning_state_unchanged(
                writer_state_before,
                writer_state_after,
            )
            if not writer_state_unchanged:
                raise RuntimeError(
                    "Writer planning changed the HarnessBench workspace "
                    "before actor execution"
                )
            if not writer_handoff.execution_decision.get("should_execute"):
                raise RuntimeError(
                    "Writer did not produce a content-complete final_scripts "
                    "execution input"
                )
            # Keep the contract in the same mutable metadata object consumed by
            # QueryContext and Director; no new execution path is introduced.
            bundle.engine.tool_metadata["writer_execution_contract"] = (
                writer_handoff.execution_contract
            )
            writer_recorder = WriterEventRecorder(writer_handoff)
            writer_report_file = (
                state_root
                / "writer"
                / (
                    f"{_safe_segment(config.session_id, 'session')}-"
                    f"{_safe_segment(config.prompt_file.stem, 'round')}.json"
                )
            )
            _write_json(
                writer_report_file,
                writer_handoff.to_dict(),
            )

        assistant_parts: list[str] = []
        error_events: list[str] = []
        tool_calls = 0
        tool_errors = 0
        input_validation_rejected_calls = 0
        director_events: list[dict[str, Any]] = []
        stream_order: list[str] = []

        async def print_system(message: str) -> None:
            if message:
                error_events.append(message)

        async def render_event(event: StreamEvent) -> None:
            nonlocal tool_calls, tool_errors, input_validation_rejected_calls
            if isinstance(event, AssistantTextDelta):
                assistant_parts.append(event.text)
            elif isinstance(event, ErrorEvent):
                error_events.append(event.message)
            elif isinstance(event, ToolExecutionStarted):
                tool_calls += 1
                stream_order.append("tool_started")
                if writer_recorder is not None:
                    writer_recorder.action_proposed(
                        event.tool_name,
                        event.tool_input,
                    )
            elif isinstance(event, DirectorEventEmitted):
                stream_order.append("director_event")
                director_events.append(
                    {
                        "event": event.event,
                        "tool_name": event.tool_name,
                        "requested_tool_name": event.requested_tool_name,
                        "status": event.status,
                        "detail": event.detail,
                        "session_id": event.session_id,
                        "tool_use_id": event.tool_use_id,
                        "timestamp": event.timestamp,
                        "data": event.data or {},
                    }
                )
            elif isinstance(event, ToolExecutionCompleted) and event.is_error:
                if _pre_director_input_rejection(
                    tool_name=event.tool_name,
                    output=event.output,
                    is_error=event.is_error,
                ):
                    input_validation_rejected_calls += 1
                    stream_order.append("tool_input_rejected")
                else:
                    stream_order.append("tool_completed")
                tool_errors += 1
                if writer_recorder is not None:
                    writer_recorder.action_completed(
                        event.tool_name,
                        is_error=True,
                    )
            elif isinstance(event, ToolExecutionCompleted):
                stream_order.append("tool_completed")
                if writer_recorder is not None:
                    writer_recorder.action_completed(
                        event.tool_name,
                        is_error=False,
                    )

        async def clear_output() -> None:
            return None

        await start_runtime(bundle)
        try:
            await handle_line(
                bundle,
                prompt,
                print_system=print_system,
                render_event=render_event,
                clear_output=clear_output,
                system_prompt_suffix=(
                    writer_handoff.prompt_appendix()
                    if writer_handoff is not None
                    else None
                ),
            )
        finally:
            await close_runtime(bundle)

        session_file = (
            bundle.session_backend.get_session_dir(config.workspace)
            / f"session-{config.session_id}.json"
        )
        if error_events:
            status = "agent_task_failed"
        elif tool_errors:
            status = "completed_with_tool_errors"
        else:
            status = "completed"
        trace_file = state_root / "rounds.jsonl"
        writer_validation = (
            writer_recorder.validation()
            if writer_recorder is not None
            else None
        )
        director_validation = _director_event_validation(
            enabled=config.director_harness_enabled,
            tool_calls=tool_calls,
            input_validation_rejected_calls=input_validation_rejected_calls,
            events=director_events,
            stream_order=stream_order,
        )
        director_log_file = (
            state_root / "director" / "events.jsonl"
            if config.director_harness_enabled
            else None
        )
        result = HarnessBenchRoundResult(
            status=status,
            task_id=config.task_id,
            session_id=config.session_id,
            workspace=str(config.workspace.resolve()),
            session_restored=restored is not None,
            assistant_text="".join(assistant_parts).strip(),
            error_events=tuple(error_events),
            tool_calls=tool_calls,
            tool_errors=tool_errors,
            session_file=str(session_file),
            trace_file=str(trace_file),
            writer_required=writer_required,
            writer_mandatory_passed=writer_handoff is not None,
            writer_state_unchanged=writer_state_unchanged,
            writer_event_validation=writer_validation,
            writer_usage=(
                dict(writer_handoff.usage)
                if writer_handoff is not None
                else {"input_tokens": 0, "output_tokens": 0}
            ),
            writer_report_file=(
                str(writer_report_file)
                if writer_report_file is not None
                else None
            ),
            director_enabled=config.director_harness_enabled,
            director_event_validation=director_validation,
            director_events=tuple(director_events),
            director_log_file=(
                str(director_log_file)
                if director_log_file is not None
                else None
            ),
        )
        trace_payload = asdict(result)
        trace_payload.pop("assistant_text", None)
        _append_trace(trace_file, trace_payload)
        return result
    finally:
        _restore_environment(removed_environment)


def public_result_payload(result: HarnessBenchRoundResult) -> dict[str, Any]:
    """Return a compact payload suitable for captured adapter stdout."""

    payload = asdict(result)
    payload.pop("assistant_text", None)
    payload["result_label"] = "OpenHarness-compatible local result"
    return payload
