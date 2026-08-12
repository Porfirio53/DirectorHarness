"""Oracle-side ``quality`` (0–1) via rubric_llm Chat Completions (same credentials as proxy trace grading)."""
from __future__ import annotations

import os
from pathlib import Path
from typing import Any

from harnessbench.grading.rubric_llm import run_llm_rubric


def oracle_openclaw_config_path(cfg: Path | None = None) -> Path | None:
    if cfg is not None:
        return cfg
    raw = os.environ.get("HARNESSBENCH_OPENCLAW_CONFIG", "").strip()
    return Path(raw).expanduser() if raw else None


def run_oracle_quality_llm(
    *,
    system: str,
    user: str | list[dict[str, Any]],
    openclaw_config: Path | None = None,
    timeout_sec: int = 240,
) -> tuple[float | None, dict[str, Any]]:
    """
    Invoke the rubric chat model expecting JSON with top-level ``quality`` in [0,1].
    Honors ``HARNESSBENCH_SKIP_ORACLE_QUALITY_LLM=1`` → returns ``(None, {skipped})``.
    """
    if os.environ.get("HARNESSBENCH_SKIP_ORACLE_QUALITY_LLM", "").strip().lower() in (
        "1",
        "true",
        "yes",
    ):
        return None, {
            "skipped": True,
            "reason": "HARNESSBENCH_SKIP_ORACLE_QUALITY_LLM",
        }

    out = run_llm_rubric(
        system=system,
        user=user,
        openclaw_config=oracle_openclaw_config_path(openclaw_config),
        timeout_sec=timeout_sec,
    )

    slim: dict[str, Any] = {
        "skipped": out.get("skipped"),
        "parse_error": out.get("parse_error"),
        "reason": out.get("reason"),
        "notes": out.get("notes"),
        "rubric_model": out.get("rubric_model"),
        "quality": out.get("quality"),
    }

    if out.get("skipped") or out.get("parse_error"):
        return None, slim

    pv = out.get("quality")
    if isinstance(pv, (int, float)):
        v = max(0.0, min(1.0, float(pv)))
        slim["quality"] = v
        return round(v, 4), slim

    slim["notes"] = (slim.get("notes") or "") + "; missing numeric quality in model JSON"
    return None, slim
