"""OpenAI-compatible Chat Completions for rubric. Env: RUBRIC_API_KEY, RUBRIC_BASE_URL, RUBRIC_MODEL; optional OPENCLAW_USER_CONFIG."""
from __future__ import annotations

import base64
import json
import os
import re
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

from harnessbench.grading.task_outcome_llm_weights import outcome_llm_weight_for_task

_WORKSPACE_OUT_TEXT_SUFFIXES = frozenset(
    {".txt", ".md", ".csv", ".json", ".yaml", ".yml", ".html", ".htm", ".xml"}
)


def read_text_file_capped(path: Path, limit: int) -> str | None:
    try:
        raw = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None
    if len(raw) > limit:
        return raw[:limit] + "\n…[truncated]"
    return raw


def collect_out_dir_text_snippets(
    workspace: Path,
    *,
    max_files: int = 14,
    per_file_cap: int = 2600,
) -> str:
    """Read text-ish files under workspace/out for rubric context (oracle quality + process rubric)."""
    out_dir = workspace.resolve() / "out"
    if not out_dir.is_dir():
        return "(no workspace/out directory)"
    parts: list[str] = []
    n = 0
    w = workspace.resolve()
    for p in sorted(out_dir.rglob("*")):
        if not p.is_file() or n >= max_files:
            break
        if p.suffix.lower() not in _WORKSPACE_OUT_TEXT_SUFFIXES:
            continue
        rel = p.relative_to(w)
        body = read_text_file_capped(p, per_file_cap)
        if body is None:
            continue
        parts.append(f"--- {rel.as_posix()} ---\n{body}")
        n += 1
    return "\n\n".join(parts) if parts else "(no readable text artifacts under out/)"


def append_workspace_out_text_excerpts_for_process_rubric(
    task_id: str,
    workspace: Path,
    user_content: str | list[dict[str, Any]],
    *,
    effective_outcome_llm_weight: float | None = None,
) -> str | list[dict[str, Any]]:
    """Attach ``out/`` text excerpts to proxy process-rubric ``user`` when outcome blend weight is non-zero.

    Uses ``effective_outcome_llm_weight`` when provided (typical: ``oracle_result`` after ``merge_oracle_quality``).
    Otherwise falls back to ``outcome_llm_weight_for_task(task_id)``.

    Tasks with ``w == 0`` skip; only non-zero defaults today are vision tasks (**0.9**), which attach.

    Vision/multimodal user content (list) gets an extra trailing text part.
    """
    w = (
        float(effective_outcome_llm_weight)
        if isinstance(effective_outcome_llm_weight, (int, float))
        else outcome_llm_weight_for_task(task_id)
    )
    w = max(0.0, min(1.0, w))
    if w <= 0.0:
        return user_content
    snippets = collect_out_dir_text_snippets(workspace)
    block = (
        "\n\n--- WORKSPACE OUTPUT EXCERPTS (text under ``out/``; judge process vs deliverables) ---\n"
        + snippets
    )
    if isinstance(user_content, list):
        return [*user_content, {"type": "text", "text": block}]
    return user_content + block


def _resolve_api_key_ref(value: str) -> str | None:
    v = value.strip()
    if v.startswith("${") and v.endswith("}"):
        return os.environ.get(v[2:-1]) or None
    return v or None


def load_openclaw_chat_credentials(path: Path) -> tuple[str | None, str | None, str | None]:
    try:
        raw = path.read_text(encoding="utf-8")
        cfg = json.loads(raw)
    except (OSError, json.JSONDecodeError):
        return None, None, None

    defaults = cfg.get("agents", {}).get("defaults")
    if not isinstance(defaults, dict):
        defaults = {}

    rubric_ref = defaults.get("rubricModel")
    primary = (
        defaults.get("model", {}).get("primary")
        if isinstance(defaults.get("model"), dict)
        else None
    )
    ref = rubric_ref if isinstance(rubric_ref, str) and "/" in rubric_ref else primary
    if not isinstance(ref, str) or "/" not in ref:
        return None, None, None

    prov_id, model_id = ref.split("/", 1)
    providers = cfg.get("models", {}).get("providers") or {}
    prov = providers.get(prov_id)
    if not isinstance(prov, dict):
        return None, None, None

    bu = prov.get("baseUrl")
    base = bu.rstrip("/") if isinstance(bu, str) else None
    ak = prov.get("apiKey")
    key = _resolve_api_key_ref(ak) if isinstance(ak, str) else None
    return key, base, model_id


def _default_openclaw_config_path() -> Path | None:
    p = os.environ.get("OPENCLAW_USER_CONFIG", "").strip()
    if p:
        pp = Path(p).expanduser()
        if pp.is_file():
            return pp
    home = Path(os.environ.get("HOME", str(Path.home())))
    oc = home / ".openclaw" / "openclaw.json"
    return oc if oc.is_file() else None


def build_workspace_image_attachment(
    workspace: Path,
    relative_paths: list[str],
    user_text: str,
    *,
    max_b: int | None = None,
) -> str | list[dict[str, Any]]:
    """
    Prefix ``user_text`` with multimodal image parts resolved under ``workspace`` (relative posix paths).

    Missing files are silently skipped if none exist returns plain ``user_text``; if text + no images remain, returns ``user_text``.
    """
    mb = (
        max_b
        if max_b is not None
        else int(os.environ.get("HARNESSBENCH_RUBRIC_MAX_IMAGE_BYTES", "4194304"))
    )
    parts: list[dict[str, Any]] = [{"type": "text", "text": user_text}]
    skipped: list[str] = []
    w = workspace.resolve()
    for rel in relative_paths:
        p = w / Path(rel.replace("\\", "/"))
        if not p.is_file():
            continue
        raw = p.read_bytes()
        if len(raw) > mb:
            skipped.append(f"{rel}({len(raw)}B)")
            continue
        suf = p.suffix.lower()
        mime = "image/png" if suf == ".png" else ("image/jpeg" if suf in (".jpg", ".jpeg") else "image/png")
        b64 = base64.standard_b64encode(raw).decode("ascii")
        parts.append({"type": "image_url", "image_url": {"url": f"data:{mime};base64,{b64}"}})
    if skipped:
        parts[0]["text"] = (
            str(parts[0].get("text", ""))
            + "\n\n[Omitted images exceeding byte cap: "
            + ", ".join(skipped)
            + "]"
        )
    if len(parts) == 1:
        return str(parts[0]["text"])
    return parts


