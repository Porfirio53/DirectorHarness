"""Director Harness 的执行前保障逻辑。

Director 不生成任务计划，也不修改 Writer Harness 的业务结论。它仅在
OpenHarness 即将执行工具前确认该工具可用；缺失时从人工备案目录中接入
候选 MCP，并记录可展示的事件。"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

from .catalog import McpCatalog
from .events import DirectorEventLog
from .models import DirectorEvent


@dataclass(frozen=True)
class DirectorRequest:
    """OpenHarness 在工具实际执行前交给 Director 的上下文。

    ``tool_name`` 和 ``tool_input`` 为模型请求的原始调用；``parsed_input``
    是已通过 OpenHarness 参数模型校验的对象。``tool_metadata`` 中可取得
    会话 ID 与 ``mcp_manager`` 等运行时依赖。
    """

    tool_name: str
    tool_input: dict[str, object]
    parsed_input: object | None
    cwd: Path
    tool_registry: object
    tool_metadata: dict[str, object] | None
    missing_tool: bool = False
    tool_use_id: str = ""


@dataclass(frozen=True)
class DirectorDecision:
    """Director 对单次工具调用的决策。

    - ``allow``：保留原工具与参数。
    - ``deny``：阻断本次调用，并让模型收到 ``reason``。
    - ``replace``：使用替代 MCP 工具名和可选替代参数继续执行。
    """

    action: Literal["allow", "deny", "replace"]
    reason: str = ""
    tool_name: str | None = None
    tool_input: dict[str, object] | None = None


class DirectorHarness:
    """会话级工具保障器。

    参数：
    - catalog：人工审核 MCP 目录；空目录时只执行现有工具检查。
    - event_log：内存与可选 JSONL 事件记录器。

    ``_passed_tools`` 仅在当前进程/会话有效。工具真实执行失败后会删除对应
    缓存，下一次调用将重新检查。
    """

    def __init__(self, catalog: McpCatalog | None = None, event_log: DirectorEventLog | None = None) -> None:
        self._catalog = catalog or McpCatalog()
        self._event_log = event_log or DirectorEventLog()
        self._passed_tools: set[str] = set()

    @property
    def events(self) -> list[DirectorEvent]:
        """返回当前会话已记录的事件副本，供 UI 或上层流程读取。"""
        return list(self._event_log.events)

    def events_for_tool_use(self, tool_use_id: str) -> list[DirectorEvent]:
        """返回关联到指定 OpenHarness 工具调用的 Director 事件副本。"""
        return [event for event in self._event_log.events if event.tool_use_id == tool_use_id]

    async def preflight(self, request: DirectorRequest) -> DirectorDecision:
        """检查一次即将执行的工具调用，并决定放行、阻断或替换。

        已通过缓存的工具会直接放行；已注册工具的首次真实调用承担最小可用性
        验证，避免为检查而重复执行可能有副作用的工具。未注册工具才进入 MCP
        检索、标准配置、连接和工具注册流程。
        """
        session_id = _session_id(request.tool_metadata)
        if not request.missing_tool and request.tool_name in self._passed_tools:
            self._emit("tool_check", request.tool_name, "skipped", "同类工具已通过本会话校验", session_id, tool_use_id=request.tool_use_id)
            return DirectorDecision("allow")
        tool = request.tool_registry.get(request.tool_name)
        if tool is not None:
            self._passed_tools.add(request.tool_name)
            self._emit(
                "tool_check",
                request.tool_name,
                "passed",
                "工具已注册且当前调用参数已通过 OpenHarness 校验；本次真实调用将作为可用性验证",
                session_id,
                {"read_only": bool(tool.is_read_only(request.parsed_input)) if request.parsed_input is not None else False},
                request.tool_use_id,
            )
            return DirectorDecision("allow")
        self._emit("tool_check", request.tool_name, "failed", "工具未注册，开始检索已备案 MCP", session_id, tool_use_id=request.tool_use_id)
        replacement = await self._configure_candidate(request, session_id)
        if replacement:
            return DirectorDecision("replace", "已配置并连接替代 MCP", replacement, request.tool_input)
        return DirectorDecision("deny", f"Director 未找到可用工具或已备案 MCP：{request.tool_name}")

    def observe_result(self, tool_name: str, is_error: bool, tool_metadata: dict[str, object] | None = None, tool_use_id: str = "") -> None:
        """接收真实调用结果，并在报错时使该工具的通过缓存失效。"""
        if not is_error:
            return
        self._passed_tools.discard(tool_name)
        self._emit("tool_result", tool_name, "failed", "实际调用报错，已取消该工具的会话校验缓存", _session_id(tool_metadata), tool_use_id=tool_use_id)

    async def preflight_plan(self, tool_names: list[str], request: DirectorRequest) -> dict[str, DirectorDecision]:
        """按去重后的工具名预检计划，供计划已明确时的批量调用使用。

        当前 OpenHarness 主链路调用 :meth:`preflight`；该方法保留给后续从
        Writer Harness 结构化计划中提前获取工具名的场景。
        """
        decisions: dict[str, DirectorDecision] = {}
        for tool_name in dict.fromkeys(tool_names):
            decisions[tool_name] = await self.preflight(
                DirectorRequest(
                    tool_name=tool_name,
                    tool_input={},
                    parsed_input=None,
                    cwd=request.cwd,
                    tool_registry=request.tool_registry,
                    tool_metadata=request.tool_metadata,
                    missing_tool=request.tool_registry.get(tool_name) is None,
                    tool_use_id=request.tool_use_id,
                )
            )
        return decisions

    async def _configure_candidate(self, request: DirectorRequest, session_id: str) -> str | None:
        """接入匹配的备案 MCP，并返回注册后的替代工具名。

        使用 OpenHarness 的 ``McpJsonConfig``、``McpClientManager`` 与
        ``McpToolAdapter`` 完成内存配置、连接和工具注册，不写入持久化配置。
        MCP 连接及 ``tools/list`` 成功即作为无副作用健康检查。
        """
        candidate = self._catalog.find_best(request.tool_name)
        if candidate is None:
            self._emit("mcp_search", request.tool_name, "failed", "备案 MCP 目录中没有匹配候选项", session_id, tool_use_id=request.tool_use_id)
            return None
        self._emit("mcp_search", request.tool_name, "passed", f"选中 MCP：{candidate.name}", session_id, tool_use_id=request.tool_use_id)
        manager = (request.tool_metadata or {}).get("mcp_manager")
        if manager is None:
            self._emit("mcp_configure", request.tool_name, "blocked", "当前 OpenHarness 运行时未提供 MCP 管理器", session_id, tool_use_id=request.tool_use_id)
            return None
        try:
            from openharness.mcp.types import McpJsonConfig
            from openharness.tools.mcp_tool import McpToolAdapter

            config = McpJsonConfig.model_validate({"mcpServers": {candidate.name: candidate.config}}).mcpServers[candidate.name]
            manager.update_server_config(candidate.name, config)
            await manager.reconnect_all()
            for tool_info in manager.list_tools():
                request.tool_registry.register(McpToolAdapter(manager, tool_info))
        except Exception as exc:
            self._emit("mcp_configure", request.tool_name, "failed", f"MCP 配置或连接失败：{exc}", session_id, tool_use_id=request.tool_use_id)
            return None
        resolved_name = _find_registered_tool(request.tool_registry, request.tool_name, candidate.name, candidate.tool_aliases)
        if resolved_name is None:
            self._emit("mcp_health", request.tool_name, "failed", "MCP 已连接，但未暴露可替代的目标工具", session_id, tool_use_id=request.tool_use_id)
            return None
        self._passed_tools.add(resolved_name)
        self._emit("mcp_health", resolved_name, "repaired", "MCP 连接成功，工具已注册并可进入真实调用", session_id, {"server": candidate.name}, request.tool_use_id)
        return resolved_name

    def _emit(self, event: str, tool_name: str, status: Literal["passed", "skipped", "failed", "repaired", "blocked"], detail: str, session_id: str, data: dict[str, Any] | None = None, tool_use_id: str = "") -> None:
        """构造并提交一条统一格式的 Director 事件。"""
        self._event_log.emit(DirectorEvent(event, tool_name, status, detail, session_id, data or {}, tool_use_id))


def create_from_environment() -> DirectorHarness | None:
    """按环境变量创建可选 Director 实例。

    ``DIRECTOR_HARNESS_ENABLED`` 为真值时启用；``DIRECTOR_MCP_CATALOG`` 指向
    人工备案 MCP 目录；``DIRECTOR_LOG_PATH`` 指向可选 JSONL 日志文件。
    未启用时返回 ``None``，调用方无需改动原有 OpenHarness 行为。
    """
    if os.environ.get("DIRECTOR_HARNESS_ENABLED", "").strip().lower() not in {"1", "true", "yes", "on"}:
        return None
    return DirectorHarness(
        catalog=McpCatalog.from_file(os.environ.get("DIRECTOR_MCP_CATALOG")),
        event_log=DirectorEventLog(os.environ.get("DIRECTOR_LOG_PATH")),
    )


def _find_registered_tool(registry: object, requested_name: str, server_name: str, aliases: tuple[str, ...]) -> str | None:
    """从注册表中定位候选 MCP 对应的实际 OpenHarness 工具名。"""
    if registry.get(requested_name) is not None:
        return requested_name
    prefix = f"mcp__{server_name}__"
    candidates = [tool.name for tool in registry.list_tools() if tool.name.startswith(prefix)]
    for alias in aliases:
        normalized = alias.replace("-", "_")
        for name in candidates:
            if name.endswith(f"__{normalized}"):
                return name
    return candidates[0] if len(candidates) == 1 else None


def _session_id(metadata: dict[str, object] | None) -> str:
    """从 OpenHarness 工具元数据中读取会话标识。"""
    return str((metadata or {}).get("session_id") or "")
