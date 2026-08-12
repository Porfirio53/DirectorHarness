"""从 usage-proxy 目录抽取过程分素材：去掉 system，保留 user/assistant 与 tool_calls。Token 以 requests.jsonl 为准。"""

from __future__ import annotations

import difflib
import json
import re
from pathlib import Path
from typing import Any


def _normalize_content(content: Any) -> str:
    if content is None:
        return ""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: list[str] = []
        for part in content:
            if isinstance(part, dict):
                if part.get("type") == "text" and "text" in part:
                    parts.append(str(part["text"]))
                elif "text" in part:
                    parts.append(str(part["text"]))
            elif isinstance(part, str):
                parts.append(part)
        return "\n".join(parts)
    return str(content)


def extract_request_messages_no_system(request_body: str) -> list[dict[str, str]]:
    """解析 chat completions body，丢弃 system，仅保留 user/assistant（及 tool 若存在可跳过，通常在上游消息里）。"""
    try:
        data = json.loads(request_body)
    except json.JSONDecodeError:
        return []
    messages = data.get("messages")
    if not isinstance(messages, list):
        return []
    out: list[dict[str, str]] = []
    for m in messages:
        if not isinstance(m, dict):
            continue
        role = str(m.get("role", "")).strip()
        if role == "system":
            continue
        if role not in ("user", "assistant", "tool"):
            continue
        text = _normalize_content(m.get("content"))
        item: dict[str, str] = {"role": role, "content": text}
        if role == "tool" and m.get("tool_call_id"):
            item["tool_call_id"] = str(m.get("tool_call_id", ""))
        out.append(item)
    return out


def parse_sse_response(response_text: str) -> tuple[str, list[dict[str, Any]]]:
    """
    解析 OpenAI 兼容的 SSE：拼接 assistant 文本，合并 tool_calls（按 index）。
    返回 (assistant_text, tool_calls)，其中 tool_calls 每项含 name、arguments（解析为 object 失败则为原字符串）。
    """
    buffers: dict[int, dict[str, Any]] = {}
    assistant_parts: list[str] = []

    for line in response_text.splitlines():
        if not line.startswith("data:"):
            continue
        payload = line[5:].strip()
        if not payload or payload == "[DONE]":
            continue
        try:
            event = json.loads(payload)
        except json.JSONDecodeError:
            continue
        choices = event.get("choices")
        if not isinstance(choices, list) or not choices:
            continue
        ch0 = choices[0] if isinstance(choices[0], dict) else {}
        delta = ch0.get("delta")
        if not isinstance(delta, dict):
            continue
        c = delta.get("content")
        if isinstance(c, str) and c:
            assistant_parts.append(c)

        raw_tcs = delta.get("tool_calls")
        if not isinstance(raw_tcs, list):
            continue
        for tc in raw_tcs:
            if not isinstance(tc, dict):
                continue
            idx = int(tc.get("index", 0))
            buf = buffers.setdefault(idx, {"name": "", "arguments": ""})
            fn = tc.get("function")
            if isinstance(fn, dict):
                if fn.get("name"):
                    buf["name"] = str(fn["name"])
                if fn.get("arguments"):
                    buf["arguments"] = str(buf["arguments"]) + str(fn["arguments"])

    merged_text = "".join(assistant_parts)

    tool_calls: list[dict[str, Any]] = []
    for idx in sorted(buffers.keys()):
        b = buffers[idx]
        name = str(b.get("name", "") or "").strip()
        args_raw = str(b.get("arguments", "") or "").strip()
        args_parsed: Any = args_raw
        if args_raw:
            try:
                args_parsed = json.loads(args_raw)
            except json.JSONDecodeError:
                pass
        if name or args_raw:
            tool_calls.append({"name": name, "arguments": args_parsed})

    return merged_text, tool_calls


def _parse_non_stream_response(response_json: dict[str, Any]) -> tuple[str, list[dict[str, Any]]]:
    choices = response_json.get("choices")
    if not isinstance(choices, list) or not choices:
        return "", []
    ch0 = choices[0] if isinstance(choices[0], dict) else {}
    msg = ch0.get("message")
    if not isinstance(msg, dict):
        return "", []
    content = _normalize_content(msg.get("content"))
    tool_calls: list[dict[str, Any]] = []
    for tc in msg.get("tool_calls") or []:
        if not isinstance(tc, dict):
            continue
        fn = tc.get("function")
        if not isinstance(fn, dict):
            continue
        name = str(fn.get("name", "") or "")
        args_raw = str(fn.get("arguments", "") or "")
        args_parsed: Any = args_raw
        if args_raw:
            try:
                args_parsed = json.loads(args_raw)
            except json.JSONDecodeError:
                pass
        tool_calls.append({"name": name, "arguments": args_parsed})
    return content, tool_calls


