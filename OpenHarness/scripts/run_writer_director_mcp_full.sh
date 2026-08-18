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
mcp_root="${workspace_root}/MCP-Persona"
python_bin="${openharness_root}/.venv/bin/python"
director_catalog="${workspace_root}/director_harness/director_mcp_catalog.json"
output_dir="${result_root}/MCP-Persona"
model="qwen3.6-plus"
task_ids=(
  1 2 4 5 8 9 10 13 14 18 19 21 23 24 25 26 28 29 30 34 36 45 47
  49 50 51 53 54 56 62 65 66 69 70 86 92 93 106 110 125 131 132 133
  135 138 141 148 152 156 157 161 173
)

if [[ ! -x "${python_bin}" ]]; then
  echo "OpenHarness virtualenv Python not found: ${python_bin}" >&2
  exit 20
fi

mkdir -p "${result_root}"
cd "${openharness_root}"
"${python_bin}" scripts/check_writer_director_full.py \
  --workspace-root "${workspace_root}" \
  --env-file "${env_file}" \
  --result-root "${result_root}" \
  --model "${model}" \
  --repeats 2 \
  --output "${result_root}/preflight.json"

if (( check_only )); then
  exit 0
fi

resume_args=()
if [[ -f "${output_dir}/run-config.json" ]]; then
  resume_args+=(--resume)
fi

"${python_bin}" scripts/run_mcp_persona_week1.py \
  --mcp-persona-root "${mcp_root}" \
  --output-dir "${output_dir}" \
  --language en \
  --mode full \
  --task-ids "${task_ids[@]}" \
  --repeats 2 \
  --experiment-stage writer-director-full \
  --openharness-mode writer_harness \
  --writer-workspace-root "${workspace_root}" \
  --writer-archive "${workspace_root}/docs/writer_director_0812.zip" \
  --writer-model "${model}" \
  --writer-max-tokens 4096 \
  --director-harness-enabled \
  --director-mcp-catalog "${director_catalog}" \
  --model "${model}" \
  --env-file "${env_file}" \
  --tool-scope server \
  --no-chain-guidance \
  --temperature 0 \
  --seed 42 \
  --max-turns 30 \
  --max-tokens 4096 \
  "${resume_args[@]}"
