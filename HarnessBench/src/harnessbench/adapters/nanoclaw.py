from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path
from typing import Any

from harnessbench.adapters.base import BaseAdapter
from harnessbench.models import AdapterRunContext, AdapterRunResult
from harnessbench.usage_proxy import register_routes


def _resolve_project_relative(raw: str | Path, root: Path) -> Path:
    p = Path(os.path.expanduser(str(raw)))
    if not p.is_absolute():
        p = root / p
    return p.resolve()


def _project_root() -> Path:
    return Path(__file__).resolve().parents[3]


def _parse_nanoclaw_stdout(stdout_text: str) -> dict[str, Any] | None:
    text = (stdout_text or "").strip()
    if not text:
        return None

    start_marker = "---NANOCLAW_OUTPUT_START---"
    end_marker = "---NANOCLAW_OUTPUT_END---"
    if start_marker in text and end_marker in text:
        start_idx = text.index(start_marker) + len(start_marker)
        end_idx = text.index(end_marker)
        payload = text[start_idx:end_idx].strip()
        try:
            return json.loads(payload)
        except json.JSONDecodeError:
            return None

    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass

    for line in reversed([line.strip() for line in text.splitlines() if line.strip()]):
        if line.startswith("{") and line.endswith("}"):
            try:
                return json.loads(line)
            except json.JSONDecodeError:
                continue
    return None

def _register_proxy_routes(proxy_base_url: str, proxy_routes_file: Path, env: dict[str, str]) -> None:
    """
    Register proxy routes for NanoClaw's API calls.
    Extracts upstream API URLs from environment variables and registers them with the proxy.
    """
    routes: dict[str, dict[str, str]] = {}

    # Check for ANTHROPIC_BASE_URL (Anthropic API)
    anthropic_base = env.get("ANTHROPIC_BASE_URL", "").strip()
    if anthropic_base and not anthropic_base.startswith("http://127.0.0.1"):
        prefix = "/nanoclaw/anthropic"
        routes[prefix] = {
            "framework": "nanoclaw",
            "provider": "anthropic",
            "upstream": anthropic_base.rstrip("/"),
        }
        # Replace with proxy URL - convert 127.0.0.1 to host.docker.internal for Docker
        #proxy_url = proxy_base_url.replace("127.0.0.1", "host.docker.internal")
        proxy_url = proxy_base_url
        env["ANTHROPIC_BASE_URL"] = f"{proxy_url}{prefix}"

    # Check for OPENAI_BASE_URL (OpenAI API)
    openai_base = env.get("OPENAI_BASE_URL", "").strip()
    if openai_base and not openai_base.startswith("http://127.0.0.1"):
        prefix = "/nanoclaw/openai"
        routes[prefix] = {
            "framework": "nanoclaw",
            "provider": "openai",
            "upstream": openai_base.rstrip("/"),
        }
        proxy_url = proxy_base_url
        env["OPENAI_BASE_URL"] = f"{proxy_url}{prefix}"

    # If no explicit base URLs, register default APIs
    if not routes:
        # Default Anthropic API
        if env.get("ANTHROPIC_API_KEY") or env.get("ANTHROPIC_AUTH_TOKEN"):
            prefix = "/nanoclaw/anthropic"
            routes[prefix] = {
                "framework": "nanoclaw",
                "provider": "anthropic",
                "upstream": "https://api.anthropic.com",
            }
            proxy_url = proxy_base_url
            env["ANTHROPIC_BASE_URL"] = f"{proxy_url}{prefix}"

        # Default OpenAI API
        if env.get("OPENAI_API_KEY"):
            prefix = "/nanoclaw/openai"
            routes[prefix] = {
                "framework": "nanoclaw",
                "provider": "openai",
                "upstream": "https://api.openai.com/v1",
            }
            proxy_url = proxy_base_url.replace("127.0.0.1", "host.docker.internal")
            env["OPENAI_BASE_URL"] = f"{proxy_url}{prefix}"

    # Register all routes
    if routes:
        register_routes(proxy_routes_file, routes)


import os
import subprocess
import sys
import threading
from pathlib import Path
from typing import Any

