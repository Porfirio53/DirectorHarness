from __future__ import annotations

import asyncio
import json
import os
import runpy
import sys
from pathlib import Path

import pytest

from openharness.mcp.client import McpClientManager
from openharness.mcp.types import McpStdioServerConfig
from openharness.rehearsal.mcp_persona_runtime import (
    _external_environment_manifest,
    directory_diff_summary,
    directory_hash,
    execution_runtime_fingerprint,
    evaluate_checkpoint_rules,
    evaluate_local_trial,
    exact_copy,
    normalize_simulator_arguments,
    configure_simulator_environment,
    prepare_task_state,
    task_by_id,
)


def test_execution_runtime_fingerprint_uses_stable_source_keys() -> None:
    common = {
        "runtime": {"python": "3.11", "packages": {"mcp": "1"}},
        "file_sha256": {
            "/first/OpenHarness/scripts/mcp_persona_stdio_server.py": "server",
            "/first/OpenHarness/src/openharness/engine/query.py": "query",
            "/first/MCP-Persona/data/tasks/en_release_data.json": "tasks",
        },
        "mcp_persona": {
            "external_environment": {"status": "captured", "packages": {"x": "1"}}
        },
    }
    moved = {
        **common,
        "file_sha256": {
            name.replace("/first/", "/second/"): digest
            for name, digest in common["file_sha256"].items()
        },
        "mcp_persona": {"external_environment": {}},
    }

    first = execution_runtime_fingerprint(common)
    second = execution_runtime_fingerprint(moved)

    assert first == second
    assert first["scope"] == "shared_actor_execution_core"
    assert set(first["execution_core_source_sha256"]) == {
        "scripts/mcp_persona_stdio_server.py",
        "src/openharness/engine/query.py",
        "MCP-Persona/data/tasks/en_release_data.json",
    }


def test_optional_external_environment_has_explicit_capture_status(
    tmp_path: Path,
) -> None:
    missing = _external_environment_manifest(tmp_path / "missing-python")
    omitted = _external_environment_manifest(None)

    assert missing["status"] == "missing"
    assert missing["captured"] is False
    assert omitted == {
        "role": "optional_official_evaluator_probe",
        "status": "not_requested",
        "captured": False,
    }


def test_normalize_simulator_arguments_bridges_released_notion_shape() -> None:
    arguments = {"block_id": "abc", "format": "markdown"}
    assert normalize_simulator_arguments("notion", arguments) == {"data": arguments}
    assert normalize_simulator_arguments("lark_mcp", arguments) == arguments


def test_configure_universal_email_uses_trial_sandbox(tmp_path: Path) -> None:
    state = tmp_path / "state"
    task = {
        "sampled_contexts": {
            "universal_email": [
                {"id": "account-1", "data": {"email": "test@example.com"}}
            ]
        }
    }

    updates = configure_simulator_environment("universal_email", task, state)

    context_path = state / "universal_email.json"
    assert updates["MCP_PERSONA_STATE_DIR"] == str(state)
    assert updates["UNIVERSAL_EMAIL_CONTEXT_PATH"] == str(context_path)
    assert json.loads(updates["UNIVERSAL_EMAIL_SANDBOX_PATHS"]) == [str(context_path)]


def test_universal_email_refuses_source_fixture_writes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root_value = os.environ.get("MCP_PERSONA_ROOT")
    if not root_value:
        pytest.skip("MCP_PERSONA_ROOT is not set")
    root = Path(root_value).resolve()
    source_context = (
        root / "data" / "simulated_tools" / "universal_email" / "context.json"
    )
    handler = (
        root
        / "data"
        / "simulated_tools"
        / "universal_email"
        / "pycode"
        / "dynamic_context_handler.py"
    )
    for key in (
        "MCP_PERSONA_STATE_DIR",
        "UNIVERSAL_EMAIL_CONTEXT_PATH",
        "UNIVERSAL_EMAIL_SANDBOX_PATHS",
    ):
        monkeypatch.delenv(key, raising=False)
    source_before = source_context.read_bytes()
    namespace = runpy.run_path(str(handler))

    with pytest.raises(RuntimeError, match="refusing to modify"):
        namespace["add_sent_message"]({"subject": "must not be written"})

    assert source_context.read_bytes() == source_before


