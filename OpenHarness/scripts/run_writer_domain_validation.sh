#!/usr/bin/env bash
set -euo pipefail

check_only=0
if [[ "${1:-}" == "--check-only" ]]; then
  check_only=1
  shift
fi

script_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
openharness_root="$(cd "${script_dir}/.." && pwd)"
workspace_root="${1:-$(cd "${openharness_root}/.." && pwd)}"
env_file="${2:-${workspace_root}/tau3-bench/.env}"
result_root="${3:-${workspace_root}/results/runs/writer-domain-validation-v1}"
config_path="${4:-${workspace_root}/results/config/writer-domain-validation-v1.json}"
harnessbench_root="${workspace_root}/HarnessBench"
mcp_root="${workspace_root}/MCP-Persona"
python_bin="${openharness_root}/.venv/bin/python"
spec_path="${workspace_root}/results/config/mcp-persona-rehearsal-spec-v1.json"

if [[ ! -x "${python_bin}" ]]; then
  echo "OpenHarness virtualenv Python not found: ${python_bin}" >&2
  exit 20
fi

mkdir -p "${result_root}"
cd "${openharness_root}"
"${python_bin}" scripts/summarize_writer_domain_validation.py preflight \
  --workspace-root "${workspace_root}" \
  --env-file "${env_file}" \
  --config "${config_path}" \
  --result-root "${result_root}" \
  --output "${result_root}/preflight.json"

if (( check_only )); then
  exit 0
fi

mapfile -t locked_values < <(
  "${python_bin}" - "${config_path}" <<'PY'
import json
import sys
from pathlib import Path

config = json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))
hb = config["harnessbench"]
mcp = config["mcp_persona"]
print(config["model"])
print(config["repeats"])
print(",".join(task for domain in hb["domains"] for task in domain["task_ids"]))
print(" ".join(str(task) for domain in mcp["domains"] for task in domain["task_ids"]))
print(hb["max_turns"])
print(hb["api_timeout_seconds"])
print(mcp["max_turns"])
print(mcp["max_tokens"])
print(mcp["api_timeout_seconds"])
print(mcp["trial_timeout_seconds"])
PY
)

model="${locked_values[0]}"
repeats="${locked_values[1]}"
hb_tasks="${locked_values[2]}"
read -r -a mcp_tasks <<< "${locked_values[3]}"
hb_max_turns="${locked_values[4]}"
hb_api_timeout="${locked_values[5]}"
mcp_max_turns="${locked_values[6]}"
mcp_max_tokens="${locked_values[7]}"
mcp_api_timeout="${locked_values[8]}"
mcp_trial_timeout="${locked_values[9]}"

run_harnessbench_arm() {
  local mode="$1"
  local output_dir="$2"
  local -a resume_args=()
  local -a writer_args=()
  local run_rc=0
  local summary_rc=0

  if [[ -f "${output_dir}/run-config.json" ]]; then
    resume_args+=(--resume)
  fi
  if [[ "${mode}" == "writer_harness" ]]; then
    writer_args+=(
      --writer-workspace-root "${workspace_root}"
      --writer-archive "${workspace_root}/writer_harness_demo.zip"
      --writer-model "${model}"
      --writer-max-tokens 4096
    )
  fi

  "${python_bin}" scripts/run_openharness_harnessbench.py run \
    --harnessbench-root "${harnessbench_root}" \
    --tasks "${hb_tasks}" \
    --model "${model}" \
    --profile qwen \
    --api-format openai \
    --temperature 0 \
    --seed 42 \
    --env-file "${env_file}" \
    --max-turns "${hb_max_turns}" \
    --api-timeout-sec "${hb_api_timeout}" \
    --grading full \
    --rubric-model "${model}" \
    --rubric-vision-model "${model}" \
    --rubric-base-url-env OPENAI_API_BASE \
    --rubric-api-key-env OPENAI_API_KEY \
    --public-url-mode loopback \
    --openharness-mode "${mode}" \
    --repeats "${repeats}" \
    --output-dir "${output_dir}" \
    "${writer_args[@]}" \
    "${resume_args[@]}" || run_rc=$?
  if (( run_rc > 1 )); then
    return "${run_rc}"
  fi

  "${python_bin}" scripts/redact_harnessbench_artifacts.py \
    --env-file "${env_file}" \
    --root "${output_dir}"
  "${python_bin}" scripts/run_openharness_harnessbench.py summarize \
    --output-dir "${output_dir}" || summary_rc=$?
  if (( summary_rc > 1 )); then
    return "${summary_rc}"
  fi
}

