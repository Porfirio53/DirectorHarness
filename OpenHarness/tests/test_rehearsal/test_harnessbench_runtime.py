from __future__ import annotations

import importlib.util
import json
import os
from dataclasses import replace
from pathlib import Path
from typing import AsyncIterator

import pytest

from openharness.api.client import (  # type: ignore[import-untyped]
    ApiMessageCompleteEvent,
    ApiMessageRequest,
    ApiStreamEvent,
)
from openharness.api.usage import UsageSnapshot  # type: ignore[import-untyped]
from openharness.engine.messages import (  # type: ignore[import-untyped]
    ConversationMessage,
    TextBlock,
    ToolUseBlock,
)
from openharness.rehearsal.harnessbench_runtime import (  # type: ignore[import-untyped]
    HarnessBenchRoundConfig,
    _workspace_file_snapshot,
    _writer_planning_state_unchanged,
    configure_isolated_runtime,
    execute_harnessbench_round,
    public_result_payload,
    register_usage_proxy_route,
    validate_round_config,
)


class StaticApiClient:
    def __init__(self, text: str) -> None:
        self.text = text
        self.requests: list[ApiMessageRequest] = []

    async def stream_message(
        self,
        request: ApiMessageRequest,
    ) -> AsyncIterator[ApiStreamEvent]:
        self.requests.append(request)
        yield ApiMessageCompleteEvent(
            message=ConversationMessage(
                role="assistant",
                content=[TextBlock(text=self.text)],
            ),
            usage=UsageSnapshot(input_tokens=2, output_tokens=1),
            stop_reason=None,
        )


class WriterThenActorApiClient:
    def __init__(self, *, writer_input_injection: Path | None = None) -> None:
        self.requests: list[ApiMessageRequest] = []
        self.writer_input_injection = writer_input_injection

    async def stream_message(
        self,
        request: ApiMessageRequest,
    ) -> AsyncIterator[ApiStreamEvent]:
        self.requests.append(request)
        if not request.tools and "script-generation stage" in (
            request.system_prompt or ""
        ):
            if self.writer_input_injection is not None:
                self.writer_input_injection.parent.mkdir(parents=True, exist_ok=True)
                self.writer_input_injection.write_text("delayed update", encoding="utf-8")
            text = json.dumps(
                {
                    "task_profile": {
                        "task_type": "file",
                        "task_goal": "complete the workspace task",
                        "success_criteria": ["output exists"],
                        "expected_output": "workspace output",
                    },
                    "difficulty_profile": {
                        "difficulty": "low",
                        "available_tools": ["read_file", "write_file"],
                        "missing_tools": [],
                        "missing_tool_requirements": [],
                        "known_conditions": ["local workspace"],
                        "unknown_conditions": ["file contents"],
                        "estimated_cost": "small",
                    },
                    "execution_plan": {
                        "pre_execution_thoughts": ["preserve fixtures"],
                        "recommended_steps": [
                            "read_file: read input",
                            "write_file: write output",
                        ],
                        "validation_steps": ["verify output"],
                    },
                    "difficulty_judgment": "low",
                    "judgment_rationale": ["local task"],
                    "execution_suggestion": "execute",
                }
            )
        elif not request.tools:
            text = json.dumps(
                {
                    "overall_score": 90,
                    "planning_score": 90,
                    "structure_score": 90,
                    "risk_score": 90,
                    "clarification_score": 90,
                    "overall_sufficiency": "sufficient",
                    "next_action": "execute",
                    "section_scores": {},
                    "check_scores": {},
                    "strengths": ["complete"],
                    "weaknesses": [],
                    "rationale": "offline",
                }
            )
        else:
            text = "actor completed"
        yield ApiMessageCompleteEvent(
            message=ConversationMessage(
                role="assistant",
                content=[TextBlock(text=text)],
            ),
            usage=UsageSnapshot(input_tokens=2, output_tokens=1),
            stop_reason=None,
        )


class ScriptedApiClient:
    def __init__(
        self,
        workspace: Path,
        *,
        fail_tool: bool = False,
        invalid_input: bool = False,
    ) -> None:
        self.workspace = workspace
        self.fail_tool = fail_tool
        self.invalid_input = invalid_input
        self.calls = 0

    async def stream_message(
        self,
        request: ApiMessageRequest,
    ) -> AsyncIterator[ApiStreamEvent]:
        del request
        self.calls += 1
        if self.calls == 1:
            message = ConversationMessage(
                role="assistant",
                content=[
                    ToolUseBlock(
                        id="toolu_linecount",
                        name="bash",
                        input=(
                            {}
                            if self.invalid_input
                            else {
                                "command": (
                                    "exit 3"
                                    if self.fail_tool
                                    else (
                                        "mkdir -p out && "
                                        "wc -l < in/input.txt | tr -d ' ' > out/linecount.txt"
                                    )
                                ),
                                "cwd": str(self.workspace),
                            }
                        ),
                    )
                ],
            )
        else:
            message = ConversationMessage(
                role="assistant",
                content=[TextBlock(text="completed")],
            )
        yield ApiMessageCompleteEvent(
            message=message,
            usage=UsageSnapshot(input_tokens=2, output_tokens=1),
            stop_reason=None,
        )