def test_prepare_task_state_and_exact_reset(tmp_path: Path) -> None:
    task = {
        "sampled_contexts": {
            "lark_mcp": [{"id": "u1", "data": {"name": "User"}}],
            "obsidian": [
                {"id": "a.md", "data": {"relative_path": "a.md", "content": "hello"}}
            ],
            "notion": [
                {
                    "id": "p1",
                    "data": {
                        "user": {"user_id": "u1"},
                        "page": {"page_id": "p1"},
                        "blocks": {"b1": {"block_id": "b1"}},
                    },
                }
            ],
        }
    }
    initial = tmp_path / "initial"
    live = tmp_path / "live"
    prepare_task_state(task, initial)
    exact_copy(initial, live)
    initial_hash = directory_hash(initial)
    shadow_source = live / "_openharness-runtime-packages" / "demo" / "tool.py"
    shadow_source.parent.mkdir(parents=True)
    shadow_source.write_text("# runtime copy\n", encoding="utf-8")
    assert directory_hash(live) == initial_hash
    (live / "lark_mcp.json").write_text("{}\n", encoding="utf-8")
    assert directory_hash(live) != initial_hash
    exact_copy(initial, live)
    assert directory_hash(live) == initial_hash
    assert (initial / "notion-comments.jsonl").is_file()


def test_local_evaluator_scores_sequence_checkpoints_and_dependencies() -> None:
    task = {
        "chains": ["demo:list", "demo:get", "demo:update"],
        "gt": [
            {"checkpoint_type": "personalized_search", "GT_value": "needle"},
            {
                "checkpoint_type": "operate",
                "tool": "demo:update",
                "operate_type": "update",
                "summary": 'Ensure the updated object id is "doc_12345678".',
            },
        ],
    }
    calls = [
        {"tool_name": "mcp__demo__list", "input": {}, "output": '{"id":"doc_12345678"}'},
        {
            "tool_name": "mcp__demo__get",
            "input": {"id": "doc_12345678"},
            "output": "needle",
        },
        {
            "tool_name": "mcp__demo__update",
            "input": {"id": "doc_12345678"},
            "output": '{"success": true}',
        },
    ]
    scores = evaluate_local_trial(
        task,
        calls,
        "done",
        state_changed=True,
        exact_reset=True,
        state_evidence='- {"id":"doc_12345678","status":"old"}\n+ {"id":"doc_12345678","status":"new"}',
    )
    assert scores.sequence_score == 1.0
    assert scores.checkpoint_score == 1.0
    assert scores.checkpoint_needs_judge_count == 0
    assert scores.execution_score == 1.0
    assert scores.dependency_link_count == 2
    assert scores.state_mutation_required is True
    assert scores.state_mutation_ok is True
    assert scores.local_pass is True


def test_local_evaluator_requires_observed_mutation_for_write_task() -> None:
    task = {
        "chains": ["demo:create"],
        "gt": [
            {"checkpoint_type": "operate", "tool": "demo:create", "operate_type": "create"}
        ],
    }
    scores = evaluate_local_trial(
        task,
        [
            {
                "tool_name": "mcp__demo__create",
                "input": {"name": "example"},
                "output": '{"success": true}',
            }
        ],
        "done",
        state_changed=False,
        exact_reset=True,
    )
    assert scores.state_mutation_required is True
    assert scores.state_mutation_ok is False
    assert scores.local_pass is False


def test_search_checkpoint_matches_structured_identifiers_across_formatting() -> None:
    task = {
        "chains": ["demo:search"],
        "gt": [
            {
                "checkpoint_type": "personalized_search",
                "GT_value": "alice的id: user_12345678; bob的id: user_87654321",
            }
        ],
    }
    details = evaluate_checkpoint_rules(
        task,
        [
            {
                "tool_name": "mcp__demo__search",
                "input": {},
                "output": '{"alice": "user_12345678", "bob": "user_87654321"}',
            }
        ],
        "",
        state_changed=False,
    )

    assert details[0]["status"] == "pass"
    assert details[0]["score"] == 1.0


def test_search_checkpoint_compares_json_as_one_structured_result() -> None:
    task = {
        "gt": [
            {
                "checkpoint_type": "personalized_search",
                "GT_value": '{"user": {"id": "user_12345678", "name": "Alice"}}',
            }
        ]
    }
    details = evaluate_checkpoint_rules(
        task,
        [
            {
                "tool_name": "mcp__demo__search",
                "input": {},
                "output": json.dumps(
                    {
                        "result": {
                            "content": [
                                {
                                    "text": json.dumps(
                                        {
                                            "items": [
                                                {
                                                    "user": {
                                                        "name": "Alice",
                                                        "id": "user_12345678",
                                                        "role": "admin",
                                                    }
                                                }
                                            ]
                                        }
                                    )
                                }
                            ]
                        }
                    }
                ),
            }
        ],
        "",
        state_changed=False,
    )

    assert details[0]["status"] == "pass"
    assert details[0]["comparison_mode"] == "structured_json_subset"