def parse_response_record(raw: dict[str, Any]) -> tuple[str, list[dict[str, Any]]]:
    """单条 proxy 落盘记录：优先 SSE，否则用 choices[0].message。"""
    rt = raw.get("response_text")
    if isinstance(rt, str) and "data:" in rt:
        text, tools = parse_sse_response(rt)
        if text.strip() or tools:
            return text, tools
    rj = raw.get("response_json")
    if isinstance(rj, dict) and rj.get("choices"):
        return _parse_non_stream_response(rj)
    if isinstance(rt, str) and rt.strip().startswith("{"):
        try:
            one = json.loads(rt)
            if isinstance(one, dict) and one.get("choices"):
                return _parse_non_stream_response(one)
        except json.JSONDecodeError:
            pass
    return "", []


def _load_requests_jsonl_index(log_path: Path) -> dict[str, dict[str, Any]]:
    """basename(raw_response_file) -> 该行 JSON（用量等）。"""
    index: dict[str, dict[str, Any]] = {}
    if not log_path.is_file():
        return index
    for line in log_path.read_text(encoding="utf-8", errors="replace").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            row = json.loads(line)
        except json.JSONDecodeError:
            continue
        raw_f = row.get("raw_response_file")
        if isinstance(raw_f, str):
            index[Path(raw_f).name] = row
    return index


def _sum_session_tokens_from_jsonl(log_path: Path) -> dict[str, int]:
    """整次会话：对 requests.jsonl 全部行累加 token。"""
    totals = {"input_tokens": 0, "output_tokens": 0, "total_tokens": 0, "llm_rounds": 0}
    if not log_path.is_file():
        return totals
    for line in log_path.read_text(encoding="utf-8", errors="replace").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            row = json.loads(line)
        except json.JSONDecodeError:
            continue
        totals["llm_rounds"] += 1
        totals["input_tokens"] += int(row.get("input_tokens", 0) or 0)
        totals["output_tokens"] += int(row.get("output_tokens", 0) or 0)
        totals["total_tokens"] += int(row.get("total_tokens", 0) or 0)
    return totals


def _last_user_content(messages: list[dict[str, str]]) -> str:
    for m in reversed(messages):
        if m.get("role") == "user":
            return str(m.get("content", ""))
    return ""


def extract_round_from_response_file(path: Path, usage_row: dict[str, Any] | None = None) -> dict[str, Any]:
    raw = json.loads(path.read_text(encoding="utf-8"))
    request_body = raw.get("request_body")
    if not isinstance(request_body, str):
        request_body = ""
    messages = extract_request_messages_no_system(request_body)
    assistant_text, tool_calls = parse_response_record(raw)

    out: dict[str, Any] = {
        "response_file": path.name,
        "task_id": raw.get("task_id", ""),
        "session_id": raw.get("session_id", ""),
        "model_id": raw.get("model_id", ""),
        "framework": raw.get("framework", ""),
        "provider": raw.get("provider", ""),
        "request_messages": messages,
        "last_user_content": _last_user_content(messages),
        "assistant_text": assistant_text,
        "tool_calls": tool_calls,
    }
    if usage_row:
        out["usage"] = {
            "input_tokens": usage_row.get("input_tokens", 0),
            "output_tokens": usage_row.get("output_tokens", 0),
            "cache_read_tokens": usage_row.get("cache_read_tokens", 0),
            "cache_write_tokens": usage_row.get("cache_write_tokens", 0),
            "total_tokens": usage_row.get("total_tokens", 0),
            "response_model": usage_row.get("response_model", ""),
        }
    return out


def _msg_fingerprint(m: dict[str, str]) -> str:
    """Stable key for a message used in LCS comparison."""
    content = str(m.get("content") or "")
    return f"{m.get('role','')}\x00{content[:300]}"


def _diff_new_messages(
    prev: list[dict[str, str]],
    curr: list[dict[str, str]],
) -> list[dict[str, str]]:
    """Return messages in *curr* that are genuinely new compared to *prev*.

    Uses LCS (via SequenceMatcher) so that context compression — which can
    *shorten* the history — is handled correctly: we find the largest common
    subsequence and return what is in ``curr`` but not matched.
    """
    prev_keys = [_msg_fingerprint(m) for m in prev]
    curr_keys = [_msg_fingerprint(m) for m in curr]
    matcher = difflib.SequenceMatcher(None, prev_keys, curr_keys, autojunk=False)
    new: list[dict[str, str]] = []
    for tag, _i1, _i2, j1, j2 in matcher.get_opcodes():
        if tag in ("insert", "replace"):
            new.extend(curr[j1:j2])
    return new


