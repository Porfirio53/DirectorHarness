from __future__ import annotations

import os
import json
import shutil
import subprocess
import sys
import threading
from pathlib import Path
from typing import Any, TextIO

from harnessbench.adapters.base import BaseAdapter
from harnessbench.models import AdapterRunContext, AdapterRunResult


def _project_root() -> Path:
    return Path(__file__).resolve().parents[3]


def _resolve_project_path(raw: str | Path) -> Path:
    p = Path(os.path.expanduser(str(raw)))
    if not p.is_absolute():
        p = _project_root() / p
    return p.resolve()


def _copy_if_exists(src: Path, dst: Path) -> None:
    if not src.exists():
        return
    dst.parent.mkdir(parents=True, exist_ok=True)
    if src.is_dir():
        shutil.copytree(src, dst, dirs_exist_ok=True)
    else:
        shutil.copy2(src, dst)


def _sync_minimal_codex_state(source_config: Path, target_home: Path) -> None:
    source_home = source_config.resolve().parent
    _copy_if_exists(source_config, target_home / "config.toml")
    for name in ("auth.json", "credentials.json", "organization.json"):
        _copy_if_exists(source_home / name, target_home / name)
    _copy_if_exists(source_home / "mcp", target_home / "mcp")


def _toml_string(value: str) -> str:
    return json.dumps(value, ensure_ascii=False)


def _set_top_level_toml_string(path: Path, key: str, value: str) -> None:
    if not path.is_file() or not value:
        return
    lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    target_prefix = f"{key} ="
    section_index = next((i for i, line in enumerate(lines) if line.lstrip().startswith("[")), len(lines))
    replacement = f"{key} = {_toml_string(value)}"
    for i, line in enumerate(lines[:section_index]):
        stripped = line.strip()
        if stripped.startswith(target_prefix):
            lines[i] = replacement
            path.write_text("\n".join(lines) + "\n", encoding="utf-8")
            return
    lines.insert(section_index, replacement)
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def _reasoning_effort_from_overrides(items: Any) -> str:
    for item in items or []:
        text = str(item).strip()
        if not text.startswith("model_reasoning_effort="):
            continue
        raw = text.split("=", 1)[1].strip()
        try:
            parsed = json.loads(raw)
        except json.JSONDecodeError:
            parsed = raw.strip('"')
        return str(parsed).strip()
    return ""


def _latest_codex_session_file(codex_home: Path) -> Path | None:
    sessions_dir = codex_home / "sessions"
    if not sessions_dir.is_dir():
        return None
    files = sorted(
        sessions_dir.rglob("*.jsonl"),
        key=lambda p: p.stat().st_mtime,
        reverse=True,
    )
    return files[0] if files else None


def _codex_content_to_text(content: Any) -> str:
    if content is None:
        return ""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: list[str] = []
        for item in content:
            if isinstance(item, dict):
                text = item.get("text")
                if isinstance(text, str):
                    parts.append(text)
            elif isinstance(item, str):
                parts.append(item)
        return "\n".join(parts)
    return str(content)


def _parse_arguments(raw: Any) -> Any:
    if isinstance(raw, str):
        try:
            return json.loads(raw)
        except json.JSONDecodeError:
            return raw
    return raw


