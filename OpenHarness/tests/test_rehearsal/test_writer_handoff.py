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


class StaticWriterClient:
    def __init__(self, report: dict[str, object]) -> None:
        self.report = report
        self.requests: list[ApiMessageRequest] = []

    async def stream_message(
        self,
        request: ApiMessageRequest,
    ) -> AsyncIterator[ApiStreamEvent]:
        self.requests.append(request)
        yield ApiMessageCompleteEvent(
            message=ConversationMessage(
                role="assistant",
                content=[TextBlock(text=json.dumps(self.report, ensure_ascii=False))],
            ),
            usage=UsageSnapshot(input_tokens=100, output_tokens=50),
            stop_reason="stop",
        )


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
            "available_tools": ["Read", "apply_patch"],
            "missing_tools": [],
            "known_conditions": ["local workspace"],
            "unknown_conditions": ["file contents"],
            "estimated_cost": "two tool calls",
        },
        "execution_plan": {
            "pre_execution_thoughts": ["preserve input"],
            "recommended_steps": ["read input", "write output"],
            "validation_steps": ["verify output"],
        },
        "difficulty_judgment": "low risk",
        "judgment_rationale": ["local deterministic task"],
        "execution_suggestion": "execute",
    }


def _v3_report() -> dict[str, object]:
    return {
        "report_version": 3,
        "task_profile": {
            "task_type": "offline research",
            "task_goal": "Create research_brief.md from local evidence.",
            "success_criteria": ["research_brief.md is grounded in local evidence."],
            "expected_output": "research_brief.md",
            "explicit_requirements": [
                {
                    "id": "R1",
                    "requirement": "Create research_brief.md from local evidence.",
                    "source_text": "Create research_brief.md from local evidence.",
                    "kind": "deliverable",
                    "operator": "satisfy",
                }
            ],
        },
        "difficulty_profile": {
            "difficulty": "low",
            "available_tools": ["read_file"],
            "missing_tools": [],
            "known_conditions": ["Local evidence is available."],
            "environment_facts": ["read_file is available."],
            "runtime_discoveries": [],
            "capability_requirements": [],
            "inferred_conditions": [],
            "unknown_conditions": [],
            "estimated_cost": "small",
        },
        "execution_plan": {
            "planning_depth": "minimal",
            "pre_execution_thoughts": [],
            "recommended_steps": [
                {
                    "id": "S1",
                    "action": "Read local evidence and create research_brief.md.",
                    "covers": ["R1"],
                    "tools": ["read_file"],
                    "depends_on": [],
                    "completion_evidence": "research_brief.md exists.",
                    "strength": "required",
                    "confidence": "high",
                }
            ],
            "validation_steps": [
                {
                    "id": "V1",
                    "check": "Verify research_brief.md is grounded in local evidence.",
                    "covers": ["R1"],
                    "check_type": "content",
                    "evidence": "research_brief.md",
                    "expected": "Every material claim maps to local evidence.",
                }
            ],
            "replan_triggers": [],
            "final_response_requirements": [],
        },
        "difficulty_judgment": "low",
        "judgment_rationale": [],
        "execution_suggestion": "execute",
    }


def test_deployed_writer_provenance_is_recorded_without_source_lock() -> None:
    workspace = _workspace_root()
    result = verify_writer_deployment(
        workspace,
        workspace / "writer_harness_demo.zip",
        include_support_files=False,
    )

    assert result.checked_files == len(WRITER_CORE_FILES)
    assert result.missing_files == ()
    assert set(result.mismatched_files).issubset(set(WRITER_CORE_FILES))
    assert set(result.file_sha256).issubset(set(WRITER_CORE_FILES))
    assert result.ok is (not result.missing_files)
    assert result.archive_match in {True, False, None}

    serialized = result.to_dict()
    assert serialized["missing_files"] == []
    assert serialized["mismatched_files"] == list(result.mismatched_files)
    assert json.loads(json.dumps(serialized)) == serialized


def test_external_core_loads_from_workspace_without_copying() -> None:
    workspace = _workspace_root()
    bindings = load_writer_core(workspace)

    assert Path(bindings.package_file) == workspace / "writer_harness/__init__.py"
    prompt = bindings.get_generated_scripts_template("en")
    assert "must not execute the task or call tools" in prompt


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
    assert "candidate tools:" not in request


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
        "openharness_adapter_alias"
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


