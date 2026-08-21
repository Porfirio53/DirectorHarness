"""Director Harness 的基础单元测试。

这些测试使用最小注册表替身，不启动真实 MCP、OpenHarness CLI 或 LLM，重点
验证会话级缓存和无备案替代项时的安全阻断行为。"""

from __future__ import annotations

import asyncio
from pathlib import Path

from director_harness import DirectorHarness, DirectorRequest


class _Tool:
    """模拟只读 OpenHarness 工具，仅覆盖 Director 使用的方法。"""

    def __init__(self, read_only: bool = True) -> None:
        self.read_only = read_only

    def is_read_only(self, parsed_input):
        """返回只读标记，模拟 OpenHarness 的 ``BaseTool.is_read_only``。"""
        del parsed_input
        return self.read_only


class _Registry:
    """只包含 ``read_file`` 的极简工具注册表替身。"""

    def __init__(self) -> None:
        self.tools = {
            "read_file": _Tool(read_only=True),
            "write_file": _Tool(read_only=False),
        }

    def get(self, name: str):
        """按工具名返回模拟工具；其他名称表示当前未注册。"""
        return self.tools.get(name)


def test_registered_tool_is_checked_once() -> None:
    """首次检查登记通过缓存，第二次同工具调用应直接跳过预检。"""
    director = DirectorHarness()
    request = DirectorRequest(
        tool_name="read_file",
        tool_input={"path": "example.txt"},
        parsed_input=object(),
        cwd=Path.cwd(),
        tool_registry=_Registry(),
        tool_metadata={"session_id": "test"},
        tool_use_id="toolu_registered",
    )

    first = asyncio.run(director.preflight(request))
    second = asyncio.run(director.preflight(request))

    assert first.action == "allow"
    assert second.action == "allow"
    assert [event.status for event in director.events] == ["passed", "skipped"]
    assert [event.tool_use_id for event in director.events] == ["toolu_registered", "toolu_registered"]


def test_missing_tool_is_blocked_without_approved_mcp() -> None:
    """没有人工备案 MCP 时，缺失工具必须被明确阻断。"""
    director = DirectorHarness()
    request = DirectorRequest(
        tool_name="search_reference",
        tool_input={},
        parsed_input=None,
        cwd=Path.cwd(),
        tool_registry=_Registry(),
        tool_metadata=None,
        missing_tool=True,
        tool_use_id="toolu_missing",
    )

    decision = asyncio.run(director.preflight(request))

    assert decision.action == "deny"
    assert [event.event for event in director.events] == ["tool_check", "mcp_search"]
    assert [event.tool_use_id for event in director.events] == ["toolu_missing", "toolu_missing"]


def _contract_metadata(*milestones: dict) -> dict[str, object]:
    return {
        "session_id": "contract-test",
        "writer_execution_contract": {
            "schema_version": 1,
            "source": "writer_milestones",
            "milestones": list(milestones),
        },
    }


def _request(
    tool_name: str,
    *,
    metadata: dict[str, object],
    tool_use_id: str,
    tool_input: dict[str, object] | None = None,
) -> DirectorRequest:
    return DirectorRequest(
        tool_name=tool_name,
        tool_input=tool_input or {},
        parsed_input=object(),
        cwd=Path.cwd(),
        tool_registry=_Registry(),
        tool_metadata=metadata,
        tool_use_id=tool_use_id,
    )


def test_explicit_write_dependency_is_checked_before_execution() -> None:
    metadata = _contract_metadata(
        {
            "id": "read-source",
            "tool_name": "read_file",
            "required": True,
            "min_calls": 1,
        },
        {
            "id": "write-output",
            "tool_name": "write_file",
            "required": True,
            "depends_on": ["read-source"],
            "min_calls": 1,
            "max_calls": 1,
        },
    )
    director = DirectorHarness()

    blocked = asyncio.run(
        director.preflight(
            _request(
                "write_file",
                metadata=metadata,
                tool_use_id="write-too-soon",
            )
        )
    )
    read = asyncio.run(
        director.preflight(
            _request("read_file", metadata=metadata, tool_use_id="read-source")
        )
    )
    director.observe_result(
        "read_file",
        False,
        metadata,
        "read-source",
        '{"next_cursor": "cursor-2"}',
    )
    write = asyncio.run(
        director.preflight(
            _request("write_file", metadata=metadata, tool_use_id="write-output")
        )
    )

    assert blocked.action == "deny"
    assert read.action == "allow"
    assert write.action == "allow"
    assert metadata["writer_plan_state"]["completed"] == ["read-source"]
    assert any(
        event.event == "plan_check"
        and event.status == "blocked"
        and event.tool_use_id == "write-too-soon"
        for event in director.events
    )


