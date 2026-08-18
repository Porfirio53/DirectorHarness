"""Director Harness 的稳定数据契约。

本模块只定义 MCP 候选项和运行事件，避免业务逻辑与 UI、日志或
OpenHarness 的具体执行实现耦合。"""

from __future__ import annotations

from dataclasses import dataclass, field
from time import time
from typing import Any, Literal


@dataclass(frozen=True)
class McpCandidate:
    """人工审核后允许 Director 在当前会话接入的 MCP 候选项。

    参数：
    - name：MCP 服务名，同时用于生成 OpenHarness 的 MCP 工具前缀。
    - capabilities：服务提供能力的关键词，参与候选匹配。
    - config：符合 OpenHarness ``mcpServers`` 的单服务配置。
    - tool_aliases：计划工具名与 MCP 实际工具名之间的映射线索。
    """

    name: str
    capabilities: tuple[str, ...]
    config: dict[str, Any]
    tool_aliases: tuple[str, ...] = ()


@dataclass(frozen=True)
class DirectorEvent:
    """一次 Director 判断、配置或修复动作的可展示事件。

    ``status`` 用于前端胶囊和详情浮窗状态；``data`` 只承载可公开的
    辅助数据，写入日志前会再次进行敏感字段脱敏。
    """
    event: str
    tool_name: str
    status: Literal["passed", "skipped", "failed", "repaired", "blocked"]
    detail: str
    session_id: str = ""
    data: dict[str, Any] = field(default_factory=dict)
    tool_use_id: str = ""
    timestamp: float = field(default_factory=time)
