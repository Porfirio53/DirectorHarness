#!/usr/bin/env bash
set -euo pipefail

workspace_root="${1:-/home/patton/projects/harness}"
env_file="${2:-${workspace_root}/.env}"
run_root="${3:-${workspace_root}/results/runs/original-full}"
openharness_root="${workspace_root}/OpenHarness"

HARNESSBENCH_ENV_FILE="${env_file}" \
HARNESSBENCH_OUTPUT_DIR="${run_root}/HarnessBench" \
  bash "${openharness_root}/scripts/run_harnessbench_openharness_baseline.sh"

bash "${openharness_root}/scripts/run_mcp_persona_openharness_baseline.sh" \
  "${workspace_root}" "${env_file}" "${run_root}/MCP-Persona"