class EnvironmentProbeApiClient:
    def __init__(self, workspace: Path) -> None:
        self.workspace = workspace
        self.calls = 0

    async def stream_message(
        self,
        request: ApiMessageRequest,
    ) -> AsyncIterator[ApiStreamEvent]:
        del request
        self.calls += 1
        if self.calls == 1:
            message = ConversationMessage(
                role="assistant",
                content=[
                    ToolUseBlock(
                        id="toolu_environment",
                        name="bash",
                        input={
                            "command": (
                                "mkdir -p out && "
                                "printf '%s' \"${OPENAI_API_KEY-}\" > out/environment-key.txt"
                            ),
                            "cwd": str(self.workspace),
                        },
                    )
                ],
            )
        else:
            message = ConversationMessage(
                role="assistant",
                content=[TextBlock(text="completed")],
            )
        yield ApiMessageCompleteEvent(
            message=message,
            usage=UsageSnapshot(input_tokens=2, output_tokens=1),
            stop_reason=None,
        )


def _round_config(
    sandbox: Path,
    *,
    prompt: str = "Do the task.",
    session_id: str = "hb-session-1",
) -> HarnessBenchRoundConfig:
    workspace = sandbox / "workspace"
    workspace.mkdir(parents=True)
    (workspace / "in").mkdir()
    (workspace / "out").mkdir()
    prompt_file = sandbox / "prompt-round1.txt"
    prompt_file.write_text(prompt, encoding="utf-8")
    return HarnessBenchRoundConfig(
        workspace=workspace,
        sandbox=sandbox,
        prompt_file=prompt_file,
        session_id=session_id,
        task_id="001-file",
        model="offline-test-model",
    )


def _reset_prompt(config: HarnessBenchRoundConfig, name: str, content: str) -> HarnessBenchRoundConfig:
    prompt_file = config.sandbox / name
    prompt_file.write_text(content, encoding="utf-8")
    return replace(config, prompt_file=prompt_file)


