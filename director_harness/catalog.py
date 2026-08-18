"""读取人工备案 MCP 目录，并为缺失工具选择候选 MCP。

目录不是在线搜索结果，而是用户或团队审核后保存的 JSON 文件，确保
Director 的自动接入只发生在可控来源范围内。"""

from __future__ import annotations

import json
from pathlib import Path

from .models import McpCandidate


class McpCatalog:
    """内存中的 MCP 候选集合。

    该类仅负责载入和匹配，不负责安装、下载或持久化修改配置。
    """

    def __init__(self, candidates: tuple[McpCandidate, ...] = ()) -> None:
        """使用候选项初始化目录。

        参数 ``candidates`` 通常来自 :meth:`from_file`，也可供测试或上层
        程序直接注入。
        """
        self._candidates = candidates

    @classmethod
    def from_file(cls, path: str | Path | None) -> "McpCatalog":
        """从 JSON 目录文件构建候选集合。

        参数 ``path`` 是包含 ``mcp_candidates`` 数组的文件路径。路径为空、
        文件不存在时返回空目录；JSON 格式错误则保留异常，避免静默使用
        可能错误的 MCP 配置。
        """
        if not path:
            return cls()
        catalog_path = Path(path).expanduser()
        if not catalog_path.is_file():
            return cls()
        payload = json.loads(catalog_path.read_text(encoding="utf-8"))
        raw_candidates = payload.get("mcp_candidates", []) if isinstance(payload, dict) else []
        candidates: list[McpCandidate] = []
        for item in raw_candidates:
            if not isinstance(item, dict) or not isinstance(item.get("name"), str):
                continue
            config = item.get("config")
            if not isinstance(config, dict):
                continue
            capabilities = item.get("capabilities", [])
            aliases = item.get("tool_aliases", [])
            candidates.append(
                McpCandidate(
                    name=item["name"],
                    capabilities=tuple(str(value) for value in capabilities if str(value).strip()),
                    config=config,
                    tool_aliases=tuple(str(value) for value in aliases if str(value).strip()),
                )
            )
        return cls(tuple(candidates))

    def find_best(self, tool_name: str) -> McpCandidate | None:
        """按别名优先、关键词重叠次之的规则选择最佳候选项。

        ``tool_name`` 是模型请求但当前未注册的 OpenHarness 工具名。精确
        命中 ``tool_aliases`` 的候选项优先级最高；无任何匹配时返回 ``None``。
        """
        target_tokens = set(_tokens(tool_name))
        scored: list[tuple[int, McpCandidate]] = []
        for candidate in self._candidates:
            candidate_text = " ".join((candidate.name, *candidate.capabilities, *candidate.tool_aliases))
            candidate_tokens = set(_tokens(candidate_text))
            score = len(target_tokens & candidate_tokens)
            if tool_name in candidate.tool_aliases:
                score += 100
            if score:
                scored.append((score, candidate))
        return max(scored, key=lambda item: (item[0], item[1].name))[1] if scored else None


def _tokens(value: str) -> list[str]:
    """将工具或能力名称规范化为用于轻量匹配的 token 列表。"""
    return [part for part in value.lower().replace("-", "_").split("_") if part]
