from __future__ import annotations

import os
import re
import shutil
import subprocess
from pathlib import Path

from harnessbench.adapters.base import BaseAdapter
from harnessbench.models import AdapterRunContext, AdapterRunResult
from harnessbench.usage_proxy import register_routes


def _project_root() -> Path:
    return Path(__file__).resolve().parents[3]


def _resolve_project_path(raw: str | Path) -> Path:
    path = Path(os.path.expanduser(str(raw)))
    if not path.is_absolute():
        path = _project_root() / path
    return path.resolve()


def _source_config_from_model_config(model_cfg: dict[str, object]) -> Path:
    raw = model_cfg.get("user_config")
    if raw:
        return _resolve_project_path(str(raw))
    env_raw = os.environ.get("HERMES_CONFIG_PATH") or os.environ.get("HERMES_CONFIG")
    if env_raw:
        return _resolve_project_path(env_raw)
    return _resolve_project_path("~/.hermes/config.yaml")


def _load_yaml(path: Path) -> dict[str, object]:
    try:
        import yaml  # type: ignore
    except ModuleNotFoundError:
        try:
            from ruamel import yaml  # type: ignore
        except ModuleNotFoundError as exc:
            raise RuntimeError("Hermes adapter requires PyYAML or ruamel.yaml to rewrite config") from exc

    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    return dict(data or {}) if isinstance(data, dict) else {}


def _dump_yaml(path: Path, data: dict[str, object]) -> None:
    try:
        import yaml  # type: ignore
    except ModuleNotFoundError:
        try:
            from ruamel import yaml  # type: ignore
        except ModuleNotFoundError as exc:
            raise RuntimeError("Hermes adapter requires PyYAML or ruamel.yaml to rewrite config") from exc

    path.write_text(
        yaml.safe_dump(data, allow_unicode=True, sort_keys=False),
        encoding="utf-8",
    )


def _safe_name(raw: str, fallback: str) -> str:
    cleaned = "".join(ch if ch.isalnum() or ch in ("-", "_", ".") else "-" for ch in raw).strip("-._")
    return cleaned or fallback


def _register_route(
    routes: dict[str, dict[str, str]],
    *,
    proxy_base_url: str,
    upstream: str,
    route_name: str,
    provider: str,
) -> str:
    prefix = f"/hermes/{route_name}"
    routes[prefix] = {
        "framework": "hermes",
        "provider": provider,
        "upstream": upstream,
    }
    return f"{proxy_base_url}{prefix}"


def _matching_custom_provider(
    custom_providers: object,
    base_url: str,
    model_name: str,
) -> dict[str, object] | None:
    if not isinstance(custom_providers, list) or not base_url:
        return None
    candidates = [
        entry
        for entry in custom_providers
        if isinstance(entry, dict)
        and str(entry.get("base_url") or "").strip().rstrip("/") == base_url
        and str(entry.get("api_key") or "").strip()
    ]
    if not candidates:
        return None
    if model_name:
        for entry in candidates:
            if str(entry.get("model") or "").strip() == model_name:
                return entry
    return candidates[0]


