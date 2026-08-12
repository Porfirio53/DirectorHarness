#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
OPENHARNESS_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
cd "$OPENHARNESS_ROOT"

PYTHON_BIN="${OPENHARNESS_PYTHON:-$OPENHARNESS_ROOT/.venv/bin/python}"
HARNESSBENCH_ROOT="${HARNESSBENCH_ROOT:-$OPENHARNESS_ROOT/../HarnessBench}"
TASK_MANIFEST="${HARNESSBENCH_TASK_MANIFEST:-$OPENHARNESS_ROOT/../results/config/harnessbench-original-full.tasks.json}"
ENV_FILE="${HARNESSBENCH_ENV_FILE:-$OPENHARNESS_ROOT/../tau3-bench/.env}"
OUTPUT_DIR="${HARNESSBENCH_OUTPUT_DIR:-$OPENHARNESS_ROOT/../results/runs/original-full/HarnessBench}"
MODEL="${HARNESSBENCH_MODEL:-qwen3.6-plus}"
PROFILE="${HARNESSBENCH_PROFILE:-qwen}"
RUBRIC_MODEL="${HARNESSBENCH_RUBRIC_MODEL:-$MODEL}"
RUBRIC_VISION_MODEL="${HARNESSBENCH_RUBRIC_VISION_MODEL:-$RUBRIC_MODEL}"
REPEATS="${HARNESSBENCH_REPEATS:-2}"

COMMON_ARGS=(
  --harnessbench-root "$HARNESSBENCH_ROOT"
  --task-manifest "$TASK_MANIFEST"
  --model "$MODEL"
  --profile "$PROFILE"
  --api-format openai
  --temperature 0
  --seed 42
  --env-file "$ENV_FILE"
  --max-turns 80
  --api-timeout-sec 300
  --grading full
  --rubric-model "$RUBRIC_MODEL"
  --rubric-vision-model "$RUBRIC_VISION_MODEL"
  --rubric-base-url-env OPENAI_API_BASE
  --rubric-api-key-env OPENAI_API_KEY
  --public-url-mode loopback
  --openharness-mode original
)

"$PYTHON_BIN" scripts/run_openharness_harnessbench.py preflight "${COMMON_ARGS[@]}"

run_rc=0
"$PYTHON_BIN" scripts/run_openharness_harnessbench.py run \
  "${COMMON_ARGS[@]}" \
  --repeats "$REPEATS" \
  --resume \
  --output-dir "$OUTPUT_DIR" || run_rc=$?

"$PYTHON_BIN" scripts/redact_harnessbench_artifacts.py \
  --env-file "$ENV_FILE" \
  --root "$OUTPUT_DIR"

summary_rc=0
"$PYTHON_BIN" scripts/run_openharness_harnessbench.py summarize \
  --output-dir "$OUTPUT_DIR" || summary_rc=$?

if (( run_rc != 0 )); then
  exit "$run_rc"
fi
exit "$summary_rc"
