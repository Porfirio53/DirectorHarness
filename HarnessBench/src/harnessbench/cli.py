from __future__ import annotations

import argparse
import json
import re
import time
import traceback

from harnessbench.config import load_app_config, load_model_config
from harnessbench.runner import run_task
from harnessbench.tasks import load_tasks

_TASK_LEADING_NUM = re.compile(r"^(\d+)-")


def _task_leading_number(task_id: str) -> int | None:
    m = _TASK_LEADING_NUM.match(task_id)
    return int(m.group(1)) if m else None


def _task_id_sort_key(task_id: str) -> tuple[int, str]:
    """按 task_id 前导数字排序（如 9-foo 在 10-bar 之前）；无数字前缀的排在最后。"""
    m = _TASK_LEADING_NUM.match(task_id)
    if m:
        return (int(m.group(1)), task_id)
    return (10**9, task_id)


def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="harnessbench")
    sub = p.add_subparsers(dest="cmd", required=True)

    sub.add_parser("tasks")

    run_task_p = sub.add_parser("run-task")
    run_task_id = run_task_p.add_mutually_exclusive_group(required=True)
    run_task_id.add_argument(
        "--task",
        default=None,
        metavar="TASK_ID",
        help="完整题目 id（如 040-test-coverage-fill）。",
    )
    run_task_id.add_argument(
        "--num",
        type=int,
        default=None,
        metavar="N",
        help="仅按 task_id 前导数字选题（须唯一）；与 --task 二选一。",
    )
    run_task_p.add_argument(
        "--harness",
        required=True,
        metavar="ID",
        help="Bench 配置条目 id（`models:` 下的键名，见 config/harness.yaml / harness.example.yaml）。",
    )
    run_task_p.add_argument("--mode", default="live")
    run_task_p.add_argument("--delete-sandbox", action="store_true")

    run_suite_p = sub.add_parser("run-suite")
    run_suite_p.add_argument(
        "--harness",
        required=True,
        metavar="ID",
        help="Bench 配置条目 id（`models:` 下的键名，见 config/harness.yaml / harness.example.yaml）。",
    )
    run_suite_p.add_argument("--mode", default="live")
    run_suite_p.add_argument("--delete-sandbox", action="store_true")
    run_suite_p.add_argument(
        "--from-task",
        metavar="TASK_ID",
        default=None,
        help="从该 task_id 起跑（含该项）；按 task_id 前导数字排序后的切片起点。",
    )
    run_suite_p.add_argument(
        "--to-task",
        metavar="TASK_ID",
        default=None,
        help="跑到该 task_id 为止（含该项）；须与 --from-task 同为（前导数字排序后）列表中的下标区间。可单独使用表示从第一题跑到该题。",
    )
    run_suite_p.add_argument(
        "--from-num",
        type=int,
        default=None,
        metavar="N",
        help="按题号前导数字筛选：包含 task_id 以 N- 开头且数字 ∈ [from-num,to-num] 的题目（与 --to-num 联用；勿与 --from-task/--to-task 同时使用）。仅给 --to-num 时默认从 1 开始。",
    )
    run_suite_p.add_argument(
        "--to-num",
        type=int,
        default=None,
        metavar="N",
        help="按题号前导数字筛选上界（含）。仅给 --from-num 时默认跑到当前题库最大题号。",
    )
    return p


def _slice_suite_task_ids(
    all_ids: list[str],
    tasks: dict,
    *,
    from_task: str | None,
    to_task: str | None,
) -> list[str]:
    """按前导数字排序后的 task_id 列表取闭区间 [from_task, to_task]（两端均含）。"""
    if not all_ids:
        return []
    start_i = 0
    end_i = len(all_ids) - 1
    if from_task is not None:
        ft = str(from_task).strip()
        if ft not in tasks:
            raise SystemExit(f"unknown --from-task: {ft!r} (not in loaded tasks)")
        start_i = all_ids.index(ft)
    if to_task is not None:
        tt = str(to_task).strip()
        if tt not in tasks:
            raise SystemExit(f"unknown --to-task: {tt!r} (not in loaded tasks)")
        end_i = all_ids.index(tt)
    if start_i > end_i:
        raise SystemExit(
            f"invalid range: --from-task {all_ids[start_i]!r} comes after --to-task {all_ids[end_i]!r} in sorted order"
        )
    return all_ids[start_i : end_i + 1]


def _suite_task_ids_by_num_range(all_ids: list[str], *, lo: int, hi: int) -> list[str]:
    """保留排序顺序，仅保留 task_id 前导数字 ∈ [lo, hi] 的题目。"""
    out: list[str] = []
    for tid in all_ids:
        n = _task_leading_number(tid)
        if n is not None and lo <= n <= hi:
            out.append(tid)
    return out


def _task_ids_with_leading_number(task_keys: list[str], n: int) -> list[str]:
    """题库中 task_id 前导数字等于 n 的全部题目（按排序键有序）。"""
    return [tid for tid in sorted(task_keys, key=_task_id_sort_key) if _task_leading_number(tid) == n]


def _resolve_run_task_id(tasks: dict, *, task: str | None, num: int | None) -> str:
    """run-task：`--task` 或 `--num` 解析为唯一 task_id。"""
    if task is not None:
        tid = str(task).strip()
        if tid not in tasks:
            raise SystemExit(f"unknown task: {tid!r}")
        return tid
    if num is None:
        raise SystemExit("run-task: internal error (neither --task nor --num)")
    matches = _task_ids_with_leading_number(list(tasks.keys()), num)
    if not matches:
        raise SystemExit(f"run-task: no task with leading number {num}")
    if len(matches) > 1:
        raise SystemExit(
            f"run-task: ambiguous --num {num}: multiple tasks {matches!r}; use --task <task_id>"
        )
    return matches[0]


