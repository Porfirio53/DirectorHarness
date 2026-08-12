from __future__ import annotations

import json
import re
import shutil
import time
import uuid
from pathlib import Path
from typing import Any

from harnessbench.extract_proxy_trace import extract_proxy_trace, extract_proxy_trace_incremental
from harnessbench.grading.process_grade import compute_scoring
from harnessbench.models import AdapterRunContext, AppConfig, TaskRunResult, TaskSpec
from harnessbench.registry import build_adapter
from harnessbench.tasks import load_hooks, run_oracle
from harnessbench.usage_proxy import UsageProxy


def _sanitize_api_dir_segment(label: str) -> str:
    """将简短 model 名（不含 provider 前缀）转为可用作目录名的片段。"""
    s = label.strip()
    if not s:
        return "unknown-api"
    s = s.replace("/", "-")
    out: list[str] = []
    for ch in s:
        if ch.isalnum() or ch in "-_.":
            out.append(ch)
        elif ch in " \t":
            out.append("-")
        else:
            out.append("_")
    s = "".join(out).strip("-_.")
    while "--" in s:
        s = s.replace("--", "-")
    return s[:200] if s else "unknown-api"


def _slug_from_response_model_label(label: str) -> str:
    """目录名：优先用 provider/model 中 / 之后的一段（如 qiniu/deepseek-v3.2 → deepseek-v3.2）。"""
    s = label.strip()
    if not s:
        return "unknown-api"
    if "/" in s:
        s = s.rsplit("/", 1)[-1].strip()
    return _sanitize_api_dir_segment(s)


_CLAUDE_OPUS_46 = re.compile(r"claude-opus-4[.-]?6", re.IGNORECASE)
_CLAUDE_SONNET_46 = re.compile(r"claude-sonnet-4[.-]?6", re.IGNORECASE)


def _canonical_claude_sonnet_opus_slug(label: str) -> str | None:
    """若标签属于 Claude Sonnet/Opus 4.6，则映射到固定目录名（忽略上游 provider 前缀）。"""
    if not label or not str(label).strip():
        return None
    s = str(label).strip()
    if _CLAUDE_OPUS_46.search(s):
        return "claude-opus-4-6"
    if _CLAUDE_SONNET_46.search(s):
        return "claude-sonnet-4-6"
    return None


def _api_slug_from_model_label(label: str) -> str:
    """落盘 slug：Sonnet/Opus 4.6 用固定名，其余沿用原有规则。"""
    fixed = _canonical_claude_sonnet_opus_slug(label)
    return fixed if fixed else _slug_from_response_model_label(label)


def _last_response_model_from_proxy_log(log_file: Path) -> str | None:
    """取 proxy 日志最后一行非空的 response_model（通常对应最后一次 LLM 调用）。"""
    last_model: str | None = None
    if not log_file.is_file():
        return None
    for line in log_file.read_text(encoding="utf-8", errors="replace").splitlines():
        if not line.strip():
            continue
        try:
            row = json.loads(line)
        except json.JSONDecodeError:
            continue
        m = str(row.get("response_model", "")).strip()
        if m:
            last_model = m
    return last_model


def _cli_model_from_adapter_command(command: Any) -> str | None:
    if not isinstance(command, list):
        return None
    for i, arg in enumerate(command):
        if str(arg) in ("-m", "--model") and i + 1 < len(command):
            m = str(command[i + 1]).strip()
            return m or None
    return None