class NanoClawAdapter(BaseAdapter):
    name = "nanoclaw"

    def run(self, ctx: AdapterRunContext) -> AdapterRunResult:
        command = str(ctx.model_config.get("command") or "nanoclaw")
        args = list(ctx.model_config.get("args") or ["agent"])
        cmd = [command, *[str(x) for x in args], "--message", ctx.prompt]

        env = os.environ.copy()
        env.update(ctx.env)
        workspace_mount_path = str(ctx.model_config.get("workspace_mount_path") or "/workspace")
        env["HARNESSBENCH_TASK_ID"] = ctx.task.task_id
        env["HARNESSBENCH_WORKSPACE"] = str(ctx.workspace)
        env["HARNESSBENCH_SANDBOX"] = str(ctx.sandbox)
        env["HARNESSBENCH_SESSION_ID"] = ctx.session_id
        env["HARNESSBENCH_PROMPT_FILE"] = str(ctx.prompt_file)
        env["NANOCLAW_WORKSPACE_DIR"] = str(ctx.workspace)
        env["NANOCLAW_WORKSPACE_MOUNT_PATH"] = workspace_mount_path

        # Register proxy routes if proxy is enabled
        proxy_base_url = str(ctx.env.get("HARNESSBENCH_LLM_PROXY_URL") or "").strip()
        proxy_routes_file = ctx.env.get("HARNESSBENCH_LLM_PROXY_ROUTES")
        if proxy_base_url and proxy_routes_file:
            _register_proxy_routes(proxy_base_url, Path(proxy_routes_file), env)

        workdir_raw = ctx.model_config.get("workdir")
        cwd = str(_resolve_project_relative(workdir_raw, _project_root())) if workdir_raw else str(ctx.workspace)
        proc = subprocess.Popen(
            cmd,
            cwd=cwd,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=env,
            bufsize=1,  # line buffered
        )

        stdout_chunks: list[str] = []
        stderr_chunks: list[str] = []

        def _reader(pipe, sink: list[str], out_stream) -> None:
            try:
                assert pipe is not None
                for line in iter(pipe.readline, ""):
                    sink.append(line)
                    out_stream.write(line)
                    out_stream.flush()
            finally:
                if pipe is not None:
                    pipe.close()

        stdout_thread = threading.Thread(
            target=_reader,
            args=(proc.stdout, stdout_chunks, sys.stdout),
            daemon=True,
        )
        stderr_thread = threading.Thread(
            target=_reader,
            args=(proc.stderr, stderr_chunks, sys.stderr),
            daemon=True,
        )

        stdout_thread.start()
        stderr_thread.start()

        try:
            returncode = proc.wait(timeout=ctx.timeout_sec)
        except subprocess.TimeoutExpired:
            proc.kill()
            returncode = proc.wait()
            stdout_thread.join(timeout=1)
            stderr_thread.join(timeout=1)

            stdout_text = "".join(stdout_chunks)
            stderr_text = "".join(stderr_chunks)

            metadata: dict[str, Any] = {
                "returncode": returncode,
                "workspace": str(ctx.workspace),
                "cwd": cwd,
                "workspace_mount_path": workspace_mount_path,
                "timed_out": True,
            }

            payload = _parse_nanoclaw_stdout(stdout_text)
            if payload is not None:
                metadata["nanoclaw_raw"] = payload

            # Rebuild session.jsonl from proxy responses even on timeout
            proxy_responses_dir = ctx.sandbox / "usage-proxy" / "responses"

            return AdapterRunResult(
                ok=False,
                command=cmd,
                stdout=stdout_text,
                stderr=stderr_text,
                metadata=metadata,
            )

        stdout_thread.join()
        stderr_thread.join()

        stdout_text = "".join(stdout_chunks)
        stderr_text = "".join(stderr_chunks)

        payload = _parse_nanoclaw_stdout(stdout_text)
        metadata: dict[str, Any] = {
            "returncode": returncode,
            "workspace": str(ctx.workspace),
            "cwd": cwd,
            "workspace_mount_path": workspace_mount_path,
        }
        if payload is not None:
            metadata["nanoclaw_raw"] = payload

        # Rebuild session.jsonl from proxy responses for process grading
        proxy_responses_dir = ctx.sandbox / "usage-proxy" / "responses"

        return AdapterRunResult(
            ok=returncode == 0,
            command=cmd,
            stdout=stdout_text,
            stderr=stderr_text,
            metadata=metadata,
        )