def build_rubric_user_content_for_task(task_id: str, user_text: str, workspace: Path) -> str | list[dict[str, Any]]:
    """
    ``013-image-edit``：proxy trace 文本后附加 ``out`` 产物图，供过程分阅卷模型审阅。
    """
    if task_id != "013-image-edit":
        return user_text
    return build_workspace_image_attachment(
        workspace,
        ["out/cat_styled.png", "out/cat_scene.png"],
        user_text,
    )


def _parse_json_object(text: str) -> dict[str, Any] | None:
    text = text.strip()
    m = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", text, re.DOTALL)
    if m:
        try:
            return json.loads(m.group(1))
        except json.JSONDecodeError:
            pass
    try:
        start = text.index("{")
        depth = 0
        for i, ch in enumerate(text[start:], start):
            if ch == "{":
                depth += 1
            elif ch == "}":
                depth -= 1
                if depth == 0:
                    return json.loads(text[start : i + 1])
    except (ValueError, json.JSONDecodeError):
        pass
    return None


def run_llm_rubric(
    *,
    system: str,
    user: str | list[dict[str, Any]],
    api_key: str | None = None,
    base_url: str | None = None,
    model: str | None = None,
    openclaw_config: Path | None = None,
    timeout_sec: int = 200,
) -> dict[str, Any]:
    ok: str | None = None
    ob: str | None = None
    om: str | None = None
    cfg_path = openclaw_config or _default_openclaw_config_path()
    if cfg_path is not None and cfg_path.is_file():
        ok, ob, om = load_openclaw_chat_credentials(cfg_path)

    key = (
        api_key
        or os.environ.get("RUBRIC_API_KEY")
        or os.environ.get("OPENAI_API_KEY")
        or ok
    )
    if not key:
        return {
            "skipped": True,
            "reason": "No RUBRIC_API_KEY, OPENAI_API_KEY, or openclaw.json apiKey",
            "scores": {},
            "total": None,
            "notes": "",
            "rubric_model": None,
        }

    base = (
        base_url
        or os.environ.get("RUBRIC_BASE_URL")
        or ob
        or "https://api.openai.com/v1"
    ).rstrip("/")
    mdl = model or os.environ.get("RUBRIC_MODEL") or om or "gpt-4o-mini"
    if isinstance(user, list):
        vm = os.environ.get("RUBRIC_VISION_MODEL", "").strip()
        if vm:
            mdl = vm

    payload = {
        "model": mdl,
        "messages": [
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ],
        "temperature": 0.2,
    }
    body = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        f"{base}/chat/completions",
        data=body,
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {key}",
        },
        method="POST",
    )
    effective_timeout = timeout_sec
    if isinstance(user, list):
        effective_timeout = max(
            timeout_sec,
            int(os.environ.get("HARNESSBENCH_RUBRIC_VISION_TIMEOUT_SEC", "300")),
        )
    try:
        with urllib.request.urlopen(req, timeout=effective_timeout) as resp:
            data = json.loads(resp.read().decode("utf-8"))
    except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as e:
        return {
            "skipped": True,
            "reason": str(e),
            "scores": {},
            "total": None,
            "notes": "",
            "rubric_model": mdl,
        }

    try:
        content = data["choices"][0]["message"]["content"]
    except (KeyError, IndexError, TypeError):
        return {
            "skipped": True,
            "reason": "unexpected API response shape",
            "raw": data,
            "scores": {},
            "total": None,
            "notes": "",
            "rubric_model": mdl,
        }

    parsed = _parse_json_object(content) if isinstance(content, str) else None
    if not parsed:
        return {
            "skipped": False,
            "parse_error": True,
            "raw_content": content[:2000] if isinstance(content, str) else "",
            "scores": {},
            "total": None,
            "notes": "Failed to parse JSON from model output",
            "rubric_model": mdl,
        }

    scores = parsed.get("scores") if isinstance(parsed.get("scores"), dict) else {}
    total = parsed.get("total")
    notes = str(parsed.get("notes", ""))
    out: dict[str, object] = {
        "skipped": False,
        "parse_error": False,
        "scores": scores,
        "total": float(total) if total is not None else None,
        "notes": notes,
        "raw_content": content[:1500] if isinstance(content, str) else "",
        "rubric_model": mdl,
    }
    vb = parsed.get("vision_breakdown")
    if isinstance(vb, dict):
        out["vision_breakdown"] = vb
    # Security gate: 1 = pass, 0 = fail; severe_security_violation overrides to 0.
    if parsed.get("severe_security_violation") is True:
        out["security_score"] = 0.0
    else:
        sg = parsed.get("security_gate")
        if isinstance(sg, bool):
            out["security_score"] = 1.0 if sg else 0.0
        elif isinstance(sg, (int, float)):
            out["security_score"] = 1.0 if float(sg) >= 0.5 else 0.0
    qv = parsed.get("quality")
    if isinstance(qv, (int, float)):
        out["quality"] = max(0.0, min(1.0, float(qv)))
    return out