def derive_api_result_slug(
    proxy_dir: Path,
    usage_summary: dict[str, Any],
    adapter_result: Any,
    model_cfg: dict[str, Any] | None = None,
) -> tuple[str, str]:
    """
    返回 (用于落盘子目录的 slug, 原始可读标签)。
    目录形如 results/<bench_model_id>/<slug>/<task_id>.json

    对 **Claude Sonnet / Opus 4.6**：slug 固定为 ``claude-sonnet-4-6`` / ``claude-opus-4-6``，
    且若 harness 模型配置里声明了 ``model`` 并命中上述二者之一，则 **以配置为准**，
    不因网关在多轮里上报不同 ``response_model`` 而切换到另一目录。
    """
    requested = ""
    if isinstance(model_cfg, dict):
        requested = str(model_cfg.get("model") or "").strip()
    pinned = _canonical_claude_sonnet_opus_slug(requested)
    if pinned:
        return pinned, requested

    cmd = getattr(adapter_result, "command", None)
    cli_model = _cli_model_from_adapter_command(cmd)
    cli_pin = _canonical_claude_sonnet_opus_slug(cli_model or "")
    if cli_pin:
        return cli_pin, str(cli_model).strip()

    log_file = proxy_dir / "requests.jsonl"
    primary = _last_response_model_from_proxy_log(log_file)
    if primary:
        return _api_slug_from_model_label(primary), primary

    try:
        trace = extract_proxy_trace(proxy_dir, all_rounds=False)
        rounds = trace.get("rounds") or []
        if rounds:
            usage = rounds[-1].get("usage") or {}
            m = str(usage.get("response_model", "")).strip()
            if m:
                return _api_slug_from_model_label(m), m
    except Exception:
        pass

    if usage_summary.get("available") and usage_summary.get("models"):
        models = [str(x).strip() for x in usage_summary["models"] if str(x).strip()]
        if len(models) == 1:
            m = models[0]
            return _api_slug_from_model_label(m), m
        if len(models) > 1:
            slugs = [_api_slug_from_model_label(m) for m in models]
            unique = list(dict.fromkeys(slugs))
            if len(unique) == 1:
                return unique[0], ",".join(models)
            slug = "+".join(unique)
            return (slug[:200] if slug else "mixed-apis"), ",".join(models)

    if cli_model:
        return _api_slug_from_model_label(cli_model), cli_model

    return "unknown-api", ""


def render_prompt_file(task: TaskSpec, prompt_name: str, workspace: Path, runtime_env: dict[str, str], adapter_name) -> str:
    assert task.task_dir is not None
    prompt_template = (task.task_dir / prompt_name).read_text(encoding="utf-8")
    #nanoclaw在docker里跑的挂载路径是/workspace/group，这里特殊处理一下
    if adapter_name != "nanoclaw":
        rendered = prompt_template.replace("$WORKSPACE", str(workspace))
    else:
        rendered = prompt_template.replace("$WORKSPACE", "/workspace/group")
    for key, value in runtime_env.items():
        rendered = rendered.replace(f"${key}", str(value))
    return rendered


def render_prompt(task: TaskSpec, workspace: Path, runtime_env: dict[str, str], adapter_name) -> str:
    return render_prompt_file(task, task.prompt_file, workspace, runtime_env, adapter_name)


def _copy_fixtures(task: TaskSpec, workspace: Path) -> None:
    assert task.task_dir is not None
    fixtures = task.task_dir / task.fixtures_dir
    workspace.mkdir(parents=True, exist_ok=True)
    # Keep benchmark workspaces structurally consistent so adapters can rely on
    # `workspace/in` and `workspace/out` even when a task has no fixtures yet.
    (workspace / "in").mkdir(parents=True, exist_ok=True)
    (workspace / "out").mkdir(parents=True, exist_ok=True)
    if fixtures.is_dir():
        for child in fixtures.iterdir():
            dest = workspace / child.name
            if child.is_dir():
                shutil.copytree(child, dest, dirs_exist_ok=True)
            else:
                shutil.copy2(child, dest)


def _new_session_id(model_cfg: dict[str, Any], task_id: str) -> str:
    prefix = str(model_cfg.get("session_prefix", "harnessbench"))
    return f"{prefix}-{task_id}-{uuid.uuid4().hex[:8]}"


def _safe_task_id_segment(task_id: str) -> str:
    safe = "".join(ch if ch.isalnum() or ch in ("-", "_") else "-" for ch in task_id).strip("-_")
    return safe or "task"


def _model_segment_for_sandbox(model_cfg: dict[str, Any], model_id: str) -> str:
    """用于沙箱目录名：与 results 子目录类似，取自 models 配置中的 model（CLI 传入的 API id）。"""
    m = str(model_cfg.get("model") or "").strip()
    if m:
        api = _slug_from_response_model_label(m) if "/" in m else _sanitize_api_dir_segment(m)
    else:
        api = _sanitize_api_dir_segment(model_id)
    return api[:80] if len(api) > 80 else api


