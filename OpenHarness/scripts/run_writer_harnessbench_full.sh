#!/usr/bin/env bash
set -euo pipefail

check_only=0
if [[ "${1:-}" == "--check-only" ]]; then
  check_only=1
  shift
fi

workspace_root="${1:-/home/patton/projects/harness}"
env_file="${2:-${workspace_root}/tau3-bench/.env}"
result_root="${3:-${workspace_root}/results/runs/writer-full/.repeat-set-01}"
openharness_root="${workspace_root}/OpenHarness"
harnessbench_root="${workspace_root}/HarnessBench"
python_bin="${openharness_root}/.venv/bin/python"
task_manifest="${workspace_root}/results/config/harnessbench-writer-full-v1.tasks.json"
output_dir="${result_root}/HarnessBench"
model="qwen3.6-plus"

if [[ ! -x "${python_bin}" ]]; then
  echo "OpenHarness virtualenv Python not found: ${python_bin}" >&2
  exit 20
fi

mkdir -p "${result_root}"
cd "${openharness_root}"
"${python_bin}" scripts/summarize_writer_full_results.py preflight \
  --workspace-root "${workspace_root}" \
  --env-file "${env_file}" \
  --result-root "${result_root}" \
  --model "${model}" \
  --output "${result_root}/preflight.json"

if (( check_only )); then
  exit 0
fi

resume_args=()
if [[ -f "${output_dir}/run-config.json" ]]; then
  resume_args+=(--resume)
fi

"${python_bin}" scripts/run_openharness_harnessbench.py run \
  --harnessbench-root "${harnessbench_root}" \
  --task-manifest "${task_manifest}" \
  --model "${model}" \
  --profile qwen \
  --api-format openai \
  --env-file "${env_file}" \
  --grading full \
  --rubric-model "${model}" \
  --rubric-vision-model "${model}" \
  --rubric-base-url-env OPENAI_API_BASE \
  --rubric-api-key-env OPENAI_API_KEY \
  --public-url-mode loopback \
  --temperature 0 \
  --seed 42 \
  --max-turns 80 \
  --api-timeout-sec 300 \
  --repeats 1 \
  --openharness-mode writer_harness \
  --writer-workspace-root "${workspace_root}" \
  --writer-archive "${workspace_root}/writer_harness_demo.zip" \
  --writer-model "${model}" \
  --writer-max-tokens 4096 \
  --output-dir "${output_dir}" \
  "${resume_args[@]}"