def test_wrapper_validates_harnessbench_arguments_and_environment(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = _round_config(tmp_path / "sandbox")
    monkeypatch.setenv("HARNESSBENCH_WORKSPACE", str(config.workspace))
    monkeypatch.setenv("HARNESSBENCH_SANDBOX", str(config.sandbox))
    monkeypatch.setenv("HARNESSBENCH_PROMPT_FILE", str(config.prompt_file))
    monkeypatch.setenv("HARNESSBENCH_SESSION_ID", config.session_id)
    monkeypatch.setenv("HARNESSBENCH_TASK_ID", config.task_id)

    validate_round_config(config)
    monkeypatch.setenv("HARNESSBENCH_SESSION_ID", "wrong-session")
    with pytest.raises(ValueError, match="does not match"):
        validate_round_config(config)

    with pytest.raises(ValueError, match="timeout must be positive"):
        validate_round_config(replace(config, api_timeout_sec=0))


def test_isolated_runtime_paths_stay_inside_sandbox(tmp_path: Path) -> None:
    config = _round_config(tmp_path / "sandbox")
    state_root = configure_isolated_runtime(config)

    assert state_root == config.sandbox / "openharness-harnessbench"
    assert Path(state_root / "data").is_dir()
    assert Path(state_root / "logs").is_dir()


def test_writer_state_allows_only_new_benchmark_input_files(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    (workspace / "in").mkdir(parents=True)
    (workspace / "out").mkdir()
    existing_input = workspace / "in" / "initial.json"
    existing_input.write_text("initial", encoding="utf-8")

    before = _workspace_file_snapshot(workspace)
    (workspace / "in" / "delayed.json").write_text("delayed", encoding="utf-8")
    assert _writer_planning_state_unchanged(
        before,
        _workspace_file_snapshot(workspace),
    )

    before = _workspace_file_snapshot(workspace)
    existing_input.write_text("changed", encoding="utf-8")
    assert not _writer_planning_state_unchanged(
        before,
        _workspace_file_snapshot(workspace),
    )

    before = _workspace_file_snapshot(workspace)
    (workspace / "out" / "unexpected.txt").write_text("changed", encoding="utf-8")
    assert not _writer_planning_state_unchanged(
        before,
        _workspace_file_snapshot(workspace),
    )


def test_usage_proxy_route_has_upstream_but_no_credentials(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = replace(_round_config(tmp_path / "sandbox"), active_profile="qwen")
    config_dir = tmp_path / "config"
    config_dir.mkdir()
    (config_dir / "settings.json").write_text(
        json.dumps(
            {
                "active_profile": "qwen",
                "model": "qwen3.6-plus",
                "api_key": "must-not-be-copied",
            }
        ),
        encoding="utf-8",
    )
    routes = tmp_path / "routes.json"
    monkeypatch.setenv("OPENHARNESS_CONFIG_DIR", str(config_dir))
    monkeypatch.setenv("HARNESSBENCH_LLM_PROXY_URL", "http://127.0.0.1:4321")
    monkeypatch.setenv("HARNESSBENCH_LLM_PROXY_ROUTES", str(routes))

    proxy_url = register_usage_proxy_route(config)
    body = routes.read_text(encoding="utf-8")

    assert proxy_url is not None
    assert proxy_url.startswith("http://127.0.0.1:4321/openharness/")
    assert "dashscope.aliyuncs.com" in body
    assert "must-not-be-copied" not in body


@pytest.mark.asyncio
async def test_multiround_keeps_session_id_and_repeat_isolation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("OPENHARNESS_CONFIG_DIR", str(tmp_path / "config"))
    first = _round_config(tmp_path / "repeat-1" / "sandbox", prompt="round one")
    first_result = await execute_harnessbench_round(first, api_client=StaticApiClient("remembered"))
    second = _reset_prompt(first, "prompt-round2.txt", "round two")
    second_client = StaticApiClient("recalled")
    second_result = await execute_harnessbench_round(second, api_client=second_client)

    assert first_result.session_id == second_result.session_id == "hb-session-1"
    assert first_result.session_restored is False
    assert second_result.session_restored is True
    assert len(second_client.requests[0].messages) >= 3
    assert first_result.session_file == second_result.session_file

    isolated = _round_config(
        tmp_path / "repeat-2" / "sandbox",
        prompt="fresh repeat",
        session_id="hb-session-1",
    )
    isolated_result = await execute_harnessbench_round(
        isolated,
        api_client=StaticApiClient("fresh"),
    )
    assert isolated_result.session_restored is False
    assert isolated_result.session_file != first_result.session_file


@pytest.mark.asyncio
async def test_scripted_agent_completes_deterministic_workspace_task(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("OPENHARNESS_CONFIG_DIR", str(tmp_path / "config"))
    config = _round_config(tmp_path / "sandbox")
    (config.workspace / "in" / "input.txt").write_text("a\nb\nc\nd\n", encoding="utf-8")

    result = await execute_harnessbench_round(
        config,
        api_client=ScriptedApiClient(config.workspace),
    )

    assert result.status == "completed"
    assert result.tool_calls == 1
    assert result.tool_errors == 0
    assert (config.workspace / "out" / "linecount.txt").read_text().strip() == "4"
    assert "assistant_text" not in public_result_payload(result)


@pytest.mark.asyncio
async def test_harnessbench_director_checks_tool_before_completion(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("OPENHARNESS_CONFIG_DIR", str(tmp_path / "config"))
    workspace_root = Path(__file__).resolve().parents[3]
    base = _round_config(tmp_path / "sandbox")
    config = replace(
        base,
        director_harness_enabled=True,
        director_mcp_catalog=(
            workspace_root / "director_harness" / "director_mcp_catalog.json"
        ),
    )
    (config.workspace / "in" / "input.txt").write_text(
        "a\nb\n",
        encoding="utf-8",
    )

    result = await execute_harnessbench_round(
        config,
        api_client=ScriptedApiClient(config.workspace),
    )

    assert result.status == "completed"
    assert result.director_enabled is True
    assert result.director_event_validation is not None
    assert result.director_event_validation["all_tool_calls_checked"] is True
    assert result.director_event_validation[
        "director_before_tool_completion"
    ] is True
    assert result.director_events[0]["event"] == "tool_check"
    assert result.director_events[0]["status"] == "passed"
    assert result.director_events[0]["tool_use_id"] == "toolu_linecount"
    assert result.director_log_file is not None
    assert Path(result.director_log_file).is_file()


@pytest.mark.asyncio
async def test_harnessbench_director_gate_excludes_invalid_tool_input(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("OPENHARNESS_CONFIG_DIR", str(tmp_path / "config"))
    workspace_root = Path(__file__).resolve().parents[3]
    base = _round_config(tmp_path / "sandbox")
    config = replace(
        base,
        director_harness_enabled=True,
        director_mcp_catalog=(
            workspace_root / "director_harness" / "director_mcp_catalog.json"
        ),
    )
    (config.workspace / "in" / "input.txt").write_text(
        "a\nb\n",
        encoding="utf-8",
    )

    result = await execute_harnessbench_round(
        config,
        api_client=ScriptedApiClient(config.workspace, invalid_input=True),
    )

    assert result.status == "completed_with_tool_errors"
    assert result.tool_calls == 1
    assert result.tool_errors == 1
    assert result.director_event_validation is not None
    assert result.director_event_validation["tool_call_count"] == 1
    assert result.director_event_validation["checked_tool_use_count"] == 0
    assert result.director_event_validation["all_tool_calls_checked"] is True
    assert result.director_event_validation[
        "director_before_tool_completion"
    ] is True


@pytest.mark.asyncio
async def test_writer_handoff_is_mandatory_no_tool_and_state_isolated(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("OPENHARNESS_CONFIG_DIR", str(tmp_path / "config"))
    workspace_root = Path(__file__).resolve().parents[3]
    config = replace(
        _round_config(tmp_path / "sandbox"),
        openharness_mode="writer_harness",
        writer_workspace_root=workspace_root,
        writer_model="offline-writer",
    )
    client = WriterThenActorApiClient()

    result = await execute_harnessbench_round(config, api_client=client)

    assert len(client.requests) == 3
    assert client.requests[0].tools == []
    assert client.requests[1].tools == []
    assert client.requests[2].tools
    assert "Writer Harness pre-execution handoff" in (
        client.requests[2].system_prompt or ""
    )
    assert result.writer_required is True
    assert result.writer_mandatory_passed is True
    assert result.writer_state_unchanged is True
    assert result.writer_event_validation is not None
    assert result.writer_event_validation["events_complete"] is True
    assert result.writer_report_file is not None
    assert Path(result.writer_report_file).is_file()


@pytest.mark.asyncio
async def test_writer_handoff_tolerates_delayed_benchmark_input(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("OPENHARNESS_CONFIG_DIR", str(tmp_path / "config"))
    workspace_root = Path(__file__).resolve().parents[3]
    config = replace(
        _round_config(tmp_path / "sandbox"),
        openharness_mode="writer_harness",
        writer_workspace_root=workspace_root,
        writer_model="offline-writer",
    )
    client = WriterThenActorApiClient(
        writer_input_injection=config.workspace / "in" / "delayed.json"
    )

    result = await execute_harnessbench_round(config, api_client=client)

    assert result.status == "completed"
    assert result.writer_state_unchanged is True
    assert len(client.requests) == 3


@pytest.mark.asyncio
async def test_scripted_agent_completes_real_harnessbench_oracle(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    harnessbench_root = Path(__file__).resolve().parents[3] / "HarnessBench"
    oracle_path = harnessbench_root / "tasks" / "001-file" / "oracle_grade.py"
    if not oracle_path.is_file():
        pytest.skip("sibling HarnessBench checkout is not available")
    monkeypatch.setenv("OPENHARNESS_CONFIG_DIR", str(tmp_path / "config"))
    config = _round_config(tmp_path / "sandbox")
    source = "alpha\nbeta\ngamma\ndelta\n"
    (config.workspace / "in" / "input.txt").write_text(source, encoding="utf-8")

    await execute_harnessbench_round(
        config,
        api_client=ScriptedApiClient(config.workspace),
    )

    spec = importlib.util.spec_from_file_location("harnessbench_task_001_oracle", oracle_path)
    assert spec is not None and spec.loader is not None
    oracle = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(oracle)
    oracle_result = oracle.score_workspace(config.workspace)
    assert oracle_result["outcome_score"] == 1.0
    assert (config.workspace / "in" / "input.txt").read_text(encoding="utf-8") == source


@pytest.mark.asyncio
async def test_workspace_tools_cannot_read_api_credentials(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    secret = "secret-that-must-not-reach-tools"
    monkeypatch.setenv("OPENHARNESS_CONFIG_DIR", str(tmp_path / "config"))
    monkeypatch.setenv("OPENAI_API_KEY", secret)
    config = _round_config(tmp_path / "sandbox")

    result = await execute_harnessbench_round(
        config,
        api_client=EnvironmentProbeApiClient(config.workspace),
    )

    assert (config.workspace / "out" / "environment-key.txt").read_text() == ""
    assert secret not in Path(result.session_file).read_text(encoding="utf-8")
    assert os.environ["OPENAI_API_KEY"] == secret


@pytest.mark.asyncio
async def test_recoverable_tool_error_is_not_reported_as_agent_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("OPENHARNESS_CONFIG_DIR", str(tmp_path / "config"))
    config = _round_config(tmp_path / "sandbox")

    result = await execute_harnessbench_round(
        config,
        api_client=ScriptedApiClient(config.workspace, fail_tool=True),
    )

    assert result.status == "completed_with_tool_errors"
    assert result.tool_errors == 1