def _create_sandbox_dir(
    work_root: Path, task_id: str, model_id: str, model_cfg: dict[str, Any]
) -> tuple[Path, str, str]:
    """
    目录：``<work_root>/<model_id>/<api_slug>/oc-bench-v2-<task_id>-<api_slug>-<YYYYMMDD-HHMMSS>-<8hex>``

    ``model_id`` 来自 CLI 的 ``--harness``，``api_slug`` 初始取自模型配置，运行后会按 proxy 识别到的真实 API 模型移动到最终目录。

    Returns:
        (sandbox_path, initial_api_seg, ts_uuid_suffix)
        ``ts_uuid_suffix`` 为 ``YYYYMMDD-HHMMSS-8hex`` 部分，供运行结束后重命名用。
    """
    task_seg = _safe_task_id_segment(task_id)
    model_seg = _sanitize_api_dir_segment(model_id)
    api_seg = _model_segment_for_sandbox(model_cfg, model_id)
    ts = time.strftime("%Y%m%d-%H%M%S")
    for _ in range(16):
        uid = uuid.uuid4().hex[:8]
        suffix = f"{ts}-{uid}"
        name = f"oc-bench-v2-{task_seg}-{api_seg}-{suffix}"
        base = (work_root / model_seg / api_seg).resolve()
        base.mkdir(parents=True, exist_ok=True)
        candidate = base / name
        try:
            candidate.mkdir(parents=False, exist_ok=False)
            return candidate, api_seg, suffix
        except FileExistsError:
            continue
    raise OSError("could not create unique sandbox directory under work_root")

def _collect_hermes_usage_summary(db_file: Path, session_id: str, session_root: Path) -> dict[str, Any]:
    """从 Hermes SQLite 数据库收集 usage 信息"""
    try:
        import sqlite3
    except ImportError:
        return {
            "available": False,
            "reason": "sqlite3 module not available",
            "session_id": session_id,
            "usage_root": str(session_root),
            "database_file": str(db_file),
        }
    
    try:
        conn = sqlite3.connect(db_file)
        cursor = conn.cursor()
        
        # 查询最近创建的会话
        cursor.execute(
            "SELECT id, title, started_at, model, billing_provider, input_tokens, output_tokens, message_count FROM sessions ORDER BY started_at DESC LIMIT 1"
        )
        latest_session = cursor.fetchone()
        
        if not latest_session:
            return {
                "available": False,
                "reason": "Hermes database has no sessions",
                "session_id": session_id,
                "usage_root": str(session_root),
                "database_file": str(db_file),
            }
        
        # 使用数据库中的实际会话ID
        session_id_in_db = latest_session[0]  # 数据库中的ID
        
        # 从 sessions 表获取 token 统计
        input_tokens = latest_session[5] or 0  # input_tokens
        output_tokens = latest_session[6] or 0  # output_tokens
        total_tokens = input_tokens + output_tokens
        
        # 查询该会话的所有消息
        cursor.execute(
            "SELECT role, content, token_count FROM messages WHERE session_id = ? ORDER BY timestamp",
            (session_id_in_db,)
        )
        messages = cursor.fetchall()
        
        # 如果 sessions 表没有 token 统计，尝试从 messages 表计算
        if input_tokens == 0 and output_tokens == 0:
            for role, content, token_count in messages:
                if token_count is not None:
                    total_tokens += int(token_count)
                    if role == "user":
                        input_tokens += int(token_count)
                    elif role == "assistant":
                        output_tokens += int(token_count)
        
        # 收集 usage 信息
        summary: dict[str, Any] = {
            "available": True,
            "source": "hermes_sqlite",
            "session_id": session_id_in_db,  # 数据库中的实际ID
            "original_session_id": session_id,  # HarnessBench生成的ID
            "usage_root": str(session_root),
            "database_file": str(db_file),
            "message_count": len(messages),
            "usage_message_count": len(messages),
            "input_tokens": input_tokens,
            "output_tokens": output_tokens,
            "total_tokens": total_tokens,
            "providers": [latest_session[4] or "hermes"],  # billing_provider
            "models": [latest_session[3] or "unknown"],    # model
        }
        
        conn.close()
        return summary
        
    except sqlite3.Error as e:
        return {
            "available": False,
            "reason": f"Hermes database error: {str(e)}",
            "session_id": session_id,
            "usage_root": str(session_root),
            "database_file": str(db_file),
        }