def main() -> int:
    args = _build_parser().parse_args()
    app_cfg = load_app_config()
    model_cfgs = load_model_config()
    tasks = load_tasks(app_cfg.tasks_dir)

    if args.cmd == "tasks":
        ordered = sorted(tasks.items(), key=lambda kv: _task_id_sort_key(kv[0]))
        print(json.dumps({k: {"title": v.title, "tags": v.tags} for k, v in ordered}, ensure_ascii=False, indent=2))
        return 0

    if args.harness not in model_cfgs:
        raise SystemExit(f"unknown harness config: {args.harness!r}")
    model_cfg = model_cfgs[args.harness]

    if args.cmd == "run-task":
        task_id = _resolve_run_task_id(tasks, task=args.task, num=args.num)
        print(f"[harnessbench] run-task {task_id} (harness={args.harness}, mode={args.mode}) ...", flush=True)
        result = run_task(app_cfg, tasks[task_id], args.harness, model_cfg, args.mode, keep_workspace=not args.delete_sandbox)
        elapsed_sec = result.elapsed_sec
        ok = getattr(result.adapter_result, "ok", False)
        print(f"[harnessbench] run-task {task_id} finished adapter_ok={ok} elapsed={elapsed_sec}s", flush=True)
        print(
            json.dumps(
                {
                    "task_id": result.task_id,
                    "elapsed_sec": elapsed_sec,
                    "api_model_slug": result.api_model_slug,
                    "api_model_label": result.api_model_label,
                    "usage_summary": result.usage_summary,
                    "oracle_result": result.oracle_result,
                    "scoring": result.scoring,
                    "sandbox": str(result.sandbox),
                },
                ensure_ascii=False,
                indent=2,
            )
        )
        return 0

    if args.cmd == "run-suite":
        outputs = []
        had_failures = False
        all_ids = sorted(tasks, key=_task_id_sort_key)
        from_raw = getattr(args, "from_task", None)
        to_raw = getattr(args, "to_task", None)
        from_num = getattr(args, "from_num", None)
        to_num = getattr(args, "to_num", None)
        num_mode = from_num is not None or to_num is not None
        if num_mode:
            if from_raw is not None or to_raw is not None:
                raise SystemExit(
                    "run-suite: choose either numeric range (--from-num / --to-num) "
                    "or task id endpoints (--from-task / --to-task), not both"
                )
            max_n = max((_task_leading_number(t) or -1) for t in all_ids)
            if max_n < 0:
                raise SystemExit("run-suite: no tasks with numeric task_id prefix")
            lo = int(from_num) if from_num is not None else 1
            hi = int(to_num) if to_num is not None else max_n
            if lo > hi:
                raise SystemExit(f"invalid --from-num/--to-num: {lo} > {hi}")
            task_ids = _suite_task_ids_by_num_range(all_ids, lo=lo, hi=hi)
            print(
                f"[harnessbench] run-suite numeric filter [{lo} .. {hi}]: {len(task_ids)} task(s) "
                f"of {len(all_ids)} total",
                flush=True,
            )
        else:
            task_ids = _slice_suite_task_ids(all_ids, tasks, from_task=from_raw, to_task=to_raw)
            if (from_raw or to_raw) and all_ids:
                fr = str(from_raw).strip() if from_raw else all_ids[0]
                to = str(to_raw).strip() if to_raw else all_ids[-1]
                print(
                    f"[harnessbench] run-suite range [{fr!r} .. {to!r}] (numeric order): {len(task_ids)} task(s) "
                    f"of {len(all_ids)} total",
                    flush=True,
                )
        total = len(task_ids)
        if total == 0:
            print("[harnessbench] run-suite: no tasks to run", flush=True)
            print(json.dumps([], ensure_ascii=False, indent=2))
            return 0
        suite_t0 = time.perf_counter()
        for idx, task_id in enumerate(task_ids, start=1):
            print(
                f"[harnessbench] run-suite [{idx}/{total}] {task_id} (harness={args.harness}, mode={args.mode}) ...",
                flush=True,
            )
            try:
                result = run_task(app_cfg, tasks[task_id], args.harness, model_cfg, args.mode, keep_workspace=not args.delete_sandbox)
            except KeyboardInterrupt:
                raise
            except Exception as exc:
                had_failures = True
                outputs.append(
                    {
                        "task_id": task_id,
                        "ok": False,
                        "error_type": type(exc).__name__,
                        "error": str(exc),
                        "traceback": traceback.format_exc(),
                    }
                )
                continue
            elapsed_sec = result.elapsed_sec
            ok = getattr(result.adapter_result, "ok", False)
            print(
                f"[harnessbench] run-suite [{idx}/{total}] {task_id} finished adapter_ok={ok} elapsed={elapsed_sec}s",
            )
            outputs.append(
                {
                    "task_id": result.task_id,
                    "ok": True,
                    "elapsed_sec": elapsed_sec,
                    "api_model_slug": result.api_model_slug,
                    "api_model_label": result.api_model_label,
                    "usage_summary": result.usage_summary,
                    "oracle_result": result.oracle_result,
                    "scoring": result.scoring,
                    "sandbox": str(result.sandbox),
                }
            )
        suite_elapsed_sec = round(time.perf_counter() - suite_t0, 3)
        print(
            f"[harnessbench] run-suite finished {total} tasks wall_elapsed={suite_elapsed_sec}s",
            flush=True,
        )
        print(json.dumps(outputs, ensure_ascii=False, indent=2))
        return 1 if had_failures else 0

    return 1


if __name__ == "__main__":
    raise SystemExit(main())
