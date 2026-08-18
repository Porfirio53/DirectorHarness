#!/usr/bin/env bash
set -euo pipefail

check_only=0
if [[ "${1:-}" == "--check-only" ]]; then
  check_only=1
  shift
fi

workspace_root="${1:-/home/patton/projects/harness}"
env_file="${2:-${workspace_root}/.env}"
result_root="${3:?result root is required}"
openharness_root="${workspace_root}/OpenHarness"
mcp_root="${workspace_root}/MCP-Persona"
python_bin="${openharness_root}/.venv/bin/python"
mcp_output="${result_root}/MCP-Persona"
harnessbench_output="${result_root}/HarnessBench"
model="qwen3.6-plus"
task_ids=(
  1 2 4 5 8 9 10 13 14 18 19 21 23 24 25 26 28 29 30 34 36 45 47
  49 50 51 53 54 56 62 65 66 69 70 86 92 93 106 110 125 131 132 133
  135 138 141 148 152 156 157 161 173
)

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

if [[ ! -f "${mcp_output}/results.jsonl" ]]; then
  echo "MCP-Persona full results not found: ${mcp_output}/results.jsonl" >&2
  exit 20
fi
if [[ ! -f "${harnessbench_output}/run-config.json" ]]; then
  echo "HarnessBench full results not found: ${harnessbench_output}" >&2
  exit 20
fi

semantic_resume=()
if [[ -f "${mcp_output}/semantic-run-config.json" ]]; then
  semantic_resume+=(--resume)
fi

"${python_bin}" scripts/review_mcp_persona_semantics.py \
  --mcp-persona-root "${mcp_root}" \
  --results "${mcp_output}/results.jsonl" \
  --summary "${mcp_output}/baseline-summary.json" \
  --output "${mcp_output}/semantic-reviews.jsonl" \
  --summary-output "${mcp_output}/semantic-summary.json" \
  --env-file "${env_file}" \
  --language en \
  --model "${model}" \
  --task-ids "${task_ids[@]}" \
  --repeats 2 \
  --require-verified52 \
  "${semantic_resume[@]}"

"${python_bin}" scripts/evaluate_mcp_persona_rehearsal.py \
  --results "${mcp_output}/results.jsonl" \
  --semantic-reviews "${mcp_output}/semantic-reviews.jsonl" \
  --spec "${workspace_root}/results/config/mcp-persona-rehearsal-spec-v1.json" \
  --output "${mcp_output}/rehearsal-step-scores.jsonl" \
  --summary-output "${mcp_output}/rehearsal-step-summary.json" \
  --expected-repeats 2

"${python_bin}" scripts/run_openharness_harnessbench.py summarize \
  --output-dir "${harnessbench_output}"

"${python_bin}" scripts/summarize_writer_full_results.py summarize \
  --mcp-output "${mcp_output}" \
  --harnessbench-output "${harnessbench_output}" \
  --output "${result_root}/full-score-summary.json"

echo "Unified score summary: ${result_root}/full-score-summary.json"