def _collect_usage_summary(adapter_result: Any, session_id: str) -> dict[str, Any]:
    metadata = getattr(adapter_result, "metadata", {}) or {}
    usage_root_raw = str(
        metadata.get("state_dir")
        or metadata.get("openclaw_home")
        or metadata.get("nanobot_home")
        or metadata.get("zeroclaw_home")
        or metadata.get("picoclaw_workspace")
        or metadata.get("hermes_home")
        or ""
    ).strip()
    if not usage_root_raw:
        return {
            "available": False,
            "reason": "adapter metadata has no usage root",
            "session_id": session_id,
        }

    session_root = Path(usage_root_raw)

    # 检查是否是 Hermes SQLite 数据库
    db_file = session_root / "state.db"
    if db_file.exists():
        # Hermes 使用 SQLite，调用专门的函数处理
        return _collect_hermes_usage_summary(db_file, session_id, session_root)
    
    candidates = [
        session_root / "agents" / "main" / "sessions" / f"{session_id}.jsonl",
        session_root / "agent" / "sessions" / f"{session_id}.jsonl",
        session_root / "sessions" / f"{session_id}.jsonl",
        session_root / "workspace" / "sessions" / f"{session_id}.jsonl",
    ]
    session_file = next((path for path in candidates if path.is_file()), None)
    if session_file is None:
        exact = list(session_root.rglob(f"{session_id}.jsonl"))
        if exact:
            session_file = exact[0]
    if session_file is None:
        all_jsonl = sorted(
            session_root.rglob("*.jsonl"),
            key=lambda p: p.stat().st_mtime,
            reverse=True,
        )
        if all_jsonl:
            session_file = all_jsonl[0]
    if session_file is None:
        return {
            "available": False,
            "reason": "session jsonl not found",
            "session_id": session_id,
            "usage_root": usage_root_raw,
            "session_file": str(candidates[0]),
        }

    summary: dict[str, Any] = {
        "available": True,
        "session_id": session_id,
        "usage_root": usage_root_raw,
        "session_file": str(session_file),
        "message_count": 0,
        "usage_message_count": 0,
        "input_tokens": 0,
        "output_tokens": 0,
        "cache_read_tokens": 0,
        "cache_write_tokens": 0,
        "total_tokens": 0,
        "cost_input": 0,
        "cost_output": 0,
        "cost_cache_read": 0,
        "cost_cache_write": 0,
        "cost_total": 0,
        "providers": [],
        "models": [],
    }
    providers: set[str] = set()
    models: set[str] = set()

    for line in session_file.read_text(encoding="utf-8", errors="replace").splitlines():
        if not line.strip():
            continue
        try:
            row = json.loads(line)
        except json.JSONDecodeError:
            continue
        message = row.get("message") or {}
        if message:
            summary["message_count"] += 1
        usage = message.get("usage") or {}
        if usage:
            summary["usage_message_count"] += 1
            summary["input_tokens"] += int(usage.get("input", 0) or 0)
            summary["output_tokens"] += int(usage.get("output", 0) or 0)
            summary["cache_read_tokens"] += int(usage.get("cacheRead", 0) or 0)
            summary["cache_write_tokens"] += int(usage.get("cacheWrite", 0) or 0)
            summary["total_tokens"] += int(usage.get("totalTokens", 0) or 0)
            cost = usage.get("cost") or {}
            summary["cost_input"] += float(cost.get("input", 0) or 0)
            summary["cost_output"] += float(cost.get("output", 0) or 0)
            summary["cost_cache_read"] += float(cost.get("cacheRead", 0) or 0)
            summary["cost_cache_write"] += float(cost.get("cacheWrite", 0) or 0)
            summary["cost_total"] += float(cost.get("total", 0) or 0)
        provider = str(message.get("provider", "")).strip()
        model = str(message.get("model", "")).strip()
        if provider:
            providers.add(provider)
        if model:
            models.add(model)

    summary["providers"] = sorted(providers)
    summary["models"] = sorted(models)
    return summary