def test_search_checkpoint_does_not_merge_json_fields_across_calls() -> None:
    task = {
        "gt": [
            {
                "checkpoint_type": "personalized_search",
                "GT_value": '{"id": "user_12345678", "name": "Alice"}',
            }
        ]
    }
    details = evaluate_checkpoint_rules(
        task,
        [
            {"tool_name": "demo:first", "output": '{"id":"user_12345678"}'},
            {"tool_name": "demo:second", "output": '{"name":"Alice"}'},
        ],
        "",
        state_changed=False,
    )

    assert details[0]["status"] == "needs_judge"


def test_operate_checkpoint_fails_wrong_explicit_parameter_value() -> None:
    task = {
        "chains": ["demo:create"],
        "gt": [
            {
                "checkpoint_type": "operate",
                "tool": "demo:create",
                "operate_type": "create",
                "summary": 'Set the `title` parameter to "Quarterly review".',
            }
        ],
    }
    details = evaluate_checkpoint_rules(
        task,
        [
            {
                "tool_name": "mcp__demo__create",
                "input": {"title": "Different title"},
                "output": '{"success": true}',
            }
        ],
        "done",
        state_changed=True,
    )

    assert details[0]["status"] == "fail"
    assert details[0]["missing_expected_values"] == ["Quarterly review"]
    assert details[0]["parameter_mismatches"] == {
        "title": ["Quarterly review"]
    }


def test_operate_checkpoint_requires_state_diff_for_mutation_pass() -> None:
    task = {
        "gt": [
            {
                "checkpoint_type": "operate",
                "tool": "demo:update",
                "operate_type": "modify",
                "summary": 'Set the `status` parameter to "done".',
            }
        ]
    }
    call = {
        "tool_name": "mcp__demo__update",
        "input": {"status": "done"},
        "output": '{"success": true}',
    }

    without_diff = evaluate_checkpoint_rules(
        task,
        [call],
        "",
        state_changed=True,
    )
    with_diff = evaluate_checkpoint_rules(
        task,
        [call],
        "",
        state_changed=True,
        state_evidence='- "status":"open"\n+ "status":"done"',
    )

    assert without_diff[0]["status"] == "needs_judge"
    assert with_diff[0]["status"] == "pass"


def test_operate_checkpoint_checks_unquoted_identifier_and_timestamp() -> None:
    task = {
        "gt": [
            {
                "checkpoint_type": "operate",
                "tool": "slack:add_reaction",
                "operate_type": "other",
                "summary": "react to message 1759820964.321017",
            }
        ]
    }
    details = evaluate_checkpoint_rules(
        task,
        [
            {
                "tool_name": "mcp__slack__add_reaction",
                "input": {"timestamp": "1767251179.057311"},
                "output": '{"success": true}',
            }
        ],
        "",
        state_changed=False,
    )

    assert details[0]["status"] == "fail"
    assert "1759820964.321017" in details[0]["missing_expected_values"]


def test_directory_diff_summary_records_state_change(tmp_path: Path) -> None:
    before = tmp_path / "before"
    after = tmp_path / "after"
    before.mkdir()
    after.mkdir()
    (before / "state.json").write_text('{"title":"old"}\n', encoding="utf-8")
    (after / "state.json").write_text('{"title":"new"}\n', encoding="utf-8")

    summary = directory_diff_summary(before, after)

    assert "before/state.json" in summary
    assert 'title":"old' in summary
    assert 'title":"new' in summary


def test_local_evaluator_does_not_count_duplicate_tool_as_missing_tools() -> None:
    task = {
        "chains": ["demo:list", "demo:get", "demo:update"],
        "gt": [],
    }
    calls = [
        {
            "tool_name": "mcp__demo__update",
            "input": {"id": str(index)},
            "output": '{"success": true}',
        }
        for index in range(3)
    ]

    scores = evaluate_local_trial(
        task,
        calls,
        "done",
        state_changed=True,
        exact_reset=True,
    )

    assert scores.execution_score == pytest.approx(1 / 3)
    assert scores.local_pass is False


