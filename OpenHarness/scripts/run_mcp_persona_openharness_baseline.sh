#!/usr/bin/env bash
set -euo pipefail

workspace_root="${1:-/home/patton/projects/harness}"
env_file="${2:-${workspace_root}/tau3-bench/.env}"
output_dir="${3:-${workspace_root}/results/runs/original-full/MCP-Persona}"
openharness_root="${workspace_root}/OpenHarness"
mcp_root="${workspace_root}/MCP-Persona"
python_bin="${openharness_root}/.venv/bin/python"
task_ids=(
  1 2 4 5 8 9 10 13 14 18 19 21 23 24 25 26 28 29 30 34 36 45 47
  49 50 51 53 54 56 62 65 66 69 70 86 92 93 106 110 125 131 132 133
  135 138 141 148 152 156 157 161 173
)

if [[ ! -x "${python_bin}" ]]; then
  echo "OpenHarness virtualenv Python not found: ${python_bin}" >&2
  exit 20
fi

resume_args=()
if [[ -f "${output_dir}/run-config.json" ]]; then
  resume_args+=(--resume)
fi

cd "${openharness_root}"
"${python_bin}" scripts/run_mcp_persona_week1.py \
  --mcp-persona-root "${mcp_root}" \
  --output-dir "${output_dir}" \
  --language en \
  --mode full \
  --task-ids "${task_ids[@]}" \
  --repeats 2 \
  --experiment-stage baseline \
  --openharness-mode original \
  --model qwen3.6-plus \
  --env-file "${env_file}" \
  --tool-scope server \
  --no-chain-guidance \
  --temperature 0 \
  --seed 42 \
  --max-turns 30 \
  --max-tokens 4096 \
  "${resume_args[@]}"

if [[ ! -f "${output_dir}/semantic-summary.json" ]]; then
  "${python_bin}" scripts/review_mcp_persona_semantics.py \
    --mcp-persona-root "${mcp_root}" \
    --results "${output_dir}/results.jsonl" \
    --summary "${output_dir}/baseline-summary.json" \
    --output "${output_dir}/semantic-reviews.jsonl" \
    --summary-output "${output_dir}/semantic-summary.json" \
    --env-file "${env_file}" \
    --language en \
    --model qwen3.6-plus \
    --task-ids "${task_ids[@]}" \
    --repeats 2 \
    --require-verified52
fi

if [[ ! -f "${output_dir}/baseline1-summary.json" ]]; then
  "${python_bin}" scripts/summarize_mcp_persona_verified52.py \
    --results "${output_dir}/results.jsonl" \
    --semantic-reviews "${output_dir}/semantic-reviews.jsonl" \
    --spec "${workspace_root}/results/config/mcp-persona-rehearsal-spec-v1.json" \
    --output "${output_dir}/baseline1-summary.json"
fi

if [[ ! -f "${output_dir}/baseline2-summary.json" ]]; then
  "${python_bin}" scripts/evaluate_mcp_persona_rehearsal.py \
    --results "${output_dir}/results.jsonl" \
    --semantic-reviews "${output_dir}/semantic-reviews.jsonl" \
    --spec "${workspace_root}/results/config/mcp-persona-rehearsal-spec-v1.json" \
    --output "${output_dir}/baseline2-step-scores.jsonl" \
    --summary-output "${output_dir}/baseline2-summary.json" \
    --expected-repeats 2
fi
