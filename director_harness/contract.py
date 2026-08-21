"""Small, versioned execution-contract helpers shared by Writer and Director.

The team Writer v1 package remains the source-of-truth deployment.  This module
is deliberately kept in the integration layer so old Writer reports continue to
work while newer reports can carry explicit milestone constraints.
"""

from __future__ import annotations

import copy
import json
from dataclasses import dataclass, field
from typing import Any, Mapping, Sequence


@dataclass(frozen=True)
class ExecutionMilestone:
    """A conservative, optionally constrained unit of an execution plan."""

    id: str
    goal: str = ""
    tool_name: str = ""
    required: bool = True
    depends_on: tuple[str, ...] = ()
    required_parameters: tuple[str, ...] = ()
    min_calls: int = 0
    max_calls: int | None = None
    max_retries: int | None = None
    parameter_bindings: dict[str, Any] = field(default_factory=dict)
    postcondition: str = ""

    def to_dict(self) -> dict[str, Any]:
        value: dict[str, Any] = {
            "id": self.id,
            "goal": self.goal,
            "tool_name": self.tool_name,
            "required": self.required,
            "depends_on": list(self.depends_on),
            "required_parameters": list(self.required_parameters),
            "min_calls": self.min_calls,
            "parameter_bindings": copy.deepcopy(self.parameter_bindings),
            "postcondition": self.postcondition,
        }
        if self.max_calls is not None:
            value["max_calls"] = self.max_calls
        if self.max_retries is not None:
            value["max_retries"] = self.max_retries
        return value


def _int_or_none(value: Any, *, minimum: int = 0) -> int | None:
    if isinstance(value, bool):
        return None
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return None
    return parsed if parsed >= minimum else None


def _string_list(value: Any) -> tuple[str, ...]:
    if not isinstance(value, (list, tuple)):
        return ()
    return tuple(str(item).strip() for item in value if str(item).strip())


def _milestone_from_mapping(item: Mapping[str, Any], index: int) -> ExecutionMilestone | None:
    milestone_id = str(item.get("id") or item.get("milestone_id") or f"milestone-{index:03d}").strip()
    if not milestone_id:
        return None
    tool_name = str(item.get("tool_name") or item.get("tool") or "").strip()
    goal = str(item.get("goal") or item.get("description") or item.get("step") or "").strip()
    required = item.get("required", True)
    if not isinstance(required, bool):
        required = str(required).strip().lower() not in {"0", "false", "no", "optional"}
    min_calls = _int_or_none(item.get("min_calls"), minimum=0)
    if min_calls is None:
        min_calls = 1 if required else 0
    max_calls = _int_or_none(item.get("max_calls"), minimum=1)
    max_retries = _int_or_none(item.get("max_retries"), minimum=0)
    if max_calls is not None and max_calls < min_calls:
        # A malformed bound must not make a required milestone impossible.
        max_calls = None
    bindings = item.get("parameter_bindings")
    if not isinstance(bindings, dict):
        bindings = {}
    postcondition = str(item.get("postcondition") or "").strip()
    return ExecutionMilestone(
        id=milestone_id,
        goal=goal,
        tool_name=tool_name,
        required=required,
        depends_on=_string_list(item.get("depends_on")),
        required_parameters=_string_list(item.get("required_parameters")),
        min_calls=min_calls,
        max_calls=max_calls,
        max_retries=max_retries,
        parameter_bindings=copy.deepcopy(bindings),
        postcondition=postcondition,
    )


def _extract_live_names(live_tool_schemas: Sequence[Mapping[str, Any]] | None) -> tuple[str, ...]:
    if not live_tool_schemas:
        return ()
    return tuple(
        dict.fromkeys(
            str(schema.get("name") or "").strip()
            for schema in live_tool_schemas
            if str(schema.get("name") or "").strip()
        )
    )


def _required_schema_parameters(
    tool_name: str,
    live_tool_schemas: Sequence[Mapping[str, Any]] | None,
) -> tuple[str, ...]:
    for schema in live_tool_schemas or ():
        if str(schema.get("name") or "").casefold() != tool_name.casefold():
            continue
        root = schema.get("input_schema", schema.get("inputSchema"))
        if not isinstance(root, Mapping):
            return ()
        required: list[str] = []

        def visit(node: Mapping[str, Any], prefix: str = "") -> None:
            properties = node.get("properties")
            properties = properties if isinstance(properties, Mapping) else {}
            names = node.get("required")
            names = names if isinstance(names, list) else []
            for raw_name in names:
                name = str(raw_name).strip()
                if not name:
                    continue
                path = f"{prefix}.{name}" if prefix else name
                if path not in required:
                    required.append(path)
                child = properties.get(name)
                if isinstance(child, Mapping):
                    visit(child, path)

        visit(root)
        return tuple(required)
    return ()