run_mcp_arm() {
  local mode="$1"
  local output_dir="$2"
  local -a resume_args=()
  local -a writer_args=()
  local -a review_resume=()

  if [[ -f "${output_dir}/run-config.json" ]]; then
    resume_args+=(--resume)
  fi
  if [[ "${mode}" == "writer_harness" ]]; then
    writer_args+=(
      --writer-workspace-root "${workspace_root}"
      --writer-archive "${workspace_root}/writer_harness_demo.zip"
      --writer-model "${model}"
      --writer-max-tokens 4096
    )
  fi

  "${python_bin}" scripts/run_mcp_persona_week1.py \
    --mcp-persona-root "${mcp_root}" \
    --output-dir "${output_dir}" \
    --language en \
    --mode full \
    --task-ids "${mcp_tasks[@]}" \
    --repeats "${repeats}" \
    --experiment-stage paired-smoke \
    --openharness-mode "${mode}" \
    --model "${model}" \
    --env-file "${env_file}" \
    --tool-scope server \
    --no-chain-guidance \
    --temperature 0 \
    --seed 42 \
    --max-turns "${mcp_max_turns}" \
    --max-tokens "${mcp_max_tokens}" \
    --api-timeout "${mcp_api_timeout}" \
    --trial-timeout "${mcp_trial_timeout}" \
    "${writer_args[@]}" \
    "${resume_args[@]}"

  if [[ -f "${output_dir}/semantic-run-config.json" ]]; then
    review_resume+=(--resume)
  fi
  "${python_bin}" scripts/review_mcp_persona_semantics.py \
    --mcp-persona-root "${mcp_root}" \
    --results "${output_dir}/results.jsonl" \
    --summary "${output_dir}/baseline-summary.json" \
    --output "${output_dir}/semantic-reviews.jsonl" \
    --summary-output "${output_dir}/semantic-summary.json" \
    --env-file "${env_file}" \
    --language en \
    --model "${model}" \
    --task-ids "${mcp_tasks[@]}" \
    --repeats "${repeats}" \
    "${review_resume[@]}"

  "${python_bin}" scripts/evaluate_mcp_persona_rehearsal.py \
    --results "${output_dir}/results.jsonl" \
    --semantic-reviews "${output_dir}/semantic-reviews.jsonl" \
    --spec "${spec_path}" \
    --output "${output_dir}/rehearsal-step-scores.jsonl" \
    --summary-output "${output_dir}/rehearsal-step-summary.json" \
    --expected-repeats "${repeats}" \
    --allow-partial
}

run_harnessbench_arm original "${result_root}/HarnessBench/original"
run_harnessbench_arm writer_harness "${result_root}/HarnessBench/writer"
run_mcp_arm original "${result_root}/MCP-Persona/original"
run_mcp_arm writer_harness "${result_root}/MCP-Persona/writer"

"${python_bin}" scripts/summarize_writer_domain_validation.py summarize \
  --config "${config_path}" \
  --result-root "${result_root}" \
  --output-json "${result_root}/paired-summary.json" \
  --output-markdown "${result_root}/paired-summary.md"

echo "Paired JSON summary: ${result_root}/paired-summary.json"
echo "Paired Markdown summary: ${result_root}/paired-summary.md"
