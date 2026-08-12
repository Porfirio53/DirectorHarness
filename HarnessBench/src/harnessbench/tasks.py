from __future__ import annotations

import importlib.util
import json
import os
from pathlib import Path
from typing import Any

from harnessbench.grading.oracle_quality_layer import merge_oracle_quality
from harnessbench.models import TaskSpec


def load_tasks(tasks_dir: Path) -> dict[str, TaskSpec]:
    out: dict[str, TaskSpec] = {}
    if not tasks_dir.is_dir():
        return out
    for child in sorted(tasks_dir.iterdir()):
        if not child.is_dir() or child.name.startswith("."):
            continue
        task_yaml = child / "task.yaml"
        if not task_yaml.is_file():
            continue
        raw = task_yaml.read_text(encoding="utf-8")
        try:
            data = json.loads(raw)
        except json.JSONDecodeError:
            try:
                import yaml  # type: ignore
            except ModuleNotFoundError as exc:
                raise RuntimeError(f"{task_yaml} is not valid JSON and PyYAML is not installed") from exc
            data = yaml.safe_load(raw) or {}
        spec = TaskSpec(
            task_id=str(data["task_id"]),
            title=str(data.get("title", data["task_id"])),
            prompt_file=str(data.get("prompt_file", "prompt.txt")),
            prompt_files=[str(x) for x in (data.get("prompt_files") or [])],
            fixtures_dir=str(data.get("fixtures_dir", "fixtures")),
            oracle_module=str(data.get("oracle_module", "oracle_grade.py")),
            hooks_module=str(data.get("hooks_module", "hooks.py")),
            timeout_sec=int(data.get("timeout_sec", 600)),
            tags=list(data.get("tags", []) or []),
            task_dir=child.resolve(),
        )
        out[spec.task_id] = spec
    return out


def _load_module(file_path: Path, module_name: str) -> Any | None:
    if not file_path.is_file():
        return None
    spec = importlib.util.spec_from_file_location(module_name, file_path)
    if spec is None or spec.loader is None:
        return None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def load_hooks(task: TaskSpec) -> Any | None:
    assert task.task_dir is not None
    return _load_module(task.task_dir / task.hooks_module, f"hooks_{task.task_id.replace('-', '_')}")


def run_oracle(task: TaskSpec, workspace: Path) -> dict[str, Any]:
    """Load ``score_workspace(workspace)`` from the task oracle module.

    Expected keys (harness uses): ``outcome_score`` (float); optional ``quality`` (float 0–1), often from
    **``rubric_llm``** inside ``oracle_grade`` (same Chat Completions API as proxy trace grading), plus optional
    ``quality_rubric_meta`` for debugging.

    Optional ``outcome_llm_weight`` (float in [0,1]): **quality** blend weight ``w`` in
    ``(1-w)*outcome_score + w*quality``; overrides ``HARNESSBENCH_OUTCOME_LLM_WEIGHT``.
    If the oracle omits ``outcome_llm_weight``, the harness applies defaults from
    ``grading/task_outcome_llm_weights.py`` (**0.9** for **08** / **13** only; **0.0** elsewhere).
    Generic text ``quality`` LLM runs only when ``w > 0`` and ``quality`` is still missing (today: vision tasks).
    """
    assert task.task_dir is not None
    module = _load_module(task.task_dir / task.oracle_module, f"oracle_{task.task_id.replace('-', '_')}")
    if module is None:
        return {
            "task": task.task_id,
            "workspace": str(workspace),
            "outcome_score": 0.0,
            "error": f"missing oracle module: {task.oracle_module}",
        }
    fn = getattr(module, "score_workspace", None)
    if not callable(fn):
        return {
            "task": task.task_id,
            "workspace": str(workspace),
            "outcome_score": 0.0,
            "error": "oracle module missing score_workspace(workspace)",
        }
    raw = dict(fn(workspace))
    cfg_oc: Path | None = None
    env_cfg = os.environ.get("HARNESSBENCH_OPENCLAW_CONFIG", "").strip()
    if env_cfg:
        cfg_oc = Path(env_cfg).expanduser()
    return merge_oracle_quality(task.task_id, task.task_dir, workspace, raw, openclaw_config=cfg_oc)
