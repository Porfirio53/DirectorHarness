from __future__ import annotations

import json
from pathlib import Path
from typing import AsyncIterator

import pytest

from openharness.api.client import (
    ApiMessageCompleteEvent,
    ApiMessageRequest,
    ApiStreamEvent,
)
from openharness.api.usage import UsageSnapshot
from openharness.engine.messages import ConversationMessage, TextBlock
from openharness.rehearsal.writer_handoff import (
    WRITER_CORE_FILES,
    WriterEventRecorder,
    align_capability_match,
    build_writer_request_text,
    generate_writer_handoff,
    load_writer_core,
    verify_writer_deployment,
)
from director_harness.contract import build_execution_contract


class SequencedWriterClient:
    def __init__(self, responses: list[dict[str, object]]) -> None:
        self.responses = responses
        self.requests: list[ApiMessageRequest] = []

    async def stream_message(
        self,
        request: ApiMessageRequest,
    ) -> AsyncIterator[ApiStreamEvent]:
        self.requests.append(request)
        index = len(self.requests) - 1
        if index >= len(self.responses):
            raise AssertionError("Writer issued more model requests than expected")
        yield ApiMessageCompleteEvent(
            message=ConversationMessage(
                role="assistant",
                content=[TextBlock(text=json.dumps(self.responses[index], ensure_ascii=False))],
            ),
            usage=UsageSnapshot(input_tokens=10, output_tokens=5),
            stop_reason="stop",
        )


def _workspace_root() -> Path:
    return Path(__file__).resolve().parents[3]


def _report() -> dict[str, object]:
    return {
        "task_profile": {
            "task_type": "workspace",
            "task_goal": "Read a file and write a count",
            "success_criteria": ["correct count"],
            "expected_output": "one file",
        },
        "difficulty_profile": {
            "difficulty": "low",
            "available_tools": ["read_file", "write_file"],
            "missing_tools": [],
            "missing_tool_requirements": [],
            "known_conditions": ["local workspace"],
            "unknown_conditions": ["file contents"],
            "estimated_cost": "two tool calls",
        },
        "execution_plan": {
            "pre_execution_thoughts": ["preserve input"],
            "recommended_steps": [
                "read_file: read input",
                "write_file: write output",
            ],
            "validation_steps": ["verify output"],
        },
        "difficulty_judgment": "low risk",
        "judgment_rationale": ["local deterministic task"],
        "execution_suggestion": "execute",
    }


def _evaluation(score: int = 90) -> dict[str, object]:
    return {
        "overall_score": score,
        "planning_score": score,
        "structure_score": score,
        "risk_score": score,
        "clarification_score": score,
        "overall_sufficiency": "sufficient" if score > 85 else "insufficient",
        "next_action": "execute" if score > 85 else "re_generate_scripts",
        "section_scores": {},
        "check_scores": {},
        "strengths": ["complete script"],
        "weaknesses": [],
        "rationale": "offline evaluation",
    }


def test_deployed_writer_matches_team_archive_byte_for_byte() -> None:
    workspace = _workspace_root()
    result = verify_writer_deployment(
        workspace,
        workspace / "docs" / "writer_director_0812.zip",
        include_support_files=False,
    )

    assert result.checked_files == len(WRITER_CORE_FILES)
    assert result.missing_files == ()
    assert result.mismatched_files == ()
    assert set(result.file_sha256) == set(WRITER_CORE_FILES)
    assert result.ok is True
    assert result.archive_match is True
    assert result.writer_source_version == "group_writer_v1"

    serialized = result.to_dict()
    assert serialized["missing_files"] == []
    assert serialized["mismatched_files"] == list(result.mismatched_files)
    assert json.loads(json.dumps(serialized)) == serialized


def test_external_core_loads_from_workspace_without_copying() -> None:
    workspace = _workspace_root()
    bindings = load_writer_core(workspace)

    assert Path(bindings.package_file) == workspace / "writer_harness/__init__.py"
    prompt = bindings.get_generated_scripts_template("en")
    assert "Do not execute the task." in prompt
    assert "Do not call tools." in prompt


def test_writer_request_uses_live_tools_without_static_catalog_noise() -> None:
    bindings = load_writer_core(_workspace_root())
    request = build_writer_request_text(
        bindings,
        query="Send one text message to the group.",
        live_tool_schemas=[
            {
                "name": "mcp__lark__send_message",
                "description": "Send a message.",
                "input_schema": {
                    "type": "object",
                    "properties": {
                        "data": {
                            "type": "object",
                            "properties": {
                                "message_type": {
                                    "type": "string",
                                    "enum": ["text", "post"],
                                }
                            },
                            "required": ["message_type"],
                        }
                    },
                    "required": ["data"],
                },
            }
        ],
    )

    assert '"name": "mcp__lark__send_message"' in request
    assert '"data.message_type"' in request
    assert '"text"' in request
    assert "The actor Harness (OpenHarness) trusted inventory" in request
    assert "authoritative exact live tool names" in request


