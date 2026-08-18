from __future__ import annotations

import json

from writer_harness.capability_matching import (
    build_openharness_tool_prompt_context,
    discover_approved_mcp_tools,
    discover_openharness_tool_catalog,
    match_openharness_capabilities,
)


def test_dynamic_catalog_includes_builtin_tools_and_configured_mcp(tmp_path, monkeypatch) -> None:
    config_dir = tmp_path / "openharness"
    config_dir.mkdir()
    (config_dir / "settings.json").write_text(
        json.dumps({"mcp_servers": {"reference": {"type": "http", "url": "https://example.test/mcp"}}}),
        encoding="utf-8",
    )
    monkeypatch.setenv("OPENHARNESS_CONFIG_DIR", str(config_dir))

    catalog = discover_openharness_tool_catalog()

    assert any(item["tool_name"] == "read_file" for item in catalog)
    assert any(item["tool_name"] == "mcp__reference__<runtime-discovered>" for item in catalog)
    assert "mcp__reference__<runtime-discovered>" in build_openharness_tool_prompt_context("en")


def test_dynamic_catalog_includes_director_approved_mcp_as_auto_connectable(tmp_path, monkeypatch) -> None:
    catalog_path = tmp_path / "director_mcp_catalog.json"
    catalog_path.write_text(
        json.dumps(
            {
                "mcp_candidates": [
                    {
                        "name": "document-reader",
                        "capabilities": ["document", "extract_text"],
                        "tool_aliases": ["read_document"],
                        "config": {"type": "stdio", "command": "python", "args": ["-m", "document_mcp"]},
                    }
                ]
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv("DIRECTOR_MCP_CATALOG", str(catalog_path))

    approved = discover_approved_mcp_tools()
    catalog = discover_openharness_tool_catalog()
    context = build_openharness_tool_prompt_context("zh")

    assert approved[0]["availability"] == "auto_connectable"
    assert any(
        item["tool_name"] == "mcp__document-reader__<runtime-discovered>"
        and item["availability"] == "auto_connectable"
        for item in catalog
    )
    assert "auto_connectable" in context
    assert "不得把候选 MCP 表述为已经可调用" in context


def test_approved_catalog_takes_precedence_over_configured_runtime_status(tmp_path, monkeypatch) -> None:
    config_dir = tmp_path / "openharness"
    config_dir.mkdir()
    (config_dir / "settings.json").write_text(
        json.dumps({"mcp_servers": {"pdf-reader": {"type": "stdio", "command": "npx"}}}),
        encoding="utf-8",
    )
    catalog_path = tmp_path / "director_mcp_catalog.json"
    catalog_path.write_text(
        json.dumps(
            {
                "mcp_candidates": [
                    {
                        "name": "pdf-reader",
                        "capabilities": ["pdf"],
                        "tool_aliases": ["read_pdf"],
                        "config": {"type": "stdio", "command": "npx"},
                    }
                ]
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv("OPENHARNESS_CONFIG_DIR", str(config_dir))
    monkeypatch.setenv("DIRECTOR_MCP_CATALOG", str(catalog_path))

    catalog = discover_openharness_tool_catalog()

    entry = next(item for item in catalog if item["tool_name"] == "mcp__pdf-reader__<runtime-discovered>")
    assert entry["availability"] == "auto_connectable"


def test_capability_matching_returns_structured_missing_tool_requirements() -> None:
    result = match_openharness_capabilities("log in to a website and submit a form", None, "")

    assert result["missing_tool_requirements"]
    requirement = result["missing_tool_requirements"][0]
    assert requirement["missing_tool"] in result["missing_tools"]
    assert requirement["capability"]
    assert "resolution_strategies" in requirement
    assert any(detail["required_capability"] == "浏览器交互" for detail in result["tool_match_details"])


def test_capability_matching_preserves_actor_composition_plan() -> None:
    report = {
        "difficulty_profile": {
            "missing_tool_requirements": [
                {
                    "capability": "PDF 内容提取",
                    "missing_tool": "PDF 内容提取",
                    "description": "没有专用 PDF 工具",
                    "required_for_steps": ["检查解析依赖", "运行提取脚本"],
                    "resolution_strategies": [{"strategy_type": "tool_composition", "tool_chain": ["bash", "write_file", "bash"]}],
                    "selection_rule": "优先使用本地工具组合",
                    "unresolved_action": "请求用户授权",
                }
            ]
        }
    }

    result = match_openharness_capabilities("提取 PDF 内容", report, "")

    assert result["missing_tool_requirements"][0]["capability"] == "PDF 内容提取"
    assert "PDF 内容提取" in result["missing_tools"]
    assert result["missing_tool_requirements"][0]["resolution_strategies"][0]["tool_chain"] == ["bash", "write_file", "bash"]


def test_script_prompt_and_completeness_check_include_missing_tool_requirements() -> None:
    from writer_harness.prompts import get_generated_scripts_template
    from writer_harness.writer_harness import WriterHarness

    assert "missing_tool_requirements" in get_generated_scripts_template("en")
    assert "resolution_strategies" in get_generated_scripts_template("en")
    assert "auto_connectable" in get_generated_scripts_template("en")
    assert "compose existing tools" in get_generated_scripts_template("en")
    assert "one missing_tools entry and one requirement per capability" in get_generated_scripts_template("en")
    assert "verb + target" in get_generated_scripts_template("en")
    assert "concrete execution action" in get_generated_scripts_template("en")
    assert "missing_tool_requirements generated in the same JSON" in get_generated_scripts_template("en")
    judgment = WriterHarness(None).judge_online_completeness('{"missing_tool_requirements": []}')
    assert "missing_tool_requirements" in judgment.matched_checks


def test_completeness_check_requires_plan_tool_linkage() -> None:
    from writer_harness.writer_harness import WriterHarness

    report = {
        "task_profile": {"task_goal": "读取文件", "expected_output": "文本", "success_criteria": ["输出文本"]},
        "difficulty_profile": {
            "available_tools": ["read_file"],
            "missing_tools": ["PDF 内容提取"],
            "missing_tool_requirements": [{"missing_tool": "PDF 内容提取", "required_for_steps": ["PDF 内容提取：运行解析脚本"]}],
            "known_conditions": [],
            "unknown_conditions": [],
        },
        "execution_plan": {
            "recommended_steps": ["read_file：读取目标文件", "PDF 内容提取：运行解析脚本"],
            "validation_steps": ["检查输出文本非空"],
        },
        "difficulty_judgment": "medium",
        "judgment_rationale": ["存在能力缺口"],
        "execution_suggestion": "cautious_execute",
    }

    judgment = WriterHarness(None).judge_online_completeness(json.dumps(report, ensure_ascii=False))

    assert "execution_plan_tool_linkage" in judgment.matched_checks
    assert "missing_tool_step_linkage" in judgment.matched_checks