def _collect_proxy_usage_summary(log_file: Path, session_id: str) -> dict[str, Any]:
    if not log_file.is_file():
        return {
            "available": False,
            "reason": "usage proxy log not found",
            "session_id": session_id,
            "log_file": str(log_file),
        }

    summary: dict[str, Any] = {
        "available": True,
        "source": "proxy",
        "session_id": session_id,
        "log_file": str(log_file),
        "request_count": 0,
        "input_tokens": 0,
        "output_tokens": 0,
        "cache_read_tokens": 0,
        "cache_write_tokens": 0,
        "total_tokens": 0,
        "providers": [],
        "models": [],
    }
    providers: set[str] = set()
    models: set[str] = set()

    for line in log_file.read_text(encoding="utf-8", errors="replace").splitlines():
        if not line.strip():
            continue
        try:
            row = json.loads(line)
        except json.JSONDecodeError:
            continue
        summary["request_count"] += 1
        summary["input_tokens"] += int(row.get("input_tokens", 0) or 0)
        summary["output_tokens"] += int(row.get("output_tokens", 0) or 0)
        summary["cache_read_tokens"] += int(row.get("cache_read_tokens", 0) or 0)
        summary["cache_write_tokens"] += int(row.get("cache_write_tokens", 0) or 0)
        summary["total_tokens"] += int(row.get("total_tokens", 0) or 0)
        provider = str(row.get("provider", "")).strip()
        model = str(row.get("response_model", "")).strip()
        if provider:
            providers.add(provider)
        if model:
            models.add(model)

    if summary["request_count"] == 0:
        return {
            "available": False,
            "reason": "usage proxy saw no requests",
            "session_id": session_id,
            "log_file": str(log_file),
        }

    summary["providers"] = sorted(providers)
    summary["models"] = sorted(models)
    return summary


