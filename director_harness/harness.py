"""Director Harness 的执行前保障逻辑。

Director 不生成任务计划，也不修改 Writer Harness 的业务结论。它在
OpenHarness 即将执行工具前确认工具可用，并在 Writer 提供显式 execution
contract 时检查有限的依赖、调用次数、重试和参数继承；缺失工具仍从人工
备案目录中接入候选 MCP，并记录可展示的事件。"""

from __future__ import annotations

import copy
import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

from .catalog import McpCatalog
from .contract import contract_json
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

    ``_passed_tools`` 和 ``_plan_states`` 均按当前进程/会话隔离。工具真实
    执行失败后会删除对应可用性缓存，下一次调用将重新检查。
    """

    def __init__(self, catalog: McpCatalog | None = None, event_log: DirectorEventLog | None = None) -> None:
        self._catalog = catalog or McpCatalog()
        self._event_log = event_log or DirectorEventLog()
        self._passed_tools: set[str] = set()
        # Populated only when Writer supplies an execution contract.
        self._plan_states: dict[str, dict[str, Any]] = {}

    @property
    def events(self) -> list[DirectorEvent]:
        """返回当前会话已记录的事件副本，供 UI 或上层流程读取。"""
        return list(self._event_log.events)

    def events_for_tool_use(self, tool_use_id: str) -> list[DirectorEvent]:
        """返回关联到指定 OpenHarness 工具调用的 Director 事件副本。"""
        return [event for event in self._event_log.events if event.tool_use_id == tool_use_id]

    def prepare_input(
        self,
        tool_name: str,
        tool_input: dict[str, object],
        tool_metadata: dict[str, object] | None = None,
        tool_use_id: str = "",
    ) -> dict[str, object]:
        """Fill explicit Writer bindings before OpenHarness validates input.

        This step never allows or denies an action. The normal preflight remains
        authoritative after Pydantic validation and before side effects.
        """
        session_id = _session_id(tool_metadata)
        state = self._plan_states.get(session_id or "__default__")
        if state is None and tool_metadata is not None:
            state = self._contract_state(
                None,
                session_id,
                metadata=tool_metadata,
            )
        if state is None:
            return tool_input
        milestones = state.get("milestones", {})
        candidates = [
            (milestone_id, item)
            for milestone_id, item in milestones.items()
            if str(item.get("tool_name") or "").casefold() == tool_name.casefold()
            and all(
                str(dependency) in state["completed"]
                for dependency in item.get("depends_on", [])
            )
            and not _milestone_at_limit(state, milestone_id)
        ]
        if not candidates:
            return tool_input
        milestone_id, milestone = candidates[0]
        replacement = self._bound_input(milestone, tool_input, state)
        if replacement != tool_input and tool_use_id:
            prepared_inputs = state.setdefault("prepared_inputs", {})
            prepared_inputs[tool_use_id] = {
                "milestone_id": milestone_id,
                "bound_parameters": sorted(set(replacement) - set(tool_input)),
            }
            if len(prepared_inputs) > 128:
                del prepared_inputs[next(iter(prepared_inputs))]
        return replacement

    async def preflight(self, request: DirectorRequest) -> DirectorDecision:
        """检查一次即将执行的工具调用，并决定放行、阻断或替换。

        已通过缓存的工具会直接放行；已注册工具的首次真实调用承担最小可用性
        验证，避免为检查而重复执行可能有副作用的工具。未注册工具才进入 MCP
        检索、标准配置、连接和工具注册流程。
        """
        session_id = _session_id(request.tool_metadata)
        if not request.missing_tool:
            plan_decision = self._preflight_contract(request, session_id)
            if plan_decision is not None:
                return plan_decision
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

    def observe_result(
        self,
        tool_name: str,
        is_error: bool,
        tool_metadata: dict[str, object] | None = None,
        tool_use_id: str = "",
        tool_output: str = "",
    ) -> None:
        """Record plan progress and invalidate a failed availability check."""
        session_id = _session_id(tool_metadata)
        self._observe_contract_result(
            session_id=session_id,
            tool_name=tool_name,
            tool_use_id=tool_use_id,
            is_error=is_error,
            tool_output=tool_output,
            tool_metadata=tool_metadata,
        )
        if not is_error:
            return
        self._passed_tools.discard(tool_name)
        self._emit("tool_result", tool_name, "failed", "实际调用报错，已取消该工具的会话校验缓存", session_id, tool_use_id=tool_use_id)

    def _contract_state(
        self,
        request: DirectorRequest | None,
        session_id: str,
        *,
        metadata: dict[str, object] | None = None,
    ) -> dict[str, Any] | None:
        metadata = metadata or (
            request.tool_metadata
            if request is not None and request.tool_metadata is not None
            else {}
        )
        raw = metadata.get("writer_execution_contract")
        if not isinstance(raw, dict):
            return None
        milestones = raw.get("milestones")
        if not isinstance(milestones, list):
            return None
        normalized: list[dict[str, Any]] = []
        for item in milestones[:128]:
            if not isinstance(item, dict):
                continue
            milestone_id = str(item.get("id") or "").strip()
            if milestone_id:
                normalized.append(item)
        fingerprint = contract_json(
            {"source": raw.get("source"), "milestones": normalized}
        )
        key = session_id or "__default__"
        state = self._plan_states.get(key)
        if state is None or state.get("fingerprint") != fingerprint:
            state = {
                "fingerprint": fingerprint,
                "source": str(raw.get("source") or ""),
                "milestones": {
                    str(item["id"]): copy.deepcopy(item) for item in normalized
                },
                "attempts": {},
                "successes": {},
                "errors": {},
                "completed": [],
                "in_flight": {},
                "prepared_inputs": {},
                "results": {},
            }
            saved = metadata.get("writer_plan_state")
            if isinstance(saved, dict):
                for counter_name in ("attempts", "successes", "errors"):
                    counter = saved.get(counter_name)
                    if isinstance(counter, dict):
                        state[counter_name] = {
                            str(key): int(value)
                            for key, value in counter.items()
                            if isinstance(value, int) and value >= 0
                        }
                completed = saved.get("completed")
                if isinstance(completed, list):
                    state["completed"] = [
                        str(value)
                        for value in completed
                        if str(value) in state["milestones"]
                    ]
            self._plan_states[key] = state
        self._publish_plan_state(metadata, state)
        return state

    @staticmethod
    def _publish_plan_state(
        metadata: dict[str, object],
        state: dict[str, Any],
    ) -> None:
        """Expose compact JSON-safe counters to the session trace."""
        metadata["writer_plan_state"] = {
            "source": state.get("source", ""),
            "attempts": dict(state.get("attempts", {})),
            "successes": dict(state.get("successes", {})),
            "errors": dict(state.get("errors", {})),
            "completed": list(state.get("completed", [])),
        }

    def _preflight_contract(
        self,
        request: DirectorRequest,
        session_id: str,
    ) -> DirectorDecision | None:
        state = self._contract_state(request, session_id)
        if state is None:
            return None
        milestones = state["milestones"]
        in_flight = state.get("in_flight", {})
        if (
            request.tool_use_id
            and isinstance(in_flight, dict)
            and request.tool_use_id in in_flight
        ):
            # QueryEngine re-validates a Director replacement with the same
            # tool-use id. Do not consume a second call slot for that pass.
            self._emit(
                "plan_check",
                request.tool_name,
                "skipped",
                "同一 tool_use_id 的替换参数已完成首次计划检查",
                session_id,
                {
                    "milestone_id": in_flight[request.tool_use_id],
                    "replacement_revalidation": True,
                },
                request.tool_use_id,
            )
            return None
        prepared_inputs = state.get("prepared_inputs", {})
        prepared = (
            prepared_inputs.pop(request.tool_use_id, None)
            if isinstance(prepared_inputs, dict) and request.tool_use_id
            else None
        )
        matching_ids = [
            key
            for key, item in milestones.items()
            if str(item.get("tool_name") or "").casefold()
            == request.tool_name.casefold()
        ]
        if not matching_ids:
            self._emit(
                "plan_check",
                request.tool_name,
                "skipped",
                "Writer contract 未声明该工具，按兼容策略观察并放行",
                session_id,
                {"contract_source": state.get("source", ""), "tracked": False},
                request.tool_use_id,
            )
            return None
        prepared_id = (
            str(prepared.get("milestone_id") or "")
            if isinstance(prepared, dict)
            else ""
        )
        milestone_id = prepared_id if prepared_id in matching_ids else next(
            (
                key
                for key in matching_ids
                if not _milestone_at_limit(state, key)
                and all(
                    str(dependency) in state["completed"]
                    for dependency in milestones[key].get("depends_on", [])
                )
            ),
            next(
                (
                    key
                    for key in matching_ids
                    if not _milestone_at_limit(state, key)
                ),
                matching_ids[0],
            ),
        )
        item = milestones[milestone_id]
        attempts = state["attempts"].get(milestone_id, 0)
        errors = state["errors"].get(milestone_id, 0)
        max_calls = _positive_int(item.get("max_calls"))
        max_retries = _nonnegative_int(item.get("max_retries"))
        tool = request.tool_registry.get(request.tool_name)
        read_only = bool(
            tool is not None
            and request.parsed_input is not None
            and tool.is_read_only(request.parsed_input)
        )
        if _milestone_at_limit(state, milestone_id):
            if read_only:
                self._emit(
                    "plan_check",
                    request.tool_name,
                    "skipped",
                    f"Writer milestone {milestone_id} 已达到上限，但只读动作按兼容策略放行",
                    session_id,
                    {"milestone_id": milestone_id, "read_only": True},
                    request.tool_use_id,
                )
                return None
            self._emit(
                "plan_check",
                request.tool_name,
                "blocked",
                f"Writer milestone {milestone_id} 已达到调用上限",
                session_id,
                {
                    "milestone_id": milestone_id,
                    "attempts": attempts,
                    "max_calls": max_calls,
                    "max_retries": max_retries,
                    "errors": errors,
                },
                request.tool_use_id,
            )
            return DirectorDecision(
                "deny",
                f"Writer milestone {milestone_id} exceeded its bounded call limit",
            )
        dependencies = [
            str(value)
            for value in item.get("depends_on", [])
            if str(value).strip()
        ]
        missing = [
            value for value in dependencies if value not in state["completed"]
        ]
        if missing:
            if read_only or state.get("source") == "writer_legacy_fallback":
                self._emit(
                    "plan_check",
                    request.tool_name,
                    "skipped",
                    f"Writer milestone {milestone_id} 依赖未完成，但兼容策略允许只读或 fallback 动作",
                    session_id,
                    {
                        "milestone_id": milestone_id,
                        "missing_dependencies": missing,
                        "read_only": read_only,
                    },
                    request.tool_use_id,
                )
                return None
            self._emit(
                "plan_check",
                request.tool_name,
                "blocked",
                f"Writer milestone {milestone_id} 依赖尚未完成",
                session_id,
                {"milestone_id": milestone_id, "missing_dependencies": missing},
                request.tool_use_id,
            )
            return DirectorDecision(
                "deny",
                f"Writer milestone {milestone_id} requires: {', '.join(missing)}",
            )
        replacement = self._bound_input(item, request.tool_input, state)
        state["attempts"][milestone_id] = attempts + 1
        in_flight_key = request.tool_use_id or f"{request.tool_name}:{attempts + 1}"
        state["in_flight"][in_flight_key] = milestone_id
        if request.tool_metadata is not None:
            self._publish_plan_state(request.tool_metadata, state)
        if replacement != request.tool_input:
            self._emit(
                "plan_check",
                request.tool_name,
                "passed",
                f"Writer milestone {milestone_id} 参数已从前置结果补齐",
                session_id,
                {
                    "milestone_id": milestone_id,
                    "bound_parameters": sorted(
                        set(replacement) - set(request.tool_input)
                    ),
                },
                request.tool_use_id,
            )
            return DirectorDecision(
                "replace",
                "已按 Writer milestone 继承前置结果参数",
                request.tool_name,
                replacement,
            )
        self._emit(
            "plan_check",
            request.tool_name,
            "passed",
            (
                f"Writer milestone {milestone_id} 参数继承、顺序与调用约束通过"
                if prepared
                else f"Writer milestone {milestone_id} 顺序与调用约束通过"
            ),
            session_id,
            {
                "milestone_id": milestone_id,
                "attempts": attempts + 1,
                "bound_parameters": (
                    list(prepared.get("bound_parameters", []))
                    if isinstance(prepared, dict)
                    else []
                ),
            },
            request.tool_use_id,
        )
        return None

    def _bound_input(
        self,
        milestone: dict[str, Any],
        tool_input: dict[str, object],
        state: dict[str, Any],
    ) -> dict[str, object]:
        bindings = milestone.get("parameter_bindings")
        if not isinstance(bindings, dict):
            return tool_input
        replacement = copy.deepcopy(tool_input)
        for target, binding in bindings.items():
            target_name = str(target).strip()
            if (
                not target_name
                or _input_path_get(replacement, target_name) not in (None, "")
            ):
                continue
            value = _resolve_binding(binding, state)
            if value is not None:
                _input_path_set(replacement, target_name, value)
        return replacement

    def _observe_contract_result(
        self,
        *,
        session_id: str,
        tool_name: str,
        tool_use_id: str,
        is_error: bool,
        tool_output: str,
        tool_metadata: dict[str, object] | None,
    ) -> None:
        state = self._plan_states.get(session_id or "__default__")
        if state is None:
            return
        in_flight = state.get("in_flight", {})
        milestone_id = (
            in_flight.pop(tool_use_id, None)
            if isinstance(in_flight, dict) and tool_use_id
            else None
        )
        if milestone_id is None:
            milestone_id = next(
                (
                    key
                    for key, item in state["milestones"].items()
                    if str(item.get("tool_name") or "").casefold()
                    == tool_name.casefold()
                ),
                None,
            )
        if milestone_id is None:
            return
        if is_error:
            state["errors"][milestone_id] = (
                state["errors"].get(milestone_id, 0) + 1
            )
            postcondition_status = "tool_failed"
        else:
            success_count = (
                state["successes"].get(milestone_id, 0) + 1
            )
            state["successes"][milestone_id] = success_count
            minimum = _nonnegative_int(
                state["milestones"][milestone_id].get("min_calls")
            )
            if minimum is None:
                minimum = 1
            if success_count >= minimum and milestone_id not in state["completed"]:
                state["completed"].append(milestone_id)
            postcondition_status = "tool_success_observed"
        state["results"][milestone_id] = {
            "output": str(tool_output or "")[:8000],
            "is_error": bool(is_error),
        }
        if tool_metadata is not None:
            self._publish_plan_state(tool_metadata, state)
        self._emit(
            "plan_result",
            tool_name,
            "failed" if is_error else "passed",
            f"Writer milestone {milestone_id} 结果已记录",
            session_id,
            {
                "milestone_id": milestone_id,
                "is_error": bool(is_error),
                "postcondition": str(
                    state["milestones"][milestone_id].get("postcondition") or ""
                ),
                "postcondition_status": postcondition_status,
            },
            tool_use_id,
        )

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


def _positive_int(value: object) -> int | None:
    if isinstance(value, bool):
        return None
    try:
        parsed = int(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None
    return parsed if parsed > 0 else None


def _nonnegative_int(value: object) -> int | None:
    if isinstance(value, bool):
        return None
    try:
        parsed = int(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None
    return parsed if parsed >= 0 else None


def _milestone_at_limit(state: dict[str, Any], milestone_id: str) -> bool:
    item = state.get("milestones", {}).get(milestone_id, {})
    max_calls = _positive_int(item.get("max_calls"))
    if max_calls is None:
        return False
    attempts = int(state.get("attempts", {}).get(milestone_id, 0) or 0)
    successes = int(state.get("successes", {}).get(milestone_id, 0) or 0)
    errors = int(state.get("errors", {}).get(milestone_id, 0) or 0)
    retries = _nonnegative_int(item.get("max_retries")) or 0
    # Reserved in-flight calls consume the declared call budget. Observed
    # failures add only the explicitly configured number of retry slots.
    total_slots = max_calls + min(errors, retries)
    return successes >= max_calls or attempts >= total_slots or errors > retries


def _input_path_get(value: object, path: str) -> object | None:
    current = value
    for part in path.split("."):
        if not isinstance(current, dict) or part not in current:
            return None
        current = current[part]
    return current


def _input_path_set(value: dict[str, object], path: str, replacement: object) -> None:
    parts = [part for part in path.split(".") if part]
    if not parts:
        return
    current = value
    for part in parts[:-1]:
        child = current.get(part)
        if not isinstance(child, dict):
            child = {}
            current[part] = child
        current = child
    current[parts[-1]] = replacement


def _resolve_binding(binding: object, state: dict[str, Any]) -> object | None:
    if isinstance(binding, str):
        source_id = binding
        output_path = ""
    elif isinstance(binding, dict):
        source_id = str(
            binding.get("from_milestone")
            or binding.get("milestone_id")
            or ""
        ).strip()
        output_path = str(binding.get("output_path") or "").strip()
    else:
        return None
    if not source_id:
        return None
    result = state.get("results", {}).get(source_id)
    if not isinstance(result, dict) or result.get("is_error"):
        return None
    raw_output = result.get("output")
    if not isinstance(raw_output, str) or not raw_output.strip():
        return None
    try:
        parsed: object = json.loads(raw_output)
    except (TypeError, ValueError):
        return None
    if output_path:
        return _input_path_get(parsed, output_path)
    return parsed