@pytest.mark.asyncio
async def test_real_release_stdio_server_discovery_and_tool_call(tmp_path: Path) -> None:
    root_value = os.environ.get("MCP_PERSONA_ROOT")
    if not root_value:
        pytest.skip("MCP_PERSONA_ROOT is not set")
    root = Path(root_value).resolve()
    task = task_by_id(root, 53)
    state = tmp_path / "state"
    prepare_task_state(task, state)
    repository = Path(__file__).resolve().parents[2]
    config = McpStdioServerConfig(
        command=sys.executable,
        args=[
            str(repository / "scripts" / "mcp_persona_stdio_server.py"),
            "--mcp-persona-root",
            str(root),
            "--task-id",
            "53",
            "--server",
            "obsidian",
            "--state-dir",
            str(state),
            "--tool-scope",
            "task",
        ],
        cwd=str(repository),
    )
    manager = McpClientManager({"obsidian": config})
    try:
        await manager.connect_all()
        statuses = manager.list_statuses()
        assert statuses[0].state == "connected", statuses[0].detail
        tools = {tool.name: tool for tool in manager.list_tools()}
        assert set(tools) == {
            "obsidian_list_files_in_vault",
            "obsidian_get_file_contents",
            "obsidian_batch_get_file_contents",
        }
        output = await manager.call_tool("obsidian", "obsidian_list_files_in_vault", {})
        payload = json.loads(output)
        assert payload["success"] is True
        assert "2023-07-18.md" in output
        failed_output = await manager.call_tool(
            "obsidian",
            "obsidian_get_file_contents",
            {"filepath": "missing.md"},
        )
        assert json.loads(failed_output)["success"] is False
        trace_lines = (state / "_openharness-obsidian-calls.jsonl").read_text(
            encoding="utf-8"
        ).splitlines()
        assert len(trace_lines) == 2
        assert json.loads(trace_lines[-1])["is_error"] is True
    finally:
        await manager.close()


@pytest.mark.asyncio
async def test_universal_email_writes_only_trial_state(tmp_path: Path) -> None:
    root_value = os.environ.get("MCP_PERSONA_ROOT")
    if not root_value:
        pytest.skip("MCP_PERSONA_ROOT is not set")
    root = Path(root_value).resolve()
    task = task_by_id(root, 86)
    initial = tmp_path / "initial"
    state = tmp_path / "live"
    prepare_task_state(task, initial)
    exact_copy(initial, state)
    initial_hash = directory_hash(initial)
    source_context = (
        root / "data" / "simulated_tools" / "universal_email" / "context.json"
    )
    source_before = source_context.read_bytes()
    repository = Path(__file__).resolve().parents[2]
    config = McpStdioServerConfig(
        command=sys.executable,
        args=[
            str(repository / "scripts" / "mcp_persona_stdio_server.py"),
            "--mcp-persona-root",
            str(root),
            "--task-id",
            "86",
            "--server",
            "universal_email",
            "--state-dir",
            str(state),
            "--tool-scope",
            "task",
        ],
        cwd=str(repository),
    )
    manager = McpClientManager({"universal_email": config})
    try:
        await manager.connect_all()
        assert manager.list_statuses()[0].state == "connected"
        setup_output = await manager.call_tool(
            "universal_email",
            "setup_email_account",
            {
                "email": "new.user@outlook.com",
                "password": "synthetic-password",
                "provider": "outlook",
            },
        )
        assert json.loads(setup_output)["success"] is True
        send_output = await manager.call_tool(
            "universal_email",
            "send_email",
            {
                "to": ["team.ops@exmail.qq.com"],
                "subject": "Sandbox write test",
                "text": "This is a synthetic sandbox write test.",
            },
        )
        assert json.loads(send_output)["success"] is True
    finally:
        await manager.close()

    assert source_context.read_bytes() == source_before
    sandbox_context = json.loads((state / "universal_email.json").read_text())
    assert any(
        account.get("email") == "new.user@outlook.com"
        for account in sandbox_context["accounts"]
    )
    assert sandbox_context["sent_messages"]
    assert directory_hash(state) != initial_hash
    exact_copy(initial, state)
    assert directory_hash(state) == initial_hash


@pytest.mark.asyncio
async def test_cross_server_close_does_not_cancel_runner_task(tmp_path: Path) -> None:
    root_value = os.environ.get("MCP_PERSONA_ROOT")
    if not root_value:
        pytest.skip("MCP_PERSONA_ROOT is not set")
    root = Path(root_value).resolve()
    task = task_by_id(root, 131)
    state = tmp_path / "state"
    prepare_task_state(task, state)
    repository = Path(__file__).resolve().parents[2]
    configs = {
        server: McpStdioServerConfig(
            command=sys.executable,
            args=[
                str(repository / "scripts" / "mcp_persona_stdio_server.py"),
                "--mcp-persona-root",
                str(root),
                "--task-id",
                "131",
                "--server",
                server,
                "--state-dir",
                str(state),
                "--tool-scope",
                "task",
            ],
            cwd=str(repository),
        )
        for server in ("notion", "xiaohongshu")
    }
    manager = McpClientManager(configs)

    await manager.connect_all()
    assert all(status.state == "connected" for status in manager.list_statuses())
    await manager.close()
    await asyncio.sleep(0)