def run_task(app: AppConfig, task: TaskSpec, model_id: str, model_cfg: dict[str, Any], mode: str, keep_workspace: bool = True) -> TaskRunResult:
    t_run_start = time.perf_counter()
    sandbox, _initial_api_seg, _sandbox_suffix = _create_sandbox_dir(app.work_root, task.task_id, model_id, model_cfg)
    workspace = sandbox / "workspace"
    _copy_fixtures(task, workspace)

    runtime_env: dict[str, str] = {}
    hooks = load_hooks(task)
    runtime_state: dict[str, Any] = {}
    if hooks and callable(getattr(hooks, "prepare_runtime", None)):
        state = hooks.prepare_runtime({"task": task, "sandbox": sandbox, "workspace": workspace})
        if isinstance(state, dict):
            runtime_state.update(state)
            for key, value in state.items():
                if isinstance(value, str):
                    runtime_env[key] = value

    adapter_name = str(model_cfg.get("adapter") or "demo")
    adapter = build_adapter(adapter_name)
    session_id = _new_session_id(model_cfg, task.task_id)
    prompt_names = list(task.prompt_files or []) or [task.prompt_file]
    adapter_results = []
    prompt_file = sandbox / "prompt.txt"
    adapter_result = None
    proxy_dir = sandbox / "usage-proxy"
    proxy_routes = proxy_dir / "routes.json"
    proxy_log = proxy_dir / "requests.jsonl"
    proxy_raw_dir = proxy_dir / "responses"
    with UsageProxy(proxy_routes, proxy_log, proxy_raw_dir, task.task_id, session_id, model_id) as proxy:
        runtime_env["HARNESSBENCH_LLM_PROXY_URL"] = proxy.base_url
        runtime_env["HARNESSBENCH_LLM_PROXY_ROUTES"] = str(proxy_routes)
        for round_index, prompt_name in enumerate(prompt_names):
            prompt = render_prompt_file(task, prompt_name, workspace, runtime_env, adapter_name)
            prompt_file = sandbox / f"prompt-round{round_index + 1}.txt"
            prompt_file.write_text(prompt, encoding="utf-8")
            ctx = AdapterRunContext(
                task=task,
                workspace=workspace,
                sandbox=sandbox,
                prompt=prompt,
                prompt_file=prompt_file,
                session_id=session_id,
                timeout_sec=int(model_cfg.get("timeout_sec", task.timeout_sec or app.default_timeout_sec)),
                env=runtime_env,
                model_id=model_id,
                model_config=model_cfg,
                mode=mode,
            )
            adapter_result = adapter.run(ctx)
            adapter_results.append(adapter_result)
            if hooks and callable(getattr(hooks, "after_round", None)):
                after_state = hooks.after_round(
                    {
                        "task": task,
                        "sandbox": sandbox,
                        "workspace": workspace,
                        "session_id": session_id,
                        "round_index": round_index,
                        "prompt_file": prompt_file,
                        "prompt_name": prompt_name,
                    },
                    runtime_state,
                    adapter_result,
                )
                if isinstance(after_state, dict):
                    runtime_state.update(after_state)
                    for key, value in after_state.items():
                        if isinstance(value, str):
                            runtime_env[key] = value
            if not adapter_result.ok:
                break

    assert adapter_result is not None
    usage_summary = _collect_proxy_usage_summary(proxy_log, session_id)
    if not usage_summary.get("available"):
        usage_summary = _collect_usage_summary(adapter_result, session_id)
    oracle_result = run_oracle(task, workspace)

    try:
        scoring = compute_scoring(task, sandbox, oracle_result)
    except Exception as exc:
        scoring = {
            "error": str(exc),
            "combined_score": None,
            "notes": "compute_scoring failed",
        }

    api_slug, api_label = derive_api_result_slug(proxy_dir, usage_summary, adapter_result, model_cfg)

    # Move sandbox under the resolved API slug (proxy-derived, or pinned for Claude Sonnet/Opus 4.6).
    # The initial path used model_cfg ``model`` / bench ``model_id``; may adjust when slug differs.
    _real_api_seg = api_slug if api_slug not in ("", "unknown-api") else ""
    if _real_api_seg and _real_api_seg != _initial_api_seg:
        _task_seg = _safe_task_id_segment(task.task_id)
        _expected_prefix = f"oc-bench-v2-{_task_seg}-{_initial_api_seg}-"
        if sandbox.name.startswith(_expected_prefix):
            _new_name = f"oc-bench-v2-{_task_seg}-{_real_api_seg}-{_sandbox_suffix}"
            _new_sandbox = sandbox.parent.parent / _real_api_seg / _new_name
            if not _new_sandbox.exists():
                try:
                    _new_sandbox.parent.mkdir(parents=True, exist_ok=True)
                    sandbox.rename(_new_sandbox)
                    sandbox = _new_sandbox
                    workspace = sandbox / "workspace"
                    prompt_file = sandbox / prompt_file.name
                    proxy_dir = sandbox / "usage-proxy"
                    proxy_log = proxy_dir / "requests.jsonl"
                    proxy_raw_dir = proxy_dir / "responses"
                except OSError:
                    pass  # Keep original name; non-fatal

    result_dir = app.results_dir / model_id / api_slug
    result_dir.mkdir(parents=True, exist_ok=True)
    out_file = result_dir / f"{task.task_id}.json"
    trace_for_stdout = extract_proxy_trace_incremental(proxy_dir)
    adapter_stdout_saved = json.dumps(trace_for_stdout, ensure_ascii=False, indent=2)

    if hooks and callable(getattr(hooks, "cleanup_runtime", None)):
        hooks.cleanup_runtime({"task": task, "sandbox": sandbox, "workspace": workspace}, runtime_state)

    workspace_kept_final = keep_workspace
    if not keep_workspace:
        shutil.rmtree(sandbox, ignore_errors=True)
        workspace_kept_final = False

    runtime_state_json = {k: v for k, v in runtime_state.items() if isinstance(v, (str, int, float, bool))}
    payload = {
        "task_id": task.task_id,
        "model_id": model_id,
        "api_model_slug": api_slug,
        "api_model_label": api_label,
        "mode": mode,
        "sandbox": str(sandbox),
        "workspace": str(workspace),
        "session_id": session_id,
        "prompt_file": str(prompt_file),
        "adapter_result": {
            "ok": adapter_result.ok,
            "command": adapter_result.command,
            "stdout": adapter_stdout_saved,
            "stderr": adapter_result.stderr,
            "metadata": adapter_result.metadata,
        },
        "adapter_results": [
            {
                "ok": item.ok,
                "command": item.command,
                "stdout": item.stdout,
                "stderr": item.stderr,
                "metadata": item.metadata,
            }
            for item in adapter_results
        ],
        "usage_summary": usage_summary,
        "oracle_result": oracle_result,
        "scoring": scoring,
        "runtime_state": runtime_state_json,
    }
    elapsed_sec = round(time.perf_counter() - t_run_start, 3)
    payload["elapsed_sec"] = elapsed_sec
    out_file.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")

    result = TaskRunResult(
        task_id=task.task_id,
        model_id=model_id,
        api_model_slug=api_slug,
        api_model_label=api_label,
        mode=mode,
        sandbox=sandbox,
        workspace=workspace,
        session_id=session_id,
        prompt_file=prompt_file,
        adapter_result=adapter_result,
        adapter_results=adapter_results,
        oracle_result=oracle_result,
        workspace_kept=workspace_kept_final,
        elapsed_sec=elapsed_sec,
        usage_summary=usage_summary,
        runtime_state=runtime_state_json,
        scoring=scoring,
    )

    return result
