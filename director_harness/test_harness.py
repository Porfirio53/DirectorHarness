"""Director Harness 的基础单元测试。

这些测试使用最小注册表替身，不启动真实 MCP、OpenHarness CLI 或 LLM，重点
验证会话级缓存和无备案替代项时的安全阻断行为。"""

from __future__ import annotations

import asyncio
from pathlib import Path

from director_harness import DirectorHarness, DirectorRequest


class _Tool:
    """模拟只读 OpenHarness 工具，仅覆盖 Director 使用的方法。"""

    def is_read_only(self, parsed_input):
        """返回只读标记，模拟 OpenHarness 的 ``BaseTool.is_read_only``。"""
        del parsed_input
        return True


class _Registry:
    """只包含 ``read_file`` 的极简工具注册表替身。"""

    def __init__(self) -> None:
        self.tool = _Tool()

    def get(self, name: str):
        """按工具名返回模拟工具；其他名称表示当前未注册。"""
        return self.tool if name == "read_file" else None


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