def _agent_key(request_body: str) -> str:
    """Derive a stable identity for the agent that made this request.

    Main agent and each sub-agent type have distinct system prompts.
    We use the first 120 chars of the system message as the bucket key
    so that rounds from different agent roles are diffed independently.
    """
    try:
        data = json.loads(request_body)
    except (json.JSONDecodeError, TypeError):
        return "unknown"
    messages = data.get("messages") or []
    sys_content = next((str(m.get("content", "")) for m in messages if m.get("role") == "system"), "")
    return sys_content[:120]


def extract_proxy_trace_incremental(proxy_dir: Path) -> dict[str, Any]:
    """Build a deduplicated unified transcript via incremental LCS diff.

    Each LLM round contributes only the *new* messages it added to the
    conversation (delta vs the previous round **from the same agent**),
    plus its own response.  Main agent and sub-agent sessions are tracked
    separately so their independent histories do not cross-contaminate the
    diff.  Context-compression rounds that shrink the history are handled
    correctly by SequenceMatcher.

    Returns a dict with:
    - ``unified_transcript``: flat list of all unique messages in order.
    - ``rounds``: per-round metadata with ``new_messages`` (delta only).
    - ``totals``: same token/round totals as other modes.
    """
    proxy_dir = proxy_dir.resolve()
    responses_dir = proxy_dir / "responses"
    log_path = proxy_dir / "requests.jsonl"
    usage_by_file = _load_requests_jsonl_index(log_path)
    session_totals = _sum_session_tokens_from_jsonl(log_path)

    if not responses_dir.is_dir():
        return {"proxy_dir": str(proxy_dir), "rounds": [], "totals": {}, "error": "missing responses/"}

    files = sorted(responses_dir.glob("*.json"), key=lambda p: p.name)
    if not files:
        return {"proxy_dir": str(proxy_dir), "rounds": [], "totals": {}, "error": "empty responses/"}

    unified: list[dict[str, Any]] = []
    # prev_by_agent[agent_key] = last seen request_messages for that agent.
    prev_by_agent: dict[str, list[dict[str, str]]] = {}
    rounds: list[dict[str, Any]] = []

    for fp in files:
        raw = json.loads(fp.read_text(encoding="utf-8"))
        provider = str(raw.get("provider", ""))
        request_body = raw.get("request_body") or ""

        # Skip compaction rounds — update the bucket but don't emit output.
        if provider == "compaction_summarizer":
            key = _agent_key(request_body)
            curr_messages = extract_request_messages_no_system(request_body)
            prev_by_agent[key] = curr_messages
            continue

        # Router / special providers: emit the response only (no history diff).
        if provider in ("router",):
            assistant_text, tool_calls = parse_response_record(raw)
            if assistant_text or tool_calls:
                entry: dict[str, Any] = {"role": "assistant", "provider": provider, "content": assistant_text}
                if tool_calls:
                    entry["tool_calls"] = tool_calls
                unified.append(entry)
            usage_row = usage_by_file.get(fp.name)
            rounds.append({
                "response_file": fp.name,
                "provider": provider,
                "new_messages": [],
                "assistant_text": assistant_text,
                "tool_calls": tool_calls,
                **({"usage": {
                    "input_tokens": usage_row.get("input_tokens", 0),
                    "output_tokens": usage_row.get("output_tokens", 0),
                    "cache_read_tokens": usage_row.get("cache_read_tokens", 0),
                    "cache_write_tokens": usage_row.get("cache_write_tokens", 0),
                    "total_tokens": usage_row.get("total_tokens", 0),
                    "response_model": usage_row.get("response_model", ""),
                }} if usage_row else {}),
            })
            continue

        key = _agent_key(request_body)
        curr_messages = extract_request_messages_no_system(request_body)
        assistant_text, tool_calls = parse_response_record(raw)

        prev = prev_by_agent.get(key, [])
        new_msgs = _diff_new_messages(prev, curr_messages)

        is_sub = "Sub-Agent" in key
        agent_label = "sub_agent" if is_sub else "main"

        # Emit only the new context messages (no response yet — the response
        # naturally appears in the *next* round's new_messages as an assistant
        # turn + tool results, avoiding double-emission).
        for msg in new_msgs:
            annotated = dict(msg)
            annotated["agent"] = agent_label
            unified.append(annotated)

        usage_row = usage_by_file.get(fp.name)
        rounds.append({
            "response_file": fp.name,
            "provider": provider,
            "agent": agent_label,
            "new_messages": new_msgs,
            "assistant_text": assistant_text,
            "tool_calls": tool_calls,
            **({"usage": {
                "input_tokens": usage_row.get("input_tokens", 0),
                "output_tokens": usage_row.get("output_tokens", 0),
                "cache_read_tokens": usage_row.get("cache_read_tokens", 0),
                "cache_write_tokens": usage_row.get("cache_write_tokens", 0),
                "total_tokens": usage_row.get("total_tokens", 0),
                "response_model": usage_row.get("response_model", ""),
            }} if usage_row else {}),
        })

        prev_by_agent[key] = curr_messages

    # The very last round's response is never picked up by a subsequent diff,
    # so append it explicitly.
    if rounds:
        last = rounds[-1]
        last_text = last.get("assistant_text") or ""
        last_tcs = last.get("tool_calls") or []
        if last_text or last_tcs:
            tail: dict[str, Any] = {
                "role": "assistant",
                "agent": last.get("agent", "main"),
                "content": last_text,
            }
            if last_tcs:
                tail["tool_calls"] = last_tcs
            unified.append(tail)

    return {
        "proxy_dir": str(proxy_dir),
        "extract_mode": "incremental_diff",
        "unified_transcript": unified,
        "rounds": rounds,
        "totals": {
            "llm_rounds": session_totals["llm_rounds"],
            "input_tokens": session_totals["input_tokens"],
            "output_tokens": session_totals["output_tokens"],
            "total_tokens": session_totals["total_tokens"],
        },
    }


