"""维护 OpenHarness 工具能力摘要，并为剧本生成与评估提供能力匹配辅助。"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any


@dataclass(frozen=True)
class CapabilitySpec:
    """描述一类任务能力及其在 OpenHarness 中可能对应的工具集合。

    参数说明：
    - name: 面向剧本和评估阶段展示的人类可读能力名。
    - description: 对该能力适用范围的简短说明。
    - aliases: 用于在 query、执行剧本或演员输出中做关键词命中的同义词集合。
    - matched_tools: 当该能力被命中时，优先提示给模型参考的具体工具名或工具组。
    """

    name: str
    description: str
    aliases: tuple[str, ...]
    matched_tools: tuple[str, ...]


OPENHARNESS_CAPABILITY_SPECS: tuple[CapabilitySpec, ...] = (
    CapabilitySpec("文件与代码检索", "读取文件、搜索代码、列出目录与定位内容", ("读取文件", "代码检索", "搜索文件", "repo 检索", "read", "grep", "glob"), ("Read", "Grep", "Glob", "LS")),
    CapabilitySpec("文件修改", "创建、修改或删除本地文件", ("修改文件", "写入文件", "代码修改", "patch", "edit"), ("apply_patch", "DeleteFile")),
    CapabilitySpec("命令执行与测试", "执行脚本、编译、运行测试并查看状态", ("运行测试", "命令执行", "编译验证", "shell", "pytest", "build"), ("RunCommand", "CheckCommandStatus", "StopCommand")),
    CapabilitySpec("网络检索", "搜索互联网并抓取网页内容", ("搜索网页", "联网检索", "官网查询", "web search", "search", "fetch"), ("WebSearch", "WebFetch")),
    CapabilitySpec("用户澄清", "在执行前向用户补充确认关键信息", ("用户确认", "补充澄清", "clarify", "ask user"), ("AskUserQuestion",)),
    CapabilitySpec("子任务分解", "将复杂任务拆分为子任务或委派子代理", ("任务拆解", "委派", "subagent", "task delegation"), ("Task",)),
    CapabilitySpec("浏览器交互", "执行页面点击、输入、表单交互等浏览器动作", ("网页登录", "页面点击", "浏览器操作", "browser", "form"), ("integrated_browser(browser tools)",)),
    CapabilitySpec("扩展系统调用", "通过 MCP 或外部系统工具访问附加能力", ("数据库访问", "外部系统", "mcp", "api 集成"), ("run_mcp",)),
)


def _load_static_tool_catalog() -> list[dict[str, str]]:
    """从 OpenHarness 工具目录加载静态工具清单。

    该函数会扫描 `OpenHarness/src/openharness/tools` 下的工具实现文件，提取：
    - 工具名
    - 文件名
    - 顶层 docstring 的首行摘要

    返回值用于在剧本生成 prompt 中向模型注入“当前演员 Harness 已知工具列表”。
    若工具目录不存在，则返回空列表，以便在缺少源码目录时优雅降级。
    """

    tools_dir = Path(__file__).resolve().parents[1] / "OpenHarness" / "src" / "openharness" / "tools"
    if not tools_dir.exists():
        return []
    catalog: list[dict[str, str]] = []
    for path in sorted(tools_dir.glob("*.py")):
        if path.name in {"__init__.py", "base.py"}:
            continue
        text = path.read_text(encoding="utf-8")
        doc = ""
        if text.startswith('"""'):
            parts = text.split('"""', 2)
            if len(parts) >= 3:
                doc = parts[1].strip().splitlines()[0].strip()
        tool_name = ""
        for line in text.splitlines():
            stripped = line.strip()
            if stripped.startswith("name = "):
                tool_name = stripped.split("=", 1)[1].strip().strip('"').strip("'")
                break
        catalog.append(
            {
                "tool_name": tool_name or path.stem.replace("_tool", ""),
                "file_name": path.name,
                "summary_en": doc or path.stem,
            }
        )
    return catalog


OPENHARNESS_TOOL_CATALOG = _load_static_tool_catalog()
OPENHARNESS_TOOL_SUMMARY_MAP = {item["tool_name"]: item["summary_en"] for item in OPENHARNESS_TOOL_CATALOG}


def build_openharness_tool_prompt_context(language: str) -> str:
    """构建可直接注入到剧本生成 prompt 中的 OpenHarness 工具能力摘要。

    参数说明：
    - language: 目标输出语言。`zh` 生成中文说明，其他值生成英文说明。

    返回值为一段面向模型的说明文字，内容包括：
    - 按能力分组的工具摘要
    - available_tools / missing_tools 的填写约束
    """

    grouped_lines: list[str] = []
    for spec in OPENHARNESS_CAPABILITY_SPECS:
        tool_summaries = [f"{tool}: {OPENHARNESS_TOOL_SUMMARY_MAP.get(tool, spec.description)}" for tool in spec.matched_tools]
        if language == "zh":
            grouped_lines.append(f"- {spec.name}：{spec.description}；可优先考虑工具 {', '.join(tool_summaries)}")
        else:
            grouped_lines.append(f"- {spec.name}: {spec.description}; candidate tools: {', '.join(tool_summaries)}")
    if language == "zh":
        return "\n".join(
            [
                "当前演员 Harness（OpenHarness）已知工具能力摘要如下，可作为 available_tools / missing_tools 判断依据：",
                *grouped_lines,
                "要求：available_tools 优先填写当前任务确实可能用上的已有工具或工具组合；missing_tools 只填写现有工具集合无法覆盖的能力、权限、登录态或外部条件。不要只写泛化能力名词，尽量写出具体工具名或可组合工具。",
            ]
        )
    return "\n".join(
        [
            "The actor Harness (OpenHarness) is known to provide the following tool capabilities. Use this as the basis for available_tools / missing_tools:",
            *grouped_lines,
            "Requirement: available_tools should prioritize concrete existing tools or tool combinations that are realistically useful for this task. missing_tools should only describe capabilities, permissions, login state, or external conditions that the current tool set cannot cover. Avoid vague capability-only labels whenever concrete tools can be named.",
        ]
    )


