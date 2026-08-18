#!/usr/bin/env bash
set -euo pipefail

python ./writer_excute.py \
  --query "请列出 OpenHarness 当前内置的工具实现文件，并总结哪些属于只读工具，哪些可能产生状态变更。" \
  --mode writer_harness \
  --writer-model "$WRITER_MODEL" \
  --writer-base-url "$WRITER_BASE_URL" \
  --writer-api-key "$WRITER_API_KEY" \
  --actor-model "$ACTOR_MODEL" \
  --actor-base-url "$ACTOR_BASE_URL" \
  --actor-api-key "$ACTOR_API_KEY" \
  --actor-api-format "$ACTOR_API_FORMAT" \
  --oh-bin "${OH_BIN:-oh}" \
  --openharness-src "${OPENHARNESS_SRC:-./OpenHarness/src}" \
  --oh-real-run \
  --execute-output-format stream-json \
  --json
