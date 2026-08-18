"""维护 OpenHarness 工具能力摘要，并为剧本生成与评估提供能力匹配辅助。"""

from __future__ import annotations

from dataclasses import dataclass
import json
import os
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
    CapabilitySpec("文件与代码检索", "读取文件、搜索代码、列出目录与定位内容", ("读取文件", "代码检索", "搜索文件", "repo 检索", "read", "grep", "glob"), ("read_file", "grep", "glob")),
    CapabilitySpec("文件修改", "创建、修改或删除本地文件", ("修改文件", "写入文件", "代码修改", "patch", "edit"), ("write_file", "edit_file")),
    CapabilitySpec("命令执行与测试", "执行脚本、编译、运行测试并查看状态", ("运行测试", "命令执行", "编译验证", "shell", "pytest", "build"), ("bash",)),
    CapabilitySpec("网络检索", "搜索互联网并抓取网页内容", ("搜索网页", "联网检索", "官网查询", "web search", "search", "fetch"), ("web_search", "web_fetch")),
    CapabilitySpec("用户澄清", "在执行前向用户补充确认关键信息", ("用户确认", "补充澄清", "clarify", "ask user"), ("ask_user_question",)),
    CapabilitySpec("子任务分解", "将复杂任务拆分为子任务或委派子代理", ("任务拆解", "委派", "subagent", "task delegation"), ("agent", "task_create")),
    CapabilitySpec("浏览器交互", "执行页面点击、输入、表单交互等浏览器动作", ("网页登录", "页面点击", "浏览器操作", "browser", "form"), ()),
    CapabilitySpec("扩展系统调用", "通过 MCP 或外部系统工具访问附加能力", ("数据库访问", "外部系统", "mcp", "api 集成"), ()),
)


def discover_openharness_tools() -> list[dict[str, str]]:
    """从 OpenHarness 工具目录加载静态工具清单。

    该函数会扫描 `OpenHarness/src/openharness/tools` 下的工具实现文件，提取：
    - 工具名
    - 文件名
    - 顶层 docstring 的首行摘要

    返回值用于在剧本生成 prompt 中向模型注入“当前演员 Harness 已知工具列表”。
    若工具目录不存在，则返回空列表，以便在缺少源码目录时优雅降级。
    """

    source_root = Path(os.environ.get("OPENHARNESS_SRC", Path(__file__).resolve().parents[1] / "OpenHarness" / "src"))
    tools_dir = source_root / "openharness" / "tools"
    if not tools_dir.exists():
        return []
    catalog: list[dict[str, str]] = []
    for path in sorted(tools_dir.glob("*.py")):
        if path.name in {"__init__.py", "base.py"}:
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except OSError:
            continue
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
                "availability": "direct",
                "source": "builtin",
            }
        )
    return catalog