def _normalize_text(value: str) -> str:
    """对文本做轻量归一化，便于后续按关键词进行能力匹配。"""

    return value.strip().lower()


def _collect_candidate_texts(query: str, report: dict[str, Any] | None, actor_harness_output: str) -> list[str]:
    """收集用于能力匹配的候选文本。

    参数说明：
    - query: 用户原始任务输入。
    - report: 结构化执行剧本字典，可为空。
    - actor_harness_output: 演员 Harness 当前输出文本。

    该函数会把任务目标、成功标准、推荐步骤、验证步骤、可用工具、缺失工具等字段
    平铺为字符串列表，供后续能力关键词匹配使用。
    """

    values: list[str] = [query, actor_harness_output]
    if isinstance(report, dict):
        task_profile = report.get("task_profile") if isinstance(report.get("task_profile"), dict) else {}
        difficulty_profile = report.get("difficulty_profile") if isinstance(report.get("difficulty_profile"), dict) else {}
        execution_plan = report.get("execution_plan") if isinstance(report.get("execution_plan"), dict) else {}
        values.extend(
            [
                str(task_profile.get("task_goal", "")),
                str(task_profile.get("expected_output", "")),
                " ".join(str(item) for item in task_profile.get("success_criteria", []) or []),
                " ".join(str(item) for item in execution_plan.get("recommended_steps", []) or []),
                " ".join(str(item) for item in execution_plan.get("validation_steps", []) or []),
                " ".join(str(item) for item in difficulty_profile.get("available_tools", []) or []),
                " ".join(str(item) for item in difficulty_profile.get("missing_tools", []) or []),
                " ".join(str(item) for item in difficulty_profile.get("unknown_conditions", []) or []),
            ]
        )
    return [item for item in values if item.strip()]


def match_openharness_capabilities(query: str, report: dict[str, Any] | None, actor_harness_output: str) -> dict[str, Any]:
    """根据任务输入、结构化剧本和演员输出推断所需能力及可能的工具覆盖情况。

    参数说明：
    - query: 用户原始任务输入。
    - report: 结构化执行剧本字典，可为空。
    - actor_harness_output: 演员 Harness 当前输出文本。

    返回结果包含：
    - required_capabilities: 命中的能力类别
    - available_tools: 推断为当前任务可优先使用的工具或工具组合
    - missing_tools: 可能仍需补充的能力、权限或外部条件
    - tool_match_details: 每条能力命中的详细依据
    - tool_match_confidence / tool_match_rationale: 便于后续展示或调试的辅助说明
    """

    candidate_texts = [_normalize_text(item) for item in _collect_candidate_texts(query, report, actor_harness_output)]
    matched_details: list[dict[str, Any]] = []
    available_tools: list[str] = []
    missing_tools: list[str] = []
    required_capabilities: list[str] = []

    for spec in OPENHARNESS_CAPABILITY_SPECS:
        hit_aliases = sorted({alias for alias in spec.aliases if any(alias in text for text in candidate_texts)})
        if not hit_aliases:
            continue
        required_capabilities.append(spec.name)
        coverage = "high"
        if spec.name in {"浏览器交互", "扩展系统调用"}:
            coverage = "partial"
        matched_tool_text = [f"{tool}｜{OPENHARNESS_TOOL_SUMMARY_MAP.get(tool, spec.description)}" for tool in spec.matched_tools]
        available_tools.extend(matched_tool_text)
        if coverage != "high":
            missing_tools.append(f"{spec.name} 仍需确认具体权限、登录态或外部系统接入条件")
        matched_details.append(
            {
                "required_capability": spec.name,
                "matched_tools": list(spec.matched_tools),
                "coverage": coverage,
                "reason": f"从 query / 编剧输出中命中需求线索：{', '.join(hit_aliases)}",
            }
        )

    unique_available = list(dict.fromkeys(available_tools))
    unique_missing = list(dict.fromkeys(missing_tools))
    confidence = "high" if matched_details else "low"
    rationale = "基于任务目标、执行剧本和演员 Harness 输出中的需求线索，与 OpenHarness 工具能力做功能相似匹配。"
    return {
        "required_capabilities": required_capabilities,
        "available_tools": unique_available,
        "missing_tools": unique_missing,
        "tool_match_details": matched_details,
        "tool_match_confidence": confidence,
        "tool_match_rationale": rationale,
    }
