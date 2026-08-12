from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

from harnessbench.models import AppConfig


def resolve_project_root() -> Path:
    return Path(__file__).resolve().parents[2]


def _load_yaml(path: Path) -> dict[str, Any]:
    if not path.is_file():
        return {}
    text = path.read_text(encoding="utf-8")
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        try:
            import yaml  # type: ignore
        except ModuleNotFoundError as exc:
            raise RuntimeError(f"{path} is not valid JSON and PyYAML is not installed") from exc
        return yaml.safe_load(text) or {}


def _expand_path(raw: str | Path, root: Path) -> Path:
    p = Path(os.path.expanduser(str(raw)))
    if not p.is_absolute():
        p = root / p
    return p.resolve()


def load_app_config(path: str | Path | None = None) -> AppConfig:
    root = resolve_project_root()
    cfg_path = _expand_path(
        path
        or os.getenv("HARNESSBENCH_APP_CONFIG")
        or "config/app.yaml",
        root,
    )
    data = _load_yaml(cfg_path)
    cfg = AppConfig(
        project_root=root,
        data_dir=_expand_path(data.get("data_dir", "data"), root),
        tasks_dir=_expand_path(data.get("tasks_dir", "tasks"), root),
        results_dir=_expand_path(data.get("results_dir", "data/results"), root),
        work_root=_expand_path(data.get("work_root", "data/workspace"), root),
        default_timeout_sec=int(data.get("default_timeout_sec", 600)),
        default_rounds=int(data.get("default_rounds", 1)),
    )
    cfg.data_dir.mkdir(parents=True, exist_ok=True)
    cfg.results_dir.mkdir(parents=True, exist_ok=True)
    cfg.work_root.mkdir(parents=True, exist_ok=True)
    return cfg


def load_model_config(path: str | Path | None = None) -> dict[str, dict[str, Any]]:
    root = resolve_project_root()
    harness_user = root / "config/harness.yaml"
    models_user = root / "config/models.yaml"
    if harness_user.is_file():
        default_cfg = "config/harness.yaml"
    elif models_user.is_file():
        default_cfg = "config/models.yaml"  # legacy path
    else:
        default_cfg = "config/harness.example.yaml"

    cfg_path = _expand_path(
        path
        or os.getenv("HARNESSBENCH_HARNESS_CONFIG")
        or os.getenv("HARNESSBENCH_MODELS_CONFIG")  # legacy env
        or default_cfg,
        root,
    )
    data = _load_yaml(cfg_path)
    models = data.get("models", {})
    if not isinstance(models, dict):
        return {}
    return {str(k): dict(v or {}) for k, v in models.items()}
