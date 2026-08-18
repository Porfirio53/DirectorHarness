$ErrorActionPreference = "Stop"

python .\writer_excute.py `
  --query "请列出 OpenHarness 当前内置的工具实现文件，并总结哪些属于只读工具，哪些可能产生状态变更。" `
  --mode writer_harness `
  --writer-model $env:WRITER_MODEL `
  --writer-base-url $env:WRITER_BASE_URL `
  --writer-api-key $env:WRITER_API_KEY `
  --actor-model $env:ACTOR_MODEL `
  --actor-base-url $env:ACTOR_BASE_URL `
  --actor-api-key $env:ACTOR_API_KEY `
  --actor-api-format $env:ACTOR_API_FORMAT `
  --oh-bin $env:OH_BIN `
  --openharness-src $env:OPENHARNESS_SRC `
  --oh-real-run `
  --execute-output-format stream-json `
  --json
