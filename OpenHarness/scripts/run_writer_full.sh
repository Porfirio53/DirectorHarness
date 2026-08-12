#!/usr/bin/env bash
set -euo pipefail

workspace_root="${1:-/home/patton/projects/harness}"
env_file="${2:-${workspace_root}/tau3-bench/.env}"
run_root="${3:-${workspace_root}/results/runs/writer-full}"
first_result_root="${run_root}/.repeat-set-01"
second_result_root="${run_root}/.repeat-set-02"
merged_result_root="${run_root}/results_with_Writer"
openharness_root="${workspace_root}/OpenHarness"

bash "${openharness_root}/scripts/run_writer_mcp_full.sh" \
  "${workspace_root}" "${env_file}" "${first_result_root}"
bash "${openharness_root}/scripts/run_writer_harnessbench_full.sh" \
  "${workspace_root}" "${env_file}" "${first_result_root}"
bash "${openharness_root}/scripts/run_writer_second_repeat.sh" \
  "${workspace_root}" "${env_file}" "${first_result_root}" \
  "${second_result_root}" "${merged_result_root}"
bash "${openharness_root}/scripts/score_writer_full_results.sh" \
  "${workspace_root}" "${env_file}" "${merged_result_root}"