def extract_proxy_trace(proxy_dir: Path, *, all_rounds: bool = False) -> dict[str, Any]:
    """
    读取 ``usage-proxy`` 目录：``responses/*.json`` + 可选 ``requests.jsonl``。
    默认只抽取 **按文件名排序后的最后一个** ``responses/*.json``（含完整累计 ``request_messages``）；
    传 ``all_rounds=True`` 时与旧行为一致，逐文件一轮一条。
    ``totals`` 始终按 **整份** ``requests.jsonl`` 汇总会话级 token（与抽取几条 response 无关）。
    """
    proxy_dir = proxy_dir.resolve()
    responses_dir = proxy_dir / "responses"
    log_path = proxy_dir / "requests.jsonl"
    usage_by_file = _load_requests_jsonl_index(log_path)
    session_totals = _sum_session_tokens_from_jsonl(log_path)

    if not responses_dir.is_dir():
        return {"proxy_dir": str(proxy_dir), "rounds": [], "totals": {}, "error": "missing responses/"}

    files = sorted(responses_dir.glob("*.json"), key=lambda p: p.name)
    if not files:
        return {"proxy_dir": str(proxy_dir), "rounds": [], "totals": {}, "error": "empty responses/"}

    to_read = files if all_rounds else [files[-1]]

    # Hermes 框架需要找倒数第二个
    framework = None
    if files:
        try:
            first_file = json.loads(files[0].read_text(encoding="utf-8"))
            framework = first_file.get("framework", "")
        except:
            pass
    # 如果是 Hermes 框架，并且有至少2个文件，抽取倒数第二个
    if framework == "hermes" and len(files) >= 2:
        to_read = [files[-2]]  # 倒数第二个文件
        
    rounds: list[dict[str, Any]] = []
    for fp in to_read:
        usage_row = usage_by_file.get(fp.name)
        rounds.append(extract_round_from_response_file(fp, usage_row))

    out: dict[str, Any] = {
        "proxy_dir": str(proxy_dir),
        "extract_mode": "all_rounds" if all_rounds else "last_response_only",
        "rounds": rounds,
        "totals": {
            "llm_rounds": session_totals["llm_rounds"],
            "input_tokens": session_totals["input_tokens"],
            "output_tokens": session_totals["output_tokens"],
            "total_tokens": session_totals["total_tokens"],
        },
    }
    if not all_rounds:
        out["source_response_file"] = files[-1].name
    return out


def main() -> int:
    import argparse

    p = argparse.ArgumentParser(description="从 usage-proxy 抽取 user/assistant/tool（无 system），token 来自 jsonl")
    p.add_argument(
        "proxy_dir",
        type=Path,
        help="usage-proxy 目录，或包含 usage-proxy 的 sandbox 目录",
    )
    p.add_argument(
        "--all-rounds",
        action="store_true",
        help="抽取全部 responses/*.json（默认只抽最后一个，含完整上下文）",
    )
    args = p.parse_args()
    root = args.proxy_dir.resolve()
    proxy = root / "usage-proxy" if (root / "usage-proxy").is_dir() else root
    trace = extract_proxy_trace(proxy, all_rounds=args.all_rounds)
    print(json.dumps(trace, ensure_ascii=False, indent=2))
    return 0 if "error" not in trace else 1


if __name__ == "__main__":
    raise SystemExit(main())
