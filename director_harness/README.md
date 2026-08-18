# Director Harness

`director_harness` 在 OpenHarness 调用实际工具前检查工具注册与调用参数。通过的工具会按工具名缓存，在同一会话中不会重复检查。未注册工具会从人工备案的 MCP 目录中选择候选项，使用 OpenHarness 标准 `mcpServers` 配置连接，并把已连接 MCP 暴露的工具注册回运行时。Writer Harness 在规划期也会读取同一备案目录，但只将其展示为 `auto_connectable` 候选；真实工具名、参数和可用状态必须在运行时经 `tools/list` 确认。

启用方式：

```powershell
$env:DIRECTOR_HARNESS_ENABLED = "true"
$env:DIRECTOR_MCP_CATALOG = ".\director_harness\director_mcp_catalog.json"
$env:DIRECTOR_LOG_PATH = ".\director-events.jsonl"
oh -p "你的任务"
```

MCP 目录示例：

```json
{
  "mcp_candidates": [
    {
      "name": "reference-search",
      "capabilities": ["search", "reference"],
      "tool_aliases": ["search_reference"],
      "config": {
        "type": "stdio",
        "command": "uvx",
        "args": ["approved-reference-mcp"]
      }
    }
  ]
}
```

目录只应包含已审核的 MCP。模块不会下载未知 MCP、安装依赖、写入密钥或修改持久化 OpenHarness 配置。规划和执行均遵循“检索直接工具 → 组合直接工具 → 检索 MCP 候选 → 无法接入时降级或澄清”的顺序。MCP 健康检查以标准连接和 `tools/list` 为基础，避免为验证而重复执行具有副作用的工具。