def _synthetic_tool_calls(tool_calls: list[dict[str, Any]]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for item in tool_calls:
        args = item.get("arguments", "")
        if not isinstance(args, str):
            args = json.dumps(args, ensure_ascii=False)
        out.append(
            {
                "id": item.get("id") or item.get("call_id") or "",
                "type": "function",
                "function": {
                    "name": str(item.get("name") or ""),
                    "arguments": args,
                },
            }
        )
    return out


def _write_codex_session_as_proxy_trace(
    *,
    session_file: Path,
    proxy_dir: Path,
    task_id: str,
    session_id: str,
    model_id: str,
    response_model: str,
) -> dict[str, Any]:
    responses_dir = proxy_dir / "responses"
    responses_dir.mkdir(parents=True, exist_ok=True)
    requests_log = proxy_dir / "requests.jsonl"

    conversation: list[dict[str, str]] = []
    request_snapshot: list[dict[str, str]] | None = None
    assistant_parts: list[str] = []
    tool_calls: list[dict[str, Any]] = []
    response_count = 0
    last_usage: dict[str, int] = {
        "input_tokens": 0,
        "output_tokens": 0,
        "cache_read_tokens": 0,
        "cache_write_tokens": 0,
        "total_tokens": 0,
    }
    usage_rows: list[str] = []

    def start_assistant_group() -> None:
        nonlocal request_snapshot
        if request_snapshot is None:
            request_snapshot = [dict(m) for m in conversation]

    def flush_assistant_group() -> None:
        nonlocal request_snapshot, assistant_parts, tool_calls, response_count
        if request_snapshot is None:
            return
        assistant_text = "\n".join(part for part in assistant_parts if part).strip()
        if not assistant_text and not tool_calls:
            request_snapshot = None
            assistant_parts = []
            tool_calls = []
            return

        response_count += 1
        response_path = responses_dir / f"codex-{response_count:04d}.json"
        synthetic_tcs = _synthetic_tool_calls(tool_calls)
        raw_record = {
            "task_id": task_id,
            "session_id": session_id,
            "model_id": model_id,
            "framework": "codex",
            "provider": "codex-session",
            "request_body": json.dumps({"messages": request_snapshot}, ensure_ascii=False),
            "response_json": {
                "model": response_model,
                "choices": [
                    {
                        "message": {
                            "role": "assistant",
                            "content": assistant_text,
                            "tool_calls": synthetic_tcs,
                        }
                    }
                ],
            },
            "source_session_file": str(session_file),
        }
        response_path.write_text(json.dumps(raw_record, ensure_ascii=False, indent=2), encoding="utf-8")
        usage_row = {
            "task_id": task_id,
            "session_id": session_id,
            "model_id": model_id,
            "framework": "codex",
            "provider": "codex-session",
            "raw_response_file": str(response_path),
            "input_tokens": last_usage["input_tokens"],
            "output_tokens": last_usage["output_tokens"],
            "cache_read_tokens": last_usage["cache_read_tokens"],
            "cache_write_tokens": last_usage["cache_write_tokens"],
            "total_tokens": last_usage["total_tokens"],
            "response_model": response_model,
        }
        usage_rows.append(json.dumps(usage_row, ensure_ascii=False))

        conversation.append({"role": "assistant", "content": assistant_text})
        request_snapshot = None
        assistant_parts = []
        tool_calls = []

    for line in session_file.read_text(encoding="utf-8", errors="replace").splitlines():
        if not line.strip():
            continue
        try:
            row = json.loads(line)
        except json.JSONDecodeError:
            continue
        payload = row.get("payload")
        if not isinstance(payload, dict):
            continue
        payload_type = str(payload.get("type") or "")

        if row.get("type") == "turn_context":
            model = str(payload.get("model") or "").strip()
            if model:
                response_model = model
            continue

        if row.get("type") == "event_msg" and payload_type == "token_count":
            info = payload.get("info")
            if isinstance(info, dict):
                usage = info.get("last_token_usage") or info.get("total_token_usage") or {}
                if isinstance(usage, dict):
                    last_usage = {
                        "input_tokens": int(usage.get("input_tokens", 0) or 0),
                        "output_tokens": int(usage.get("output_tokens", 0) or 0),
                        "cache_read_tokens": int(usage.get("cached_input_tokens", 0) or 0),
                        "cache_write_tokens": 0,
                        "total_tokens": int(usage.get("total_tokens", 0) or 0),
                    }
            continue

        if row.get("type") != "response_item":
            continue

        if payload_type == "message":
            role = str(payload.get("role") or "")
            content = _codex_content_to_text(payload.get("content"))
            if role == "assistant":
                start_assistant_group()
                if content:
                    assistant_parts.append(content)
            elif role == "user":
                flush_assistant_group()
                if content and (not conversation or conversation[-1] != {"role": "user", "content": content}):
                    conversation.append({"role": "user", "content": content})
            continue

        if payload_type == "function_call":
            start_assistant_group()
            raw_args = payload.get("arguments")
            tool_calls.append(
                {
                    "call_id": str(payload.get("call_id") or ""),
                    "name": str(payload.get("name") or ""),
                    "arguments": _parse_arguments(raw_args),
                }
            )
            continue

        if payload_type == "function_call_output":
            flush_assistant_group()
            output = _codex_content_to_text(payload.get("output"))
            if output:
                item = {"role": "tool", "content": output}
                call_id = str(payload.get("call_id") or "")
                if call_id:
                    item["tool_call_id"] = call_id
                conversation.append(item)

    flush_assistant_group()
    if usage_rows:
        requests_log.write_text("\n".join(usage_rows) + "\n", encoding="utf-8")

    return {
        "session_file": str(session_file),
        "proxy_dir": str(proxy_dir),
        "response_count": response_count,
        "requests_log": str(requests_log),
    }


def _write_codex_exec_json_as_proxy_trace(
    *,
    stdout_text: str,
    stdout_log_file: Path,
    proxy_dir: Path,
    task_id: str,
    session_id: str,
    model_id: str,
    response_model: str,
    initial_prompt: str,
) -> dict[str, Any]:
    responses_dir = proxy_dir / "responses"
    responses_dir.mkdir(parents=True, exist_ok=True)
    requests_log = proxy_dir / "requests.jsonl"

    conversation: list[dict[str, str]] = [{"role": "user", "content": initial_prompt}]
    response_count = 0
    usage_rows: list[str] = []
    last_usage: dict[str, int] = {
        "input_tokens": 0,
        "output_tokens": 0,
        "cache_read_tokens": 0,
        "cache_write_tokens": 0,
        "total_tokens": 0,
    }

    def write_response(*, assistant_text: str = "", tool_calls: list[dict[str, Any]] | None = None) -> None:
        nonlocal response_count
        tool_calls = tool_calls or []
        if not assistant_text and not tool_calls:
            return
        response_count += 1
        response_path = responses_dir / f"codex-stdout-{response_count:04d}.json"
        request_snapshot = [dict(m) for m in conversation]
        raw_record = {
            "task_id": task_id,
            "session_id": session_id,
            "model_id": model_id,
            "framework": "codex",
            "provider": "codex-exec-json",
            "request_body": json.dumps({"messages": request_snapshot}, ensure_ascii=False),
            "response_json": {
                "model": response_model,
                "choices": [
                    {
                        "message": {
                            "role": "assistant",
                            "content": assistant_text,
                            "tool_calls": _synthetic_tool_calls(tool_calls),
                        }
                    }
                ],
            },
            "source_stdout_log_file": str(stdout_log_file),
        }
        response_path.write_text(json.dumps(raw_record, ensure_ascii=False, indent=2), encoding="utf-8")
        usage_row = {
            "task_id": task_id,
            "session_id": session_id,
            "model_id": model_id,
            "framework": "codex",
            "provider": "codex-exec-json",
            "raw_response_file": str(response_path),
            "input_tokens": last_usage["input_tokens"],
            "output_tokens": last_usage["output_tokens"],
            "cache_read_tokens": last_usage["cache_read_tokens"],
            "cache_write_tokens": last_usage["cache_write_tokens"],
            "total_tokens": last_usage["total_tokens"],
            "response_model": response_model,
        }
        usage_rows.append(json.dumps(usage_row, ensure_ascii=False))
        conversation.append({"role": "assistant", "content": assistant_text})

    for line in stdout_text.splitlines():
        if not line.strip():
            continue
        try:
            row = json.loads(line)
        except json.JSONDecodeError:
            continue
        row_type = str(row.get("type") or "")
        item = row.get("item")
        if row_type == "item.completed" and isinstance(item, dict):
            item_type = str(item.get("type") or "")
            if item_type == "agent_message":
                write_response(assistant_text=str(item.get("text") or ""))
                continue
            if item_type == "command_execution":
                command = str(item.get("command") or "")
                output = str(item.get("aggregated_output") or "")
                exit_code = item.get("exit_code")
                status = str(item.get("status") or "")
                write_response(
                    tool_calls=[
                        {
                            "call_id": str(item.get("id") or ""),
                            "name": "shell",
                            "arguments": {
                                "command": command,
                                "status": status,
                                "exit_code": exit_code,
                            },
                        }
                    ]
                )
                tool_content = output
                if exit_code is not None:
                    tool_content = f"exit_code={exit_code}\n{tool_content}"
                conversation.append(
                    {
                        "role": "tool",
                        "tool_call_id": str(item.get("id") or ""),
                        "content": tool_content,
                    }
                )
                continue

        if row_type == "turn.completed":
            usage = row.get("usage")
            if isinstance(usage, dict):
                input_tokens = int(usage.get("input_tokens", 0) or 0)
                output_tokens = int(usage.get("output_tokens", 0) or 0)
                cache_read = int(usage.get("cached_input_tokens", 0) or 0)
                last_usage = {
                    "input_tokens": input_tokens,
                    "output_tokens": output_tokens,
                    "cache_read_tokens": cache_read,
                    "cache_write_tokens": 0,
                    "total_tokens": int(usage.get("total_tokens", 0) or input_tokens + output_tokens),
                }

    if usage_rows:
        requests_log.write_text("\n".join(usage_rows) + "\n", encoding="utf-8")

    return {
        "stdout_log_file": str(stdout_log_file),
        "proxy_dir": str(proxy_dir),
        "response_count": response_count,
        "requests_log": str(requests_log),
    }


class CodexAdapter(BaseAdapter):
    name = "codex"

    def run(self, ctx: AdapterRunContext) -> AdapterRunResult:
        command = str(ctx.model_config.get("command") or "codex")
        user_config_raw = str(ctx.model_config.get("user_config") or "~/.codex/config.toml")
        user_config = _resolve_project_path(user_config_raw)
        if not user_config.is_file():
            return AdapterRunResult(ok=False, stderr=f"missing Codex source config: {user_config}")

        isolated_home = ctx.sandbox
        codex_home = isolated_home / ".codex"
        codex_home.mkdir(parents=True, exist_ok=True)
        _sync_minimal_codex_state(user_config, codex_home)
        model = str(ctx.model_config.get("model") or "").strip()
        if model:
            _set_top_level_toml_string(codex_home / "config.toml", "model", model)
        reasoning_effort = (
            str(ctx.model_config.get("model_reasoning_effort") or "").strip()
            or _reasoning_effort_from_overrides(ctx.model_config.get("config_overrides"))
        )
        if reasoning_effort:
            _set_top_level_toml_string(codex_home / "config.toml", "model_reasoning_effort", reasoning_effort)

        last_message_file = ctx.sandbox / "codex-last-message.txt"
        cmd = [
            command,
            "exec",
            "--cd",
            str(ctx.workspace),
            "--skip-git-repo-check",
            "--sandbox",
            str(ctx.model_config.get("sandbox") or "workspace-write"),
            "--output-last-message",
            str(last_message_file),
        ]
        ask_for_approval = str(ctx.model_config.get("ask_for_approval") or "").strip()
        if ask_for_approval:
            cmd.extend(["--ask-for-approval", ask_for_approval])
        if bool(ctx.model_config.get("json", True)):
            cmd.append("--json")
        profile = str(ctx.model_config.get("profile") or "").strip()
        if profile:
            cmd.extend(["--profile", profile])
        if model:
            cmd.extend(["--model", model])
        for item in ctx.model_config.get("config_overrides") or []:
            cmd.extend(["--config", str(item)])
        for item in ctx.model_config.get("extra_args") or []:
            cmd.append(str(item))
        cmd.append("-")

        env = os.environ.copy()
        env.update(ctx.env)
        env["HOME"] = str(isolated_home)
        env["CODEX_HOME"] = str(codex_home)
        env["WORKSPACE"] = str(ctx.workspace)
        env["HARNESSBENCH_TASK_ID"] = ctx.task.task_id
        env["HARNESSBENCH_WORKSPACE"] = str(ctx.workspace)
        env["HARNESSBENCH_SANDBOX"] = str(ctx.sandbox)
        env["HARNESSBENCH_SESSION_ID"] = ctx.session_id
        env["HARNESSBENCH_PROMPT_FILE"] = str(ctx.prompt_file)
        env["HARNESSBENCH_MODEL_ID"] = ctx.model_id

        log_stem = f"codex-{ctx.prompt_file.stem}"
        stdout_log_file = ctx.sandbox / f"{log_stem}.stdout.jsonl"
        stderr_log_file = ctx.sandbox / f"{log_stem}.stderr.log"
        stream_to_console = bool(ctx.model_config.get("stream_to_console", False))
        print(f"[harnessbench:codex] stdout_log={stdout_log_file}", flush=True)
        print(f"[harnessbench:codex] stderr_log={stderr_log_file}", flush=True)

        proc = subprocess.Popen(
            cmd,
            cwd=str(ctx.workspace),
            text=True,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=env,
            bufsize=1,
        )

        stdout_chunks: list[str] = []
        stderr_chunks: list[str] = []

        def _reader(pipe: TextIO | None, sink: list[str], log_file: Path, mirror: TextIO | None = None) -> None:
            try:
                assert pipe is not None
                log_file.parent.mkdir(parents=True, exist_ok=True)
                with log_file.open("w", encoding="utf-8", buffering=1) as fh:
                    for line in iter(pipe.readline, ""):
                        sink.append(line)
                        fh.write(line)
                        if mirror is not None:
                            mirror.write(line)
                            mirror.flush()
            finally:
                if pipe is not None:
                    pipe.close()

        stdout_thread = threading.Thread(
            target=_reader,
            args=(proc.stdout, stdout_chunks, stdout_log_file, sys.stdout if stream_to_console else None),
            daemon=True,
        )
        stderr_thread = threading.Thread(
            target=_reader,
            args=(proc.stderr, stderr_chunks, stderr_log_file, sys.stderr if stream_to_console else None),
            daemon=True,
        )
        stdout_thread.start()
        stderr_thread.start()

        if proc.stdin is not None:
            try:
                proc.stdin.write(ctx.prompt)
                proc.stdin.close()
            except BrokenPipeError:
                pass

        timed_out = False
        try:
            returncode = proc.wait(timeout=ctx.timeout_sec)
        except subprocess.TimeoutExpired:
            timed_out = True
            proc.kill()
            returncode = proc.wait()

        stdout_thread.join(timeout=1 if timed_out else None)
        stderr_thread.join(timeout=1 if timed_out else None)
        stdout_text = "".join(stdout_chunks)
        stderr_text = "".join(stderr_chunks)
        codex_session_file = _latest_codex_session_file(codex_home)
        synthetic_proxy_trace: dict[str, Any] = {}
        if codex_session_file is not None:
            synthetic_proxy_trace = _write_codex_session_as_proxy_trace(
                session_file=codex_session_file,
                proxy_dir=ctx.sandbox / "usage-proxy",
                task_id=ctx.task.task_id,
                session_id=ctx.session_id,
                model_id=ctx.model_id,
                response_model=model or ctx.model_id,
            )
        if not synthetic_proxy_trace.get("response_count"):
            synthetic_proxy_trace = _write_codex_exec_json_as_proxy_trace(
                stdout_text=stdout_text,
                stdout_log_file=stdout_log_file,
                proxy_dir=ctx.sandbox / "usage-proxy",
                task_id=ctx.task.task_id,
                session_id=ctx.session_id,
                model_id=ctx.model_id,
                response_model=model or ctx.model_id,
                initial_prompt=ctx.prompt,
            )

        return AdapterRunResult(
            ok=returncode == 0,
            command=cmd,
            stdout=stdout_text,
            stderr=stderr_text,
            metadata={
                "returncode": returncode,
                "timed_out": timed_out,
                "isolated_home": str(isolated_home),
                "codex_home": str(codex_home),
                "state_dir": str(codex_home),
                "source_user_config_path": str(user_config),
                "codex_config_path": str(codex_home / "config.toml"),
                "last_message_file": str(last_message_file),
                "stdout_log_file": str(stdout_log_file),
                "stderr_log_file": str(stderr_log_file),
                "codex_session_file": str(codex_session_file) if codex_session_file else "",
                "synthetic_proxy_trace": synthetic_proxy_trace,
                "workspace": str(ctx.workspace),
            },
        )
