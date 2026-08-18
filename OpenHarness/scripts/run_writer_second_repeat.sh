#!/usr/bin/env bash
set -euo pipefail

check_only=0
if [[ "${1:-}" == "--check-only" ]]; then
  check_only=1
  shift
fi

workspace_root="${1:-/home/patton/projects/harness}"
env_file="${2:-${workspace_root}/.env}"
first_result_root="${3:-${workspace_root}/results/runs/writer-full/.repeat-set-01}"
second_result_root="${4:-${workspace_root}/results/runs/writer-full/.repeat-set-02}"
merged_result_root="${5:-${workspace_root}/results/runs/writer-full/results_with_Writer}"
openharness_root="${workspace_root}/OpenHarness"
python_bin="${openharness_root}/.venv/bin/python"

if [[ ! -x "${python_bin}" ]]; then
  echo "OpenHarness virtualenv Python not found: ${python_bin}" >&2
  exit 20
fi
if [[ ! -f "${env_file}" ]]; then
  echo "Model environment file not found: ${env_file}" >&2
  exit 20
fi

cd "${openharness_root}"
"${python_bin}" scripts/merge_writer_full_repeats.py \
  --first-result-root "${first_result_root}" \
  --second-result-root "${second_result_root}" \
  --check-only

"${python_bin}" scripts/summarize_writer_full_results.py preflight \
  --workspace-root "${workspace_root}" \
  --env-file "${env_file}" \
  --result-root "${second_result_root}" \
  --model qwen3.6-plus

if (( check_only )); then
  exit 0
fi

bash scripts/run_writer_mcp_full.sh \
  "${workspace_root}" \
  "${env_file}" \
  "${second_result_root}"

bash scripts/run_writer_harnessbench_full.sh \
  "${workspace_root}" \
  "${env_file}" \
  "${second_result_root}"

"${python_bin}" scripts/merge_writer_full_repeats.py \
  --first-result-root "${first_result_root}" \
  --second-result-root "${second_result_root}" \
  --output-root "${merged_result_root}"

echo "Second repeat: ${second_result_root}"
echo "Merged two-repeat results: ${merged_result_root}"
echo "Next: bash ${openharness_root}/scripts/score_writer_full_results.sh ${workspace_root} ${env_file} ${merged_result_root}"