def _merge_user_config(
    user_config: Path,
    out_path: Path,
    *,
    proxy_base_url: str = "",
    proxy_routes_file: Path | None = None,
) -> None:
    data = _load_yaml(user_config)
    routes: dict[str, dict[str, str]] = {}
    custom_providers = data.get("custom_providers")

    def rewrite_url(raw: object, route_name: str, provider: str) -> str | None:
        upstream = str(raw or "").strip().rstrip("/")
        if not upstream:
            return None
        if proxy_base_url and upstream.startswith(proxy_base_url.rstrip("/")):
            return upstream
        if proxy_base_url and proxy_routes_file is not None:
            return _register_route(
                routes,
                proxy_base_url=proxy_base_url,
                upstream=upstream,
                route_name=route_name,
                provider=provider,
            )
        return upstream

    model_cfg = data.get("model")
    if isinstance(model_cfg, dict):
        provider_name = str(model_cfg.get("provider") or "model").strip() or "model"
        original_base_url = str(model_cfg.get("base_url") or "").strip().rstrip("/")
        model_name = str(model_cfg.get("default") or model_cfg.get("model") or "").strip()
        matching_provider = _matching_custom_provider(custom_providers, original_base_url, model_name)
        if proxy_base_url and matching_provider:
            matching_name = str(matching_provider.get("name") or "custom").strip() or "custom"
            rewritten = _register_route(
                routes,
                proxy_base_url=proxy_base_url,
                upstream=original_base_url,
                route_name=f"custom-{_safe_name(matching_name, 'custom')}",
                provider=matching_name,
            )
        else:
            rewritten = rewrite_url(original_base_url, f"model-{_safe_name(provider_name, 'model')}", provider_name)
        if rewritten:
            if proxy_base_url and matching_provider and not str(model_cfg.get("api_mode") or "").strip():
                api_mode = str(matching_provider.get("api_mode") or "").strip()
                if api_mode:
                    model_cfg["api_mode"] = api_mode
            model_cfg["base_url"] = rewritten

    if isinstance(custom_providers, list):
        for index, entry in enumerate(custom_providers):
            if not isinstance(entry, dict):
                continue
            provider_name = str(entry.get("name") or f"custom-{index}").strip() or f"custom-{index}"
            rewritten = rewrite_url(entry.get("base_url"), f"custom-{_safe_name(provider_name, f'custom-{index}')}", provider_name)
            if rewritten:
                entry["base_url"] = rewritten

    auxiliary = data.get("auxiliary")
    if isinstance(auxiliary, dict):
        for key, entry in auxiliary.items():
            if not isinstance(entry, dict):
                continue
            rewritten = rewrite_url(entry.get("base_url"), f"aux-{_safe_name(str(key), 'aux')}", str(key))
            if rewritten:
                entry["base_url"] = rewritten

    if routes and proxy_routes_file is not None:
        register_routes(proxy_routes_file, routes)

    _dump_yaml(out_path, data)


def _build_command(ctx: AdapterRunContext, command: str, args: list[str]) -> list[str]:
    fmt = {
        "workspace": str(ctx.workspace),
        "sandbox": str(ctx.sandbox),
        "prompt_file": str(ctx.prompt_file),
        "session_id": ctx.session_id,
        "task_id": ctx.task.task_id,
        "model_id": ctx.model_id,
    }
    cmd = [command, *[str(arg).format(**fmt) for arg in args]]

    if "-q" in cmd or "--query" in cmd:
        for index, arg in enumerate(cmd):
            if arg in {"-q", "--query"}:
                cmd.insert(index + 1, ctx.prompt)
                break
        return cmd

    cmd.extend(["-q", ctx.prompt])
    return cmd


# Hermes session ID 格式：YYYYMMDD_HHMMSS_xxxxxxxx
# 匹配 stdout/stderr 中 "Session: 20260410_181750_d6f1d7" 这样的行
# 注意：该行被 Unicode 框线字符（│ U+2502）包裹，需要用 re.search 而非 startswith
_SESSION_ID_RE = re.compile(r"Session:\s*(\d{8}_\d{6}_[0-9a-f]+)", re.IGNORECASE)

# Hermes 在多线程退出时会因 CPython stdin buffer lock 竞争而以 SIGABRT(-6) 退出。
# 这是已知的无害 bug，任务本身已完整执行，不应视为失败。
_BENIGN_RETURNCODES = {0, -6}


def _parse_session_id(text: str) -> str | None:
    """从 Hermes stdout/stderr 中提取 session ID。"""
    match = _SESSION_ID_RE.search(text)
    return match.group(1) if match else None