@pytest.mark.asyncio
async def test_report_generation_exposes_no_tools_and_records_complete_events() -> None:
    workspace = _workspace_root()
    client = StaticWriterClient(_report())
    schemas = [
        {"name": "read_file", "description": "Read a file"},
        {"name": "write_file", "description": "Write a file"},
    ]

    result = await generate_writer_handoff(
        api_client=client,
        model="offline-test",
        workspace_root=workspace,
        query="Read in/input.txt, count its lines, and write out/linecount.txt.",
        live_tool_schemas=schemas,
    )

    assert len(client.requests) == 1
    assert client.requests[0].tools == []
    assert result.tools_exposed_to_writer == 0
    assert result.usage == {"input_tokens": 100, "output_tokens": 50}
    assert result.final_report["difficulty_profile"]["available_tools"] == [
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


@pytest.mark.asyncio
async def test_v3_review_uses_only_selected_full_schemas_and_skips_repair() -> None:
    report = _v3_report()
    client = SequencedWriterClient(
        [
            report,
            {"passed": True, "defects": [], "summary": "Draft is ready."},
        ]
    )
    schemas = [
        {
            "name": "read_file",
            "description": "Read one local file.",
            "input_schema": {
                "type": "object",
                "properties": {"path": {"type": "string"}},
                "required": ["path"],
            },
        },
        {
            "name": "delete_file",
            "description": "Delete one local file.",
            "input_schema": {
                "type": "object",
                "properties": {"unselected_full_schema_marker": {"type": "string"}},
            },
        },
    ]

    result = await generate_writer_handoff(
        api_client=client,
        model="qwen3.6-plus",
        workspace_root=_workspace_root(),
        query="Create research_brief.md from local evidence.",
        live_tool_schemas=schemas,
    )

    assert len(client.requests) == 2
    assert all(request.tools == [] for request in client.requests)
    assert client.requests[0].extra_body == {"enable_thinking": False}
    assert client.requests[1].extra_body == {
        "enable_thinking": False,
        "response_format": {"type": "json_object"},
    }
    assert client.requests[1].max_tokens == 2048
    review_payload = json.loads(client.requests[1].messages[0].text)
    assert [schema["name"] for schema in review_payload["selected_tool_schemas"]] == ["read_file"]
    assert "unselected_full_schema_marker" not in client.requests[1].messages[0].text
    assert {tool["name"] for tool in review_payload["live_tool_catalog"]} == {
        "read_file",
        "delete_file",
    }
    assert result.writer_mode == "external_writer_harness_v3"
    assert result.model_request_count == 2
    assert result.semantic_review["revision_performed"] is False
    assert result.usage == {"input_tokens": 20, "output_tokens": 10}


@pytest.mark.asyncio
async def test_v3_defect_triggers_exactly_one_repair_and_reaudit() -> None:
    draft = _v3_report()
    requirement = draft["task_profile"]["explicit_requirements"][0]  # type: ignore[index]
    requirement.update(  # type: ignore[union-attr]
        {
            "requirement": "Create at least 3 research briefs.",
            "source_text": "Create at least 3 research briefs.",
            "kind": "quantity",
            "operator": "at_least",
            "value": "3",
        }
    )
    draft_step = draft["execution_plan"]["recommended_steps"][0]  # type: ignore[index]
    draft_step["action"] = "Create 3 research briefs."  # type: ignore[index]
    draft_step["completion_evidence"] = "3 briefs exist."  # type: ignore[index]
    draft_validation = draft["execution_plan"]["validation_steps"][0]  # type: ignore[index]
    draft_validation.update(  # type: ignore[union-attr]
        {
            "check": "Count the research briefs.",
            "check_type": "count_constraint",
            "expected": "Count >= 3",
        }
    )
    client = SequencedWriterClient(
        [
            draft,
            {
                "passed": False,
                "defects": [
                    {
                        "code": "quantity_bound_anchoring",
                        "message": "S1 turns the lower bound into an exact target.",
                        "repair": "Keep at-least semantics in S1.",
                        "requirement_ids": ["R1"],
                        "step_ids": ["S1"],
                    }
                ],
                "patch_operations": [
                    {
                        "op": "replace",
                        "path": "/execution_plan/recommended_steps/S1/action",
                        "value": "Create at least 3 research briefs.",
                    },
                    {
                        "op": "replace",
                        "path": "/execution_plan/recommended_steps/S1/completion_evidence",
                        "value": "At least 3 briefs exist.",
                    },
                ],
                "summary": "One local repair is needed.",
            },
        ]
    )

    result = await generate_writer_handoff(
        api_client=client,
        model="offline-test",
        workspace_root=_workspace_root(),
        query="Create at least 3 research briefs.",
        live_tool_schemas=[
            {
                "name": "read_file",
                "description": "Read one local file.",
                "input_schema": {"type": "object"},
            }
        ],
    )

    assert len(client.requests) == 2
    assert all(request.tools == [] for request in client.requests)
    assert result.model_request_count == 2
    assert result.semantic_review["revision_performed"] is True
    assert result.semantic_review["revision_accepted"] is True
    assert result.semantic_review["revision_mode"] == "review_patch_local"
    assert result.semantic_review["final_contract_audit"]["quantity_bound_anchoring"] == []
    assert result.revision_raw_response is None
    assert result.final_report["execution_plan"]["recommended_steps"][0]["action"].startswith(
        "Create at least 3"
    )
    appendix = result.prompt_appendix()
    assert "judgment_rationale" not in appendix
    assert "semantic_review" not in appendix