def discover_configured_mcp_tools() -> list[dict[str, str]]:
    config_dir = Path(os.environ.get("OPENHARNESS_CONFIG_DIR", Path.home() / ".openharness"))
    config_path = config_dir / "settings.json"
    if not config_path.is_file():
        return []
    try:
        payload = json.loads(config_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return []
    raw_servers = payload.get("mcp_servers", payload.get("mcpServers", {})) if isinstance(payload, dict) else {}
    if not isinstance(raw_servers, dict):
        return []
    return [
        {
            "tool_name": f"mcp__{name}__<runtime-discovered>",
            "file_name": "settings.json",
            "summary_en": "Configured MCP server; its callable tools are discovered when OpenHarness connects.",
            "availability": "runtime_discovery",
            "source": "configured_mcp",
        }
        for name, config in sorted(raw_servers.items())
        if isinstance(name, str) and isinstance(config, dict)
    ]


def discover_approved_mcp_tools() -> list[dict[str, str]]:
    default_path = Path(__file__).resolve().parents[1] / "director_harness" / "director_mcp_catalog.json"
    catalog_path = Path(os.environ.get("DIRECTOR_MCP_CATALOG", default_path)).expanduser()
    if not catalog_path.is_file():
        return []
    try:
        payload = json.loads(catalog_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return []
    candidates = payload.get("mcp_candidates", []) if isinstance(payload, dict) else []
    if not isinstance(candidates, list):
        return []
    catalog: list[dict[str, str]] = []
    for item in candidates:
        if not isinstance(item, dict):
            continue
        name = str(item.get("name", "")).strip()
        config = item.get("config")
        if not name or not isinstance(config, dict):
            continue
        capabilities = [str(value).strip() for value in item.get("capabilities", []) if str(value).strip()]
        aliases = [str(value).strip() for value in item.get("tool_aliases", []) if str(value).strip()]
        description = ", ".join([*capabilities, *aliases]) or "Approved MCP capability candidate"
        catalog.append(
            {
                "tool_name": f"mcp__{name}__<runtime-discovered>",
                "file_name": catalog_path.name,
                "summary_en": f"Approved MCP candidate for: {description}. Director may connect and register it at runtime.",
                "availability": "auto_connectable",
                "source": "director_catalog",
            }
        )
    return catalog


def discover_openharness_tool_catalog() -> list[dict[str, str]]:
    catalog: dict[str, dict[str, str]] = {}
    availability_rank = {"runtime_discovery": 1, "auto_connectable": 2, "direct": 3}
    for item in [*discover_openharness_tools(), *discover_configured_mcp_tools(), *discover_approved_mcp_tools()]:
        current = catalog.get(item["tool_name"])
        if current is None or availability_rank[item["availability"]] > availability_rank[current["availability"]]:
            catalog[item["tool_name"]] = item
    return list(catalog.values())


def _tool_summary_map(catalog: list[dict[str, str]]) -> dict[str, str]:
    return {item["tool_name"]: item["summary_en"] for item in catalog}


def build_openharness_tool_prompt_context(language: str) -> str:
    """构建可直接注入到剧本生成 prompt 中的 OpenHarness 工具能力摘要。

    参数说明：
    - language: 目标输出语言。`zh` 生成中文说明，其他值生成英文说明。

    返回值为一段面向模型的说明文字，内容包括：
    - 按能力分组的工具摘要
    - available_tools / missing_tools 的填写约束
    """

    catalog = discover_openharness_tool_catalog()
    summary_map = _tool_summary_map(catalog)
    discovered_tools = set(summary_map)
    grouped_lines: list[str] = []
    for spec in OPENHARNESS_CAPABILITY_SPECS:
        tool_summaries = [f"{tool}: {summary_map[tool]}" for tool in spec.matched_tools if tool in discovered_tools]
        if not tool_summaries:
            tool_summaries = ["当前动态清单中未发现可确认的内置工具"] if language == "zh" else ["no confirmed built-in tool in the current dynamic inventory"]
        if language == "zh":
            grouped_lines.append(f"- {spec.name}：{spec.description}；可优先考虑工具 {', '.join(tool_summaries)}")
        else:
            grouped_lines.append(f"- {spec.name}: {spec.description}; candidate tools: {', '.join(tool_summaries)}")
    if language == "zh":
        return "\n".join(
            [
                "当前演员 Harness（OpenHarness）可信工具目录由内置工具、已配置 MCP 服务和 Director 人工备案 MCP 组成。availability=direct 的内置工具可直接使用；availability=runtime_discovery 的服务需先连接发现；availability=auto_connectable 的备案 MCP 仅可由 Director 在运行时自动连接和注册。真实 MCP 工具名、参数与健康状态均以 tools/list 结果为准。以下清单可作为 available_tools / missing_tools / missing_tool_requirements 判断依据：",
                *grouped_lines,
                "已发现条目：" + ("；".join(f"{item['tool_name']} [{item['availability']}]（{item['summary_en']}）" for item in catalog) or "无"),
                "建议：available_tools 说明当前任务可能使用的直接工具或工具组合；missing_tools 仅记录当前无直接覆盖的任务能力，每项采用“动词 + 目标对象”的简洁名称。先合并同一目标的读取、解析、提取、格式转换等近义动作；不得把直接读取、离线解析工具、工具别名、MCP 状态或缺少系统调用工具拆成多个缺失条目。实现方式和运行条件应写入 resolution_strategies、available_tools、unknown_conditions 或 preconditions。只有独立缺失且各自不可替代的能力才拆分。每项能力仅保留一个 missing_tools 条目和一个 requirement；missing_tool 可使用便于说明策略的简洁名称，多个候选路径合并到 resolution_strategies，禁止功能相同或仅措辞不同的重复项。规划时必须依次：先检索 availability=direct 的工具，再组合多个直接工具；仍无法覆盖时，检索与能力描述相符的 availability=auto_connectable 或 runtime_discovery MCP，并在 resolution_strategies 中注明其为运行时候选、连接/注册前置条件、验证方式和失败降级方案；不得把候选 MCP 表述为已经可调用，也不得虚构真实 MCP 工具名。",
            ]
        )
    return "\n".join(
        [
            "The actor Harness (OpenHarness) trusted inventory below combines built-in tools, configured MCP services, and Director-approved MCP candidates. availability=direct means a direct candidate; runtime_discovery requires connection; auto_connectable means Director may connect and register the approved MCP at runtime. Actual MCP names, schemas, and health remain subject to tools/list. Use this as the basis for available_tools / missing_tools / missing_tool_requirements:",
            *grouped_lines,
            "Discovered entries: " + ("; ".join(f"{item['tool_name']} [{item['availability']}] ({item['summary_en']})" for item in catalog) or "none"),
            "Guidance: first retrieve availability=direct tools, then compose direct tools where needed. If coverage is still missing, retrieve matching auto_connectable or runtime_discovery MCP candidates and state runtime connection/registration, validation, and fallback in resolution_strategies. Do not present an MCP candidate as callable or invent an actual MCP tool name before tools/list succeeds. missing_tools must contain only directly unavailable task capabilities, each named as one concise verb-plus-target item. Merge synonymous read, parse, extract, or transform actions for the same target; do not split a capability into direct access, offline parser, tool alias, MCP state, or missing-system-tool variants. Put implementation choices and conditions in strategies or conditions, and split only independently required capabilities. Keep one missing_tools entry and one requirement per capability; a requirement may use a concise label, while alternative paths must be merged into resolution_strategies without functionally duplicate entries.",
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
    catalog = discover_openharness_tool_catalog()
    summary_map = _tool_summary_map(catalog)
    discovered_tools = set(summary_map)
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
        matched_tools = [tool for tool in spec.matched_tools if tool in discovered_tools]
        matched_tool_text = [f"{tool}｜{summary_map[tool]}" for tool in matched_tools]
        available_tools.extend(matched_tool_text)
        if not matched_tools:
            coverage = "missing"
            missing_tools.append(f"{spec.name} 缺少可确认的已发现工具")
        matched_details.append(
            {
                "required_capability": spec.name,
                "matched_tools": matched_tools,
                "coverage": coverage,
                "reason": f"从 query / 编剧输出中命中需求线索：{', '.join(hit_aliases)}",
            }
        )

    unique_available = list(dict.fromkeys(available_tools))
    actor_missing_tools: list[str] = []
    generated_requirements = []
    if isinstance(report, dict):
        difficulty_profile = report.get("difficulty_profile")
        if isinstance(difficulty_profile, dict):
            raw_missing_tools = difficulty_profile.get("missing_tools")
            if isinstance(raw_missing_tools, list):
                actor_missing_tools = [str(item).strip() for item in raw_missing_tools if str(item).strip()]
            raw_requirements = difficulty_profile.get("missing_tool_requirements")
            if isinstance(raw_requirements, list):
                generated_requirements = [item for item in raw_requirements if isinstance(item, dict)]
    for item in generated_requirements:
        missing_tool = str(item.get("missing_tool") or item.get("capability") or item.get("requirement") or "").strip()
        if missing_tool:
            item["missing_tool"] = missing_tool
            actor_missing_tools.append(missing_tool)
    unique_missing = list(dict.fromkeys([*actor_missing_tools, *missing_tools]))
    generated_missing_tools = {str(item.get("missing_tool", "")).strip() for item in generated_requirements}
    inferred_requirements = [
        {
            "missing_tool": item,
            "capability": item,
            "description": "当前动态发现的 OpenHarness 工具和运行前可确认的 MCP 能力无法保证直接覆盖该动作。",
            "required_for_steps": [],
            "resolution_strategies": [],
            "selection_rule": "优先选择运行时已验证且无需新增高风险权限的方案。",
            "unresolved_action": "在运行时发现工具、检查前置条件后仍无法覆盖时，向用户请求所需权限、依赖或 MCP 配置。",
        }
        for item in unique_missing
        if item not in generated_missing_tools
    ]
    missing_tool_requirements = [*generated_requirements, *inferred_requirements]
    confidence = "high" if matched_details else "low"
    rationale = "基于任务目标、执行剧本和演员 Harness 输出中的需求线索，与 OpenHarness 工具能力做功能相似匹配。"
    return {
        "required_capabilities": required_capabilities,
        "available_tools": unique_available,
        "missing_tools": unique_missing,
        "missing_tool_requirements": missing_tool_requirements,
        "tool_match_details": matched_details,
        "tool_match_confidence": confidence,
        "tool_match_rationale": rationale,
    }
