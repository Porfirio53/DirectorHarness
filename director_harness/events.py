"""记录可供 UI 展示和问题追溯的 Director 事件。

JSONL 每行一条事件，适合在 OpenHarness 运行期间持续追加，并由前端按
会话读取或转换为对话中的 Director 高亮胶囊。"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from .models import DirectorEvent


class DirectorEventLog:
    """同时维护内存事件列表和可选 JSONL 事件文件。

    参数 ``path`` 为日志文件路径；未提供时仍保留内存事件，便于嵌入式 UI
    或测试直接读取。
    """

    def __init__(self, path: str | Path | None = None) -> None:
        self._path = Path(path).expanduser() if path else None
        self.events: list[DirectorEvent] = []

    def emit(self, event: DirectorEvent) -> None:
        """保存一条事件，并在配置路径时追加其脱敏后的 JSON 表示。"""
        self.events.append(event)
        if self._path is None:
            return
        self._path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "event": event.event,
            "tool_name": event.tool_name,
            "status": event.status,
            "detail": event.detail,
            "session_id": event.session_id,
            "tool_use_id": event.tool_use_id,
            "timestamp": event.timestamp,
            "data": _redact(event.data),
        }
        with self._path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(payload, ensure_ascii=False) + "\n")


def _redact(value: Any) -> Any:
    """递归隐藏日志数据中可能包含凭证的字段值。"""
    if isinstance(value, dict):
        return {str(key): "***" if _sensitive(str(key)) else _redact(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_redact(item) for item in value]
    return value


def public_event_payload(event: DirectorEvent) -> dict[str, Any]:
    """将 Director 事件转换为可安全输出到流式 UI 的数据。"""
    return {
        "event": event.event,
        "tool_name": event.tool_name,
        "status": event.status,
        "detail": event.detail,
        "session_id": event.session_id,
        "tool_use_id": event.tool_use_id,
        "timestamp": event.timestamp,
        "data": _redact(event.data),
    }


def _sensitive(key: str) -> bool:
    """判断键名是否表示 API 凭证或授权信息。"""
    normalized = key.lower()
    return any(token in normalized for token in ("api_key", "apikey", "token", "secret", "authorization", "password"))
