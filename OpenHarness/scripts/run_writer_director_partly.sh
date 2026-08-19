#!/usr/bin/env bash
set -euo pipefail

script_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
workspace_root="$(cd "${script_dir}/../.." && pwd)"
if [[ "${1:-}" != -* && -n "${1:-}" ]]; then
  workspace_root="$1"
  shift
fi
env_file="${workspace_root}/.env"
if [[ "${1:-}" != -* && -n "${1:-}" ]]; then
  env_file="$1"
  shift
fi

exec "${workspace_root}/OpenHarness/.venv/bin/python" \
  "${workspace_root}/OpenHarness/scripts/run_writer_director_partly.py" \
  "${workspace_root}" \
  "${env_file}" \
  "$@"
