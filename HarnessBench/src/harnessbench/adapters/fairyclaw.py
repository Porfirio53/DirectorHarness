from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path
from typing import Any

from harnessbench.adapters.base import BaseAdapter
from harnessbench.models import AdapterRunContext, AdapterRunResult
from harnessbench.usage_proxy import register_routes


def _project_root() -> Path:
    return Path(__file__).resolve().parents[3]


def _resolve_project_path(raw: str | Path) -> Path:
    p = Path(os.path.expanduser(str(raw)))
    if not p.is_absolute():
        p = _project_root() / p
    return p.resolve()


def _load_yaml(path: Path) -> dict[str, Any]:
    try:
        import yaml  # type: ignore
    except ModuleNotFoundError as exc:
        raise RuntimeError("PyYAML is required for fairyclaw adapter") from exc
    text = path.read_text(encoding="utf-8")
    data = yaml.safe_load(text)
    return data if isinstance(data, dict) else {}


def _dump_yaml(path: Path, data: dict[str, Any]) -> None:
    import yaml  # type: ignore

    path.write_text(yaml.safe_dump(data, allow_unicode=True, sort_keys=False), encoding="utf-8")


def _merge_llm_endpoints_for_proxy(
    llm_yaml: Path,
    proxy_base_url: str,
    routes_file: Path | None,
) -> None:
    """Rewrite each profile's api_base through Harness-Bench usage-proxy (OpenAI-compatible)."""
    if not llm_yaml.is_file():
        return
    data = _load_yaml(llm_yaml)
    profiles = data.get("profiles")
    if not isinstance(profiles, dict):
        return
    routes: dict[str, dict[str, str]] = {}
    for name, cfg in profiles.items():
        if not isinstance(cfg, dict):
            continue
        upstream = str(cfg.get("api_base") or "").strip()
        if not upstream:
            continue
        safe = "".join(c if (c.isalnum() or c in "-_") else "_" for c in str(name))[:80] or "profile"
        prefix = f"/fairyclaw/{safe}"
        routes[prefix] = {
            "framework": "fairyclaw",
            "provider": str(name),
            "upstream": upstream.rstrip("/"),
        }
        cfg["api_base"] = f"{proxy_base_url.rstrip('/')}{prefix}"
    if routes and routes_file is not None:
        register_routes(routes_file, routes)
        _dump_yaml(llm_yaml, data)


class FairyClawAdapter(BaseAdapter):
    """Harness-Bench + FairyClaw via in-process ``fairyclaw agent`` (no separate server)."""

    name = "fairyclaw"

    def run(self, ctx: AdapterRunContext) -> AdapterRunResult:
        command = str(ctx.model_config.get("command") or "fairyclaw")
        user_config_raw = str(
            ctx.model_config.get("user_config")
            or os.path.expanduser("~/FairyClaw/config")
        )
        user_config = _resolve_project_path(user_config_raw)
        if not user_config.is_dir():
            return AdapterRunResult(
                ok=False,
                stderr=f"fairyclaw user_config must be an existing config directory: {user_config}",
            )

        fc_root = (ctx.sandbox / ".fairyclaw").resolve()
        config_dir = fc_root / "config"
        data_dir = fc_root / "data"
        config_dir.mkdir(parents=True, exist_ok=True)
        data_dir.mkdir(parents=True, exist_ok=True)
        (data_dir / "logs").mkdir(parents=True, exist_ok=True)
        (data_dir / "files").mkdir(parents=True, exist_ok=True)

        for child in user_config.iterdir():
            dest = config_dir / child.name
            if child.is_dir():
                shutil.copytree(child, dest, dirs_exist_ok=True)
            else:
                dest.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(child, dest)

        llm_yaml = config_dir / "llm_endpoints.yaml"
        proxy_url = str(ctx.env.get("HARNESSBENCH_LLM_PROXY_URL") or "").strip()
        routes_file = ctx.env.get("HARNESSBENCH_LLM_PROXY_ROUTES")
        if proxy_url and routes_file:
            _merge_llm_endpoints_for_proxy(llm_yaml, proxy_url, Path(routes_file))

        extra = [str(x) for x in (ctx.model_config.get("args") or [])]
        idle = ctx.model_config.get("bench_idle_seconds")
        poll = ctx.model_config.get("bench_poll_interval")
        min_wait = ctx.model_config.get("bench_min_wait_after_send")

        def _build_env() -> dict[str, str]:
            env = os.environ.copy()
            env.update(ctx.env)
            env["HOME"] = str(ctx.sandbox.resolve())
            env["FAIRYCLAW_CONFIG_DIR"] = str(config_dir.resolve())
            env["FAIRYCLAW_DATA_DIR"] = str(data_dir.resolve())
            env["FAIRYCLAW_LLM_ENDPOINTS_CONFIG_PATH"] = str(llm_yaml.resolve())
            env["FAIRYCLAW_GATEWAY_HOST"] = "127.0.0.1"
            # Set filesystem_root_dir to the task workspace so that the agent's
            # run_command / file tools operate relative to the workspace by default.
            env["FAIRYCLAW_FILESYSTEM_ROOT_DIR"] = str(ctx.workspace.resolve())
            if "FAIRYCLAW_DATABASE_URL" not in env:
                env["FAIRYCLAW_DATABASE_URL"] = f"sqlite+aiosqlite:///{data_dir / 'fairyclaw.db'}"
            if "FAIRYCLAW_LOG_FILE_PATH" not in env:
                env["FAIRYCLAW_LOG_FILE_PATH"] = str(data_dir / "logs" / "fairyclaw.log")
            return env

        common_meta: dict[str, Any] = {
            "fairyclaw_config_dir": str(config_dir),
            "fairyclaw_data_dir": str(data_dir),
            "source_user_config": str(user_config),
            "sandbox": str(ctx.sandbox),
            "workspace": str(ctx.workspace),
            "state_dir": str(data_dir.resolve()),
        }

        cmd: list[str] = [
            command,
            *extra,
            "agent",
            "--json-only",
            "--session",
            ctx.session_id,
            "--timeout",
            str(float(ctx.timeout_sec)),
        ]
        if idle is not None:
            cmd.extend(["--idle-seconds", str(float(idle))])
        if poll is not None:
            cmd.extend(["--poll-interval", str(float(poll))])
        if min_wait is not None:
            cmd.extend(["--min-wait-after-send", str(float(min_wait))])
        cmd.extend(["--message", ctx.prompt])
        try:
            completed = subprocess.run(
                cmd,
                cwd=str(ctx.workspace.resolve()),
                text=True,
                capture_output=True,
                timeout=ctx.timeout_sec + 120,
                env=_build_env(),
                check=False,
            )
        except subprocess.TimeoutExpired as exc:
            return AdapterRunResult(
                ok=False,
                command=cmd,
                stdout="",
                stderr=f"fairyclaw agent timed out: {exc!s}",
                metadata=common_meta,
            )
        return AdapterRunResult(
            ok=completed.returncode == 0,
            command=cmd,
            stdout=completed.stdout,
            stderr=completed.stderr,
            metadata={
                **common_meta,
                "returncode": completed.returncode,
            },
        )