def test_capability_alignment_preserves_source_and_uses_live_names() -> None:
    core = {
        "available_tools": [
            "Read｜read a file",
            "apply_patch｜edit a file",
            "run_mcp｜external tools",
        ],
        "missing_tools": [],
        "required_capabilities": ["文件与代码检索", "扩展系统调用"],
    }

    aligned = align_capability_match(
        core,
        report=_report(),
        live_tool_names=[
            "read_file",
            "edit_file",
            "bash",
            "mcp__slack__find_users",
        ],
    )

    assert aligned["available_tools"] == [
        "read_file",
        "edit_file",
    ]
    assert core["available_tools"][0].startswith("Read")
    assert {item["mapping"] for item in aligned["alignment_details"]} == {
        "exact",
        "openharness_adapter_alias",
    }


def test_mcp_wildcard_does_not_replace_precise_writer_selection() -> None:
    aligned = align_capability_match(
        {
            "available_tools": ["run_mcp｜external tools"],
            "missing_tools": [],
            "required_capabilities": ["扩展系统调用"],
        },
        report={
            **_report(),
            "difficulty_profile": {
                **_report()["difficulty_profile"],
                "available_tools": ["mcp__alpha__write_item"],
            },
        },
        live_tool_names=[
            "mcp__alpha__write_item",
            "mcp__alpha__delete_item",
            "mcp__beta__send_message",
        ],
    )

    assert aligned["available_tools"] == ["mcp__alpha__write_item"]


def test_exact_writer_tools_exclude_broad_capability_fallbacks() -> None:
    aligned = align_capability_match(
        {
            "available_tools": [
                "read_file｜read a file",
                "glob｜broad keyword fallback",
            ],
            "missing_tools": ["浏览器交互 缺少可确认的已发现工具"],
            "missing_tool_requirements": [
                {
                    "missing_tool": "浏览器交互 缺少可确认的已发现工具",
                }
            ],
            "required_capabilities": ["文件与代码检索", "浏览器交互"],
        },
        report={
            **_report(),
            "difficulty_profile": {
                **_report()["difficulty_profile"],
                "available_tools": ["read_file"],
            },
        },
        live_tool_names=["read_file", "glob"],
    )

    assert aligned["available_tools"] == ["read_file"]
    assert aligned["missing_tools"] == []


def test_legacy_writer_report_gets_non_blocking_execution_contract() -> None:
    contract = build_execution_contract(
        _report(),
        live_tool_schemas=[
            {"name": "read_file"},
            {"name": "write_file"},
        ],
    )

    assert contract["source"] == "writer_legacy_fallback"
    assert [item["tool_name"] for item in contract["milestones"]] == [
        "read_file",
        "write_file",
    ]
    assert all("max_calls" not in item for item in contract["milestones"])


@pytest.mark.asyncio
async def test_structured_writer_milestones_survive_actor_handoff() -> None:
    report = _report()
    report["execution_plan"]["milestones"] = [
        {
            "id": "read-input",
            "goal": "Read the input",
            "tool_name": "read_file",
            "required": True,
            "min_calls": 1,
            "postcondition": "source content is available",
        },
        {
            "id": "write-output",
            "goal": "Write the result",
            "tool_name": "write_file",
            "required": True,
            "depends_on": ["read-input", "missing-milestone"],
            "max_calls": 1,
            "parameter_bindings": {
                "path": {
                    "from_milestone": "read-input",
                    "output_path": "output_path",
                }
            },
        },
    ]
    client = SequencedWriterClient([report, _evaluation(91)])

    result = await generate_writer_handoff(
        api_client=client,
        model="offline-writer",
        actor_model="offline-actor",
        workspace_root=_workspace_root(),
        query="Read and write a result.",
        live_tool_schemas=[
            {
                "name": "read_file",
                "input_schema": {
                    "type": "object",
                    "properties": {"path": {"type": "string"}},
                    "required": ["path"],
                },
            },
            {
                "name": "write_file",
                "input_schema": {
                    "type": "object",
                    "properties": {
                        "path": {"type": "string"},
                        "content": {"type": "string"},
                    },
                    "required": ["path", "content"],
                },
            },
        ],
    )

    assert result.execution_contract["source"] == "writer_milestones"
    assert [item["id"] for item in result.execution_contract["milestones"]] == [
        "read-input",
        "write-output",
    ]
    assert result.actor_contract["execution_plan"]["milestones"][1]["max_calls"] == 1
    assert result.execution_contract["milestones"][1]["min_calls"] == 1
    assert result.execution_contract["milestones"][1]["depends_on"] == ["read-input"]
    assert result.execution_contract["milestones"][1]["required_parameters"] == [
        "path",
        "content",
    ]
    assert '"milestones"' in client.requests[1].messages[0].text
    assert result.to_dict(include_raw_response=False)["execution_contract"][
        "source"
    ] == "writer_milestones"
    appendix = result.prompt_appendix()
    assert '"execution_contract"' in appendix
    assert "mandatory milestone" in appendix


