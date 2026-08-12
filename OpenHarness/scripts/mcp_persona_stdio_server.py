#!/usr/bin/env python3
"""Expose released MCP-Persona simulator functions as a stdio MCP server."""

from __future__ import annotations

import argparse
import asyncio
import json
import shutil
import sys
import time
import types as python_types
from pathlib import Path
from typing import Any

from mcp import types
from mcp.server import Server
from mcp.server.stdio import stdio_server

from openharness.rehearsal.mcp_persona_runtime import (
    configure_simulator_environment,
    load_simulator,
    normalize_simulator_arguments,
    released_tool_schemas,
    simulator_source,
    task_by_id,
)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--mcp-persona-root", type=Path, required=True)
    parser.add_argument("--task-id", type=int, required=True)
    parser.add_argument("--language", choices=("en", "zh"), default="en")
    parser.add_argument("--server", required=True)
    parser.add_argument("--state-dir", type=Path, required=True)
    parser.add_argument("--tool-scope", choices=("task", "server"), default="server")
    return parser.parse_args()


def shadow_fixed_state_package(root: Path, server: str, state_dir: Path) -> Path | None:
    """Redirect simulators with package-relative state into the trial sandbox."""

    if server not in {"instagram", "reddit"}:
        return None
    original = root / "data" / "simulated_tools" / server
    shadow = state_dir / "_openharness-runtime-packages" / server
    if shadow.exists():
        shutil.rmtree(shadow)
    shutil.copytree(original, shadow)
    bundled_state = shadow / ("context.json" if server == "instagram" else "reddit.json")
    bundled_state.unlink(missing_ok=True)
    bundled_state.symlink_to((state_dir / f"{server}.json").resolve())
    return shadow


async def run_server(args: argparse.Namespace) -> None:
    root = args.mcp_persona_root.resolve()
    task = task_by_id(root, args.task_id, args.language)
    state_dir = args.state_dir.resolve()
    configure_simulator_environment(args.server, task, state_dir)
    shadow = shadow_fixed_state_package(root, args.server, state_dir)
    package_root = (
        shadow.parent if shadow is not None else root / "data" / "simulated_tools"
    )
    sys.path.insert(0, str(package_root))
    # Some environments already contain the unrelated PyPI ``slack`` package.
    # The released simulators use ``slack.pycode`` as a namespace package, so
    # explicitly bind the task server package to the released source tree.
    package = python_types.ModuleType(args.server)
    package.__path__ = [  # type: ignore[attr-defined]
        str(shadow if shadow is not None else root / "data" / "simulated_tools" / args.server)
    ]
    sys.modules[args.server] = package

    schemas = released_tool_schemas(root, args.server)
    if args.tool_scope == "task":
        allowed = {
            str(name).split(":", 1)[1]
            for name in task.get("chains", [])
            if str(name).startswith(f"{args.server}:")
        }
        schemas = {name: value for name, value in schemas.items() if name in allowed}

    available: dict[str, tuple[dict[str, Any], Any]] = {}
    for name, value in schemas.items():
        try:
            source = simulator_source(root, args.server, name)
        except FileNotFoundError:
            continue
        if shadow is not None:
            source = shadow / "pycode" / source.name
        available[name] = (value, load_simulator(source))

    app = Server(f"mcp-persona-{args.server}", version="local-compat-1")

    @app.list_tools()
    async def list_tools() -> list[types.Tool]:
        return [
            types.Tool(
                name=name,
                description=str(value.get("description", "")),
                inputSchema=dict(value.get("input_schema", {"type": "object"})),
            )
            for name, (value, _function) in sorted(available.items())
        ]

    @app.call_tool(validate_input=True)
    async def call_tool(name: str, arguments: dict[str, Any]) -> types.CallToolResult:
        entry = available.get(name)
        if entry is None:
            return types.CallToolResult(
                content=[types.TextContent(type="text", text=f"Unknown tool: {name}")],
                isError=True,
            )
        _schema, function = entry
        normalized = normalize_simulator_arguments(args.server, arguments)
        try:
            result = function(normalized)
        except Exception as exc:
            payload = {"success": False, "error": f"{type(exc).__name__}: {exc}", "result": None}
        else:
            payload = result if isinstance(result, dict) else {"success": True, "result": result}
        is_error = payload.get("success") is False
        trace_path = args.state_dir / f"_openharness-{args.server}-calls.jsonl"
        with trace_path.open("a", encoding="utf-8") as stream:
            stream.write(
                json.dumps(
                    {
                        "timestamp_ns": time.time_ns(),
                        "server": args.server,
                        "tool_name": name,
                        "input": arguments,
                        "simulator_input": normalized,
                        "output": payload,
                        "is_error": is_error,
                    },
                    ensure_ascii=False,
                    default=str,
                )
                + "\n"
            )
        return types.CallToolResult(
            content=[
                types.TextContent(
                    type="text",
                    text=json.dumps(payload, ensure_ascii=False, default=str),
                )
            ],
            isError=is_error,
        )

    async with stdio_server() as (read_stream, write_stream):
        await app.run(
            read_stream,
            write_stream,
            app.create_initialization_options(),
            raise_exceptions=True,
        )


def main() -> int:
    asyncio.run(run_server(parse_args()))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