class HermesAgentAdapter(BaseAdapter):
    name = "hermes_agent"

    def run(self, ctx: AdapterRunContext) -> AdapterRunResult:
        command = str(ctx.model_config.get("command") or "hermes")
        args = [str(arg) for arg in (ctx.model_config.get("args") or ["chat"])]
        use_usage_proxy = bool(ctx.model_config.get("use_usage_proxy", False))

        source_config = _source_config_from_model_config(ctx.model_config)
        if not source_config.is_file():
            return AdapterRunResult(ok=False, stderr=f"missing Hermes source config: {source_config}")

        isolated_home = ctx.sandbox
        hermes_home = isolated_home / ".hermes"
        hermes_home.mkdir(parents=True, exist_ok=True)
        hermes_workspace = hermes_home / "workspace"
        hermes_workspace.mkdir(parents=True, exist_ok=True)

        sandbox_user_config = hermes_home / "config.src.yaml"
        shutil.copy2(source_config, sandbox_user_config)
        merged_cfg = hermes_home / "config.yaml"
        _merge_user_config(
            sandbox_user_config,
            merged_cfg,
            proxy_base_url=str(ctx.env.get("HARNESSBENCH_LLM_PROXY_URL") or "") if use_usage_proxy else "",
            proxy_routes_file=(
                Path(ctx.env["HARNESSBENCH_LLM_PROXY_ROUTES"])
                if use_usage_proxy and ctx.env.get("HARNESSBENCH_LLM_PROXY_ROUTES")
                else None
            ),
        )

        # ── Session 续接逻辑 ──────────────────────────────────────────────────
        # 每轮结束后把 Hermes session ID 写入 last_session_id.txt，
        # 下一轮优先用 --resume <id> 续接；若文件不存在（第 1 轮）则不加任何参数。
        # 若上一轮因 stdout 格式问题未能解析到 session ID，则 fallback 到 -c
        # （让 Hermes 自动续接最近一次 session），保证多轮对话上下文连续。
        session_id_file = hermes_home / "last_session_id.txt"
        resume_session_id: str | None = None
        resume_method: str = "none"  # "resume" | "continue" | "none"

        if session_id_file.exists():
            saved = session_id_file.read_text(encoding="utf-8").strip()
            if saved:
                resume_session_id = saved
                resume_method = "resume"

        # ── 构建命令 ──────────────────────────────────────────────────────────
        cmd = _build_command(ctx, command, args)

        if resume_method == "resume":
            # 精确续接：--resume <session_id>
            cmd.extend(["--resume", resume_session_id])
        elif resume_method == "continue":
            # Fallback：-c 续接最近一次 session（当上一轮 session ID 解析失败时）
            cmd.append("-c")

        # ── 执行 ──────────────────────────────────────────────────────────────
        env = os.environ.copy()
        env["HOME"] = str(isolated_home)
        env["HERMES_HOME"] = str(hermes_home)
        env["HERMES_CONFIG"] = str(merged_cfg)
        env["HERMES_CONFIG_PATH"] = str(merged_cfg)

        completed = subprocess.run(
            cmd,
            cwd=str(ctx.workspace),
            text=True,
            capture_output=True,
            timeout=ctx.timeout_sec,
            env=env,
            check=False,
        )

        # ── 解析本轮产生的 session ID ─────────────────────────────────────────
        # Hermes 在 stdout 的 UI 框里打印：
        #   │             Session: 20260410_181750_d6f1d7             │
        # 用 re.search 可以穿透框线字符直接匹配。
        combined_output = completed.stdout + "\n" + completed.stderr
        session_id_from_output = _parse_session_id(combined_output)

        # 持久化 session ID 供下一轮使用
        if session_id_from_output:
            # 成功解析：写入精确 ID，下一轮用 --resume
            session_id_file.write_text(session_id_from_output, encoding="utf-8")
        elif resume_session_id:
            # 本轮未解析到新 ID（可能是 Hermes 版本差异），保留上一轮的 ID
            session_id_file.write_text(resume_session_id, encoding="utf-8")
        else:
            # 第 1 轮且未解析到 ID：写入特殊标记，下一轮用 -c fallback
            session_id_file.write_text("__use_continue__", encoding="utf-8")

        # 处理 fallback 标记（上一轮写入了 __use_continue__）
        if resume_session_id == "__use_continue__":
            resume_method = "continue"
            resume_session_id = None

        # ── 判断本轮是否成功 ──────────────────────────────────────────────────
        # returncode -6 (SIGABRT) 是 CPython 多线程退出时的已知无害 bug，
        # 只要 stdout 有内容就认为任务执行完成。
        rc = completed.returncode
        if rc in _BENIGN_RETURNCODES:
            ok = bool(completed.stdout.strip())
        else:
            ok = False

        return AdapterRunResult(
            ok=ok,
            command=cmd,
            stdout=completed.stdout,
            stderr=completed.stderr,
            metadata={
                "returncode": rc,
                "source_user_config_path": str(source_config),
                "sandbox_user_config_path": str(sandbox_user_config),
                "hermes_config_path": str(merged_cfg),
                "isolated_home": str(isolated_home),
                "hermes_home": str(hermes_home),
                "hermes_workspace": str(hermes_workspace),
                "workspace": str(ctx.workspace),
                "usage_proxy_enabled": use_usage_proxy,
                "session_id": ctx.session_id,
                "hermes_session_id": session_id_from_output or resume_session_id,
                "resume_method": resume_method,
            },
        )
