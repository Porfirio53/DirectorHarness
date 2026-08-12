from __future__ import annotations

import argparse
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
from openharness.engine.messages import (
    ConversationMessage,
    TextBlock,
    ToolUseBlock,
)
from openharness.rehearsal.mcp_persona_runtime import task_by_id
from scripts.run_mcp_persona_week1 import _run_trial


class WriterThenMcpActorClient:
    def __init__(self) -> None:
        self.requests: list[ApiMessageRequest] = []

    async def stream_message(
        self,
        request: ApiMessageRequest,
    ) -> AsyncIterator[ApiStreamEvent]:
        self.requests.append(request)
        if not request.tools:
            message = ConversationMessage(
                role="assistant",
                content=[
                    TextBlock(
                        text=json.dumps(
                            {
                                "task_profile": {
                                    "task_type": "MCP read",
                                    "task_goal": "list chats",
                                    "success_criteria": ["chat list returned"],
                                    "expected_output": "chat details",
                                },
                                "difficulty_profile": {
                                    "difficulty": "low",
                                    "available_tools": ["run_mcp"],
                                    "missing_tools": [],
                                    "known_conditions": ["user context"],
                                    "unknown_conditions": ["live chat data"],
                                    "estimated_cost": "one call",
                                },
                                "execution_plan": {
                                    "pre_execution_thoughts": [
                                        "preserve state"
                                    ],
                                    "recommended_steps": [
                                        "call im_v1_chat_list"
                                    ],
                                    "validation_steps": [
                                        "verify returned chat"
                                    ],
                                },
                                "difficulty_judgment": "read-only",
                                "judgment_rationale": ["single read"],
                                "execution_suggestion": "execute",
                            }
                        )
                    )
                ],
            )
        elif len(self.requests) == 2:
            tool_name = next(
                str(value["name"])
                for value in request.tools
                if "im_v1_chat_list" in str(value.get("name"))
            )
            message = ConversationMessage(
                role="assistant",
                content=[
                    ToolUseBlock(
                        id="writer-smoke-call",
                        name=tool_name,
                        input={
                            "params": {
                                "user_id_type": "open_id",
                                "sort_type": "ByActiveTimeDesc",
                                "page_size": 20,
                            },
                            "useUAT": True,
                        },
                    )
                ],
            )
        else:
            message = ConversationMessage(
                role="assistant",
                content=[TextBlock(text="Listed the available chats.")],
            )
        yield ApiMessageCompleteEvent(
            message=message,
            usage=UsageSnapshot(input_tokens=3, output_tokens=2),
            stop_reason="stop",
        )


@pytest.mark.asyncio
async def test_mcp_writer_handoff_precedes_tools_and_preserves_reset(
    tmp_path: Path,
) -> None:
    openharness_root = Path(__file__).resolve().parents[2]
    workspace_root = openharness_root.parent
    mcp_root = workspace_root / "MCP-Persona"
    if not mcp_root.is_dir():
        pytest.skip("sibling MCP-Persona checkout is unavailable")
    args = argparse.Namespace(
        language="en",
        tool_scope="task",
        chain_guidance=False,
        max_tokens=1024,
        max_turns=4,
        trial_timeout=30.0,
        model="offline-actor",
        openharness_mode="writer_harness",
        writer_model="offline-writer",
        writer_workspace_root=workspace_root,
        writer_max_tokens=1024,
    )
    client = WriterThenMcpActorClient()

    result = await _run_trial(
        task=task_by_id(mcp_root, 4, "en"),
        trial=1,
        args=args,
        root=mcp_root,
        openharness_root=openharness_root,
        api_client=client,  # type: ignore[arg-type]
        task_dir=tmp_path / "task-4",
    )

    assert len(client.requests) == 3
    assert client.requests[0].tools == []
    assert client.requests[1].tools
    assert result["writer"]["mandatory_passed"] is True
    assert result["writer"]["planning_state_unchanged"] is True
    assert result["writer"]["event_validation"]["events_complete"] is True
    assert result["state"]["exact_reset"] is True
    event_types = [value["type"] for value in result["events"]]
    assert event_types.index("global_plan_created") < event_types.index(
        "tool_started"
    )