def _resolve_tool_name(label: str, live_names: Sequence[str]) -> str:
    """Resolve only exact names and a deliberately tiny legacy alias table."""

    normalized = label.strip().casefold()
    by_fold = {name.casefold(): name for name in live_names}
    if normalized in by_fold:
        return by_fold[normalized]
    aliases = {
        "read": ("read_file",),
        "grep": ("grep",),
        "glob": ("glob",),
        "ls": ("glob",),
        "write": ("write_file", "edit_file"),
        "edit": ("edit_file", "write_file"),
        "apply_patch": ("edit_file", "write_file"),
        "runcommand": ("bash",),
        "run_command": ("bash",),
        "search": ("web_search",),
        "fetch": ("web_fetch",),
    }
    for candidate in aliases.get(normalized, ()):
        if candidate.casefold() in by_fold:
            return by_fold[candidate.casefold()]
    return ""


def _step_tool_name(step: str, live_names: Sequence[str], report_tools: Sequence[Any]) -> str:
    lowered = step.casefold()
    for name in live_names:
        if name.casefold() in lowered:
            return name
    for raw in report_tools:
        label = str(raw).split("｜", 1)[0].split(":", 1)[0].strip()
        if label and label.casefold() in lowered:
            resolved = _resolve_tool_name(label, live_names)
            if resolved:
                return resolved
    # A step often starts with ``tool: ...``.  Resolve that token only, never
    # arbitrary words from the natural-language description.
    token = step.split("｜", 1)[0].split(":", 1)[0].strip().split()[0] if step.strip() else ""
    return _resolve_tool_name(token, live_names)


def build_execution_contract(
    report: Mapping[str, Any] | None,
    *,
    live_tool_schemas: Sequence[Mapping[str, Any]] | None = None,
) -> dict[str, Any]:
    """Return a stable contract, preserving old reports through a safe fallback.

    Explicit Writer milestones retain their constraints.  For legacy reports we
    derive only tool/goal links and required schema parameters; cardinality and
    ordering remain unconstrained so the adapter cannot accidentally block a
    valid fan-out or multi-stage task.
    """

    source_report = report if isinstance(report, Mapping) else {}
    execution_plan = source_report.get("execution_plan")
    execution_plan = execution_plan if isinstance(execution_plan, Mapping) else {}
    live_names = _extract_live_names(live_tool_schemas)
    difficulty = source_report.get("difficulty_profile")
    difficulty = difficulty if isinstance(difficulty, Mapping) else {}
    report_tools = difficulty.get("available_tools")
    report_tools = report_tools if isinstance(report_tools, list) else []
    raw_milestones = execution_plan.get("milestones")
    explicit = isinstance(raw_milestones, list) and any(isinstance(item, Mapping) for item in raw_milestones)
    raw_milestones_list = raw_milestones if isinstance(raw_milestones, list) else []
    milestones: list[ExecutionMilestone] = []
    if explicit:
        for index, item in enumerate(raw_milestones_list, 1):
            if not isinstance(item, Mapping):
                continue
            milestone = _milestone_from_mapping(item, index)
            if milestone is None:
                continue
            resolved = _resolve_tool_name(milestone.tool_name, live_names)
            resolved = resolved or milestone.tool_name
            schema_parameters = (
                milestone.required_parameters
                or _required_schema_parameters(resolved, live_tool_schemas)
            )
            if (
                resolved != milestone.tool_name
                or schema_parameters != milestone.required_parameters
            ):
                milestone = ExecutionMilestone(
                    id=milestone.id,
                    goal=milestone.goal,
                    tool_name=resolved,
                    required=milestone.required,
                    depends_on=milestone.depends_on,
                    required_parameters=schema_parameters,
                    min_calls=milestone.min_calls,
                    max_calls=milestone.max_calls,
                    max_retries=milestone.max_retries,
                    parameter_bindings=milestone.parameter_bindings,
                    postcondition=milestone.postcondition,
                )
            milestones.append(milestone)
        known_ids = {milestone.id for milestone in milestones}
        milestones = [
            ExecutionMilestone(
                id=milestone.id,
                goal=milestone.goal,
                tool_name=milestone.tool_name,
                required=milestone.required,
                depends_on=tuple(
                    dependency
                    for dependency in milestone.depends_on
                    if dependency in known_ids and dependency != milestone.id
                ),
                required_parameters=milestone.required_parameters,
                min_calls=milestone.min_calls,
                max_calls=milestone.max_calls,
                max_retries=milestone.max_retries,
                parameter_bindings=milestone.parameter_bindings,
                postcondition=milestone.postcondition,
            )
            for milestone in milestones
        ]
    if not milestones:
        steps = execution_plan.get("recommended_steps")
        steps = steps if isinstance(steps, list) else []
        for index, raw_step in enumerate(steps, 1):
            goal = str(raw_step).strip()
            if not goal:
                continue
            tool_name = _step_tool_name(goal, live_names, report_tools)
            milestones.append(
                ExecutionMilestone(
                    id=f"fallback-{index:03d}",
                    goal=goal,
                    tool_name=tool_name,
                    required=True,
                    required_parameters=_required_schema_parameters(
                        tool_name,
                        live_tool_schemas,
                    ),
                )
            )
    return {
        "schema_version": 1,
        "source": "writer_milestones" if explicit and milestones else "writer_legacy_fallback",
        "live_tool_names": list(live_names),
        "milestones": [milestone.to_dict() for milestone in milestones],
    }


def contract_json(contract: Mapping[str, Any]) -> str:
    """Serialize a contract for prompt/debug output without unstable ordering."""

    return json.dumps(contract, ensure_ascii=False, sort_keys=True)