def test_safe_read_is_allowed_when_explicit_dependency_is_unmet() -> None:
    metadata = _contract_metadata(
        {
            "id": "prepare",
            "tool_name": "write_file",
            "min_calls": 1,
        },
        {
            "id": "verify",
            "tool_name": "read_file",
            "depends_on": ["prepare"],
            "min_calls": 1,
            "max_calls": 1,
        },
    )
    director = DirectorHarness()

    decision = asyncio.run(
        director.preflight(
            _request("read_file", metadata=metadata, tool_use_id="safe-read")
        )
    )

    assert decision.action == "allow"
    assert any(
        event.event == "plan_check"
        and event.status == "skipped"
        and event.tool_use_id == "safe-read"
        for event in director.events
    )


def test_write_call_limit_reserves_in_flight_call_and_allows_one_retry() -> None:
    metadata = _contract_metadata(
        {
            "id": "write-once",
            "tool_name": "write_file",
            "min_calls": 1,
            "max_calls": 1,
            "max_retries": 1,
        }
    )
    director = DirectorHarness()

    first = asyncio.run(
        director.preflight(
            _request("write_file", metadata=metadata, tool_use_id="write-1")
        )
    )
    concurrent = asyncio.run(
        director.preflight(
            _request("write_file", metadata=metadata, tool_use_id="write-concurrent")
        )
    )
    director.observe_result("write_file", True, metadata, "write-1", "failed")
    retry = asyncio.run(
        director.preflight(
            _request("write_file", metadata=metadata, tool_use_id="write-retry")
        )
    )
    director.observe_result("write_file", False, metadata, "write-retry", "ok")
    duplicate = asyncio.run(
        director.preflight(
            _request("write_file", metadata=metadata, tool_use_id="write-duplicate")
        )
    )

    assert first.action == "allow"
    assert concurrent.action == "deny"
    assert retry.action == "allow"
    assert duplicate.action == "deny"


def test_parameter_binding_uses_successful_prior_json_result() -> None:
    metadata = _contract_metadata(
        {
            "id": "list-page",
            "tool_name": "read_file",
            "min_calls": 1,
        },
        {
            "id": "write-page",
            "tool_name": "write_file",
            "depends_on": ["list-page"],
            "parameter_bindings": {
                "cursor": {
                    "from_milestone": "list-page",
                    "output_path": "next_cursor",
                }
            },
        },
    )
    director = DirectorHarness()
    asyncio.run(
        director.preflight(
            _request("read_file", metadata=metadata, tool_use_id="list-page")
        )
    )
    director.observe_result(
        "read_file",
        False,
        metadata,
        "list-page",
        '{"next_cursor": "cursor-2"}',
    )

    decision = asyncio.run(
        director.preflight(
            _request(
                "write_file",
                metadata=metadata,
                tool_use_id="write-page",
                tool_input={"path": "out.txt"},
            )
        )
    )
    revalidated = asyncio.run(
        director.preflight(
            _request(
                "write_file",
                metadata=metadata,
                tool_use_id="write-page",
                tool_input=decision.tool_input,
            )
        )
    )

    assert decision.action == "replace"
    assert revalidated.action == "allow"
    assert decision.tool_name == "write_file"
    assert decision.tool_input == {"path": "out.txt", "cursor": "cursor-2"}
    assert metadata["writer_plan_state"]["attempts"]["write-page"] == 1


def test_plan_progress_is_restored_from_session_metadata() -> None:
    metadata = _contract_metadata(
        {
            "id": "read-source",
            "tool_name": "read_file",
            "min_calls": 1,
        },
        {
            "id": "write-output",
            "tool_name": "write_file",
            "depends_on": ["read-source"],
            "min_calls": 1,
        },
    )
    metadata["writer_plan_state"] = {
        "source": "writer_milestones",
        "attempts": {"read-source": 1},
        "successes": {"read-source": 1},
        "errors": {},
        "completed": ["read-source"],
    }

    decision = asyncio.run(
        DirectorHarness().preflight(
            _request(
                "write_file",
                metadata=metadata,
                tool_use_id="resumed-write",
                tool_input={"path": "out.txt", "content": "done"},
            )
        )
    )

    assert decision.action == "allow"
    assert metadata["writer_plan_state"]["completed"] == ["read-source"]
