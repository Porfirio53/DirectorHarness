#!/usr/bin/env bash
set -euo pipefail

check_only=0
if [[ "${1:-}" == "--check-only" ]]; then
  check_only=1
  shift
fi

workspace_root="${1:-/home/patton/projects/harness}"
env_file="${2:-${workspace_root}/.env}"
default_run_id="$(date -u +%Y%m%dT%H%M%SZ)-group-writer-v1"
result_root="${3:-${workspace_root}/results/runs/writer-director-full/${default_run_id}}"
openharness_root="${workspace_root}/OpenHarness"

if (( check_only )); then
  bash "${openharness_root}/scripts/run_writer_director_mcp_full.sh" \
    --check-only "${workspace_root}" "${env_file}" "${result_root}"
  bash "${openharness_root}/scripts/run_writer_director_harnessbench_full.sh" \
    --check-only "${workspace_root}" "${env_file}" "${result_root}"
  exit 0
fi

bash "${openharness_root}/scripts/run_writer_director_mcp_full.sh" \
  "${workspace_root}" "${env_file}" "${result_root}"
bash "${openharness_root}/scripts/run_writer_director_harnessbench_full.sh" \
  "${workspace_root}" "${env_file}" "${result_root}"
bash "${openharness_root}/scripts/score_writer_director_full_results.sh" \
  "${workspace_root}" "${env_file}" "${result_root}"

echo "Writer+Director full result root: ${result_root}"