@pytest.mark.asyncio
async def test_team_writer_v1_scores_complete_script_and_records_events() -> None:
    workspace = _workspace_root()
    client = SequencedWriterClient([_report(), _evaluation(91)])
    schemas = [
        {"name": "read_file", "description": "Read a file"},
        {"name": "write_file", "description": "Write a file"},
    ]

    result = await generate_writer_handoff(
        api_client=client,
        model="offline-writer",
        actor_model="offline-actor",
        workspace_root=workspace,
        query="Read in/input.txt, count its lines, and write out/linecount.txt.",
        live_tool_schemas=schemas,
    )

    assert len(client.requests) == 2
    assert all(request.tools == [] for request in client.requests)
    assert client.requests[0].model == "offline-actor"
    assert client.requests[1].model == "offline-writer"
    assert client.requests[1].extra_body == {
        "thinking": {"type": "disabled"}
    }
    assert result.tools_exposed_to_writer == 0
    assert result.writer_mode == "team_writer_harness_v1"
    assert result.writer_source_version == "group_writer_v1"
    assert result.model_request_count == 2
    assert result.regeneration_performed is False
    assert result.judge_overall_score == 91
    assert result.execution_decision == {
        "should_execute": True,
        "score_band": "high",
        "reason": "Writer score is above 85; execute the approved final_scripts.",
    }
    assert result.usage == {"input_tokens": 20, "output_tokens": 10}
    assert result.final_report["difficulty_profile"]["available_tools"] == (
        result.core_capability_match["available_tools"]
    )
    assert result.aligned_capability_match["available_tools"] == [
        "read_file",
        "write_file",
    ]
    recorder = WriterEventRecorder(result)
    recorder.action_proposed("read_file", {"path": "in/input.txt"})
    recorder.action_completed("read_file", is_error=False)
    validation = recorder.validation()
    assert validation["writer_mandatory_passed"] is True
    assert validation["events_complete"] is True
    assert validation["tool_step_count"] == 1
    appendix = result.prompt_appendix()
    assert '"writer_source_version": "group_writer_v1"' in appendix
    assert '"writer_overall_score": 91' in appendix
    assert '"final_scripts"' in appendix


@pytest.mark.asyncio
async def test_team_writer_v1_incomplete_script_triggers_one_actor_regeneration() -> None:
    incomplete = _report()
    incomplete["execution_plan"] = {
        "pre_execution_thoughts": [],
        "recommended_steps": [],
        "validation_steps": [],
    }
    repaired = _report()
    client = SequencedWriterClient(
        [
            incomplete,
            _evaluation(55),
            repaired,
            _evaluation(82),
        ]
    )

    result = await generate_writer_handoff(
        api_client=client,
        model="offline-writer",
        actor_model="offline-actor",
        workspace_root=_workspace_root(),
        query="Read a local file and write its line count.",
        live_tool_schemas=[
            {"name": "read_file", "description": "Read a file"},
            {"name": "write_file", "description": "Write a file"},
        ],
    )

    assert len(client.requests) == 4
    assert all(request.tools == [] for request in client.requests)
    assert [request.model for request in client.requests] == [
        "offline-actor",
        "offline-writer",
        "offline-actor",
        "offline-writer",
    ]
    assert "Revise the existing execution script" in client.requests[2].messages[0].text
    assert result.regeneration_performed is True
    assert result.initial_actor_harness_output == json.dumps(
        incomplete,
        ensure_ascii=False,
    )
    assert result.regeneration_raw_response == json.dumps(
        repaired,
        ensure_ascii=False,
    )
    assert result.model_request_count == 4
    assert result.core_online_completeness["round_index"] == 2
    assert result.judge_overall_score == 82
    assert result.execution_decision["score_band"] == "medium"


@pytest.mark.asyncio
async def test_team_writer_v1_keeps_group_low_score_cautious_execution() -> None:
    client = SequencedWriterClient([_report(), _evaluation(62)])

    result = await generate_writer_handoff(
        api_client=client,
        model="offline-writer",
        workspace_root=_workspace_root(),
        query="Read a file and write a count.",
        live_tool_schemas=[
            {"name": "read_file", "description": "Read a file"},
            {"name": "write_file", "description": "Write a file"},
        ],
    )

    assert len(client.requests) == 2
    assert result.model_request_count == 2
    assert result.judge_overall_score == 62
    assert result.execution_decision["should_execute"] is True
    assert result.execution_decision["score_band"] == "low"
    assert "low-score cautious execution policy" in result.execution_decision["reason"]
