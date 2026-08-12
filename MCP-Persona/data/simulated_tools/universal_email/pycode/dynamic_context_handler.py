"""Dynamic context handler for universal_email simulated tools."""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any, Dict, List, Optional


def _configured_context_path() -> Optional[Path]:
    """Return the explicit writable state path supplied by a harness."""
    explicit_path = os.environ.get("UNIVERSAL_EMAIL_CONTEXT_PATH")
    if explicit_path:
        return Path(explicit_path)

    sandbox_paths = os.environ.get("UNIVERSAL_EMAIL_SANDBOX_PATHS")
    if sandbox_paths:
        try:
            paths = json.loads(sandbox_paths)
        except json.JSONDecodeError as exc:
            raise RuntimeError(
                "UNIVERSAL_EMAIL_SANDBOX_PATHS must be a JSON list"
            ) from exc
        if isinstance(paths, list) and paths and isinstance(paths[0], str):
            return Path(paths[0])
        raise RuntimeError(
            "UNIVERSAL_EMAIL_SANDBOX_PATHS must contain a context file path"
        )

    state_dir = os.environ.get("MCP_PERSONA_STATE_DIR")
    if state_dir:
        return Path(state_dir) / "universal_email.json"
    return None


def _source_context_path() -> Path:
    return Path(__file__).parent.parent / "context.json"


def get_context_path() -> Path:
    """Get the path to the context file."""
    return _configured_context_path() or _source_context_path()


def load_context() -> Dict[str, Any]:
    context_path = get_context_path()
    if context_path.exists():
        with open(context_path, "r", encoding="utf-8") as f:
            return json.load(f)
    return {"accounts": [], "sent_messages": [], "inbox": []}


def save_context(context: Dict[str, Any]) -> None:
    context_path = _configured_context_path()
    if context_path is None:
        raise RuntimeError(
            "universal_email requires an explicit sandbox context path for writes; "
            "refusing to modify the released source fixture"
        )
    if context_path.resolve() == _source_context_path().resolve():
        raise RuntimeError(
            "universal_email sandbox context must not point at the released source fixture"
        )
    context_path.parent.mkdir(parents=True, exist_ok=True)
    with open(context_path, "w", encoding="utf-8") as f:
        json.dump(context, f, ensure_ascii=False, indent=2)


def find_account_by_email(email: str) -> Optional[Dict[str, Any]]:
    context = load_context()
    for account in context.get("accounts", []):
        if account.get("email") == email:
            return account
    return None


def add_account(account: Dict[str, Any]) -> None:
    context = load_context()
    context["accounts"] = [
        acc
        for acc in context.get("accounts", [])
        if acc.get("email") != account.get("email")
    ]
    context["accounts"].append(account)
    save_context(context)


def add_sent_message(message: Dict[str, Any]) -> None:
    context = load_context()
    if "sent_messages" not in context:
        context["sent_messages"] = []
    context["sent_messages"].append(message)
    save_context(context)


def get_recent_messages(days: int = 3, limit: int = 20) -> List[Dict[str, Any]]:
    context = load_context()
    inbox = context.get("inbox", [])
    return inbox[:limit]


def get_message_by_uid(uid: str) -> Optional[Dict[str, Any]]:
    context = load_context()
    for msg in context.get("inbox", []):
        if msg.get("uid") == uid:
            return msg
    return None
