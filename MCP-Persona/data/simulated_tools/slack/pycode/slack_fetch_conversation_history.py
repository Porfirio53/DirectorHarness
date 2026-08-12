from pathlib import Path
import json
import os
import base64
from slack.pycode.dynamic_context_handler import (
    load_context,
    save_context,
    get_entity_by_path,
    list_entities_by_path,
    create_entity_by_path,
    update_entity_by_path,
    delete_entity_by_path,
)


def analyze_response_patterns(parameters_used):
    # 1. Basic input format validation (types, required fields, etc.)
    if not isinstance(parameters_used, dict):
        return {"success": False, "error": "Input must be a dictionary", "result": None}

    allowed_keys = {"channel", "cursor", "inclusive", "latest", "limit", "oldest"}
    unexpected_keys = set(parameters_used.keys()) - allowed_keys
    if unexpected_keys:
        return {
            "success": False,
            "error": f"Unexpected parameter(s): {', '.join(sorted(unexpected_keys))}",
            "result": None,
        }

    if "channel" not in parameters_used:
        return {
            "success": False,
            "error": "Missing required parameter: channel",
            "result": None,
        }
    channel = parameters_used.get("channel")
    if not isinstance(channel, str) or not channel.strip():
        return {
            "success": False,
            "error": "Parameter 'channel' must be a non-empty string",
            "result": None,
        }

    cursor = parameters_used.get("cursor")
    if cursor is not None and not isinstance(cursor, str):
        return {
            "success": False,
            "error": "Parameter 'cursor' must be a string if provided",
            "result": None,
        }

    inclusive = parameters_used.get("inclusive", False)
    if inclusive is not None and not isinstance(inclusive, bool):
        return {
            "success": False,
            "error": "Parameter 'inclusive' must be a boolean if provided",
            "result": None,
        }

    limit = parameters_used.get("limit", 100)
    if not isinstance(limit, int):
        return {
            "success": False,
            "error": "Parameter 'limit' must be an integer",
            "result": None,
        }
    if limit < 1 or limit > 1000:
        return {
            "success": False,
            "error": "Parameter 'limit' must be between 1 and 1000",
            "result": None,
        }

    def parse_timestamp(ts_val):
        if ts_val is None:
            return None
        if isinstance(ts_val, (int, float)):
            return float(ts_val)
        if isinstance(ts_val, str):
            ts_val = ts_val.strip()
            try:
                return float(ts_val)
            except Exception:
                return None
        return None

    oldest_raw = parameters_used.get("oldest", None)
    latest_raw = parameters_used.get("latest", None)
    if oldest_raw is not None and not isinstance(oldest_raw, str):
        return {
            "success": False,
            "error": "Parameter 'oldest' must be a string Slack timestamp or None",
            "result": None,
        }
    if latest_raw is not None and not isinstance(latest_raw, str):
        return {
            "success": False,
            "error": "Parameter 'latest' must be a string Slack timestamp or None",
            "result": None,
        }

    oldest_ts = parse_timestamp(oldest_raw) if oldest_raw is not None else None
    latest_ts = parse_timestamp(latest_raw) if latest_raw is not None else None
    if oldest_raw is not None and oldest_ts is None:
        return {
            "success": False,
            "error": f"Invalid 'oldest' timestamp format: {oldest_raw}",
            "result": None,
        }
    if latest_raw is not None and latest_ts is None:
        return {
            "success": False,
            "error": f"Invalid 'latest' timestamp format: {latest_raw}",
            "result": None,
        }
    if oldest_ts is not None and latest_ts is not None and oldest_ts > latest_ts:
        return {
            "success": False,
            "error": "Parameter 'oldest' must be less than or equal to 'latest'",
            "result": None,
        }

    # 2. Load all context data and select context(s) based on context_id from environment variable
    context_file_path = Path(json.loads(os.environ.get("SLACK_SANDBOX_PATHS"))[0])
    all_context_data = load_context(
        context_file_path
    )  # Returns Dict[{context_id}: {context_dict}] or List[Dict]

    tool_name = "slack:fetch_conversation_history"
    tool_description = "Fetches a chronological list of messages and events from a specified Slack conversation, accessible by the authenticated user/bot, with options for pagination and time range filtering."
    tool_name_lower = tool_name.lower() if tool_name else ""
    tool_description_lower = tool_description.lower() if tool_description else ""
    is_query_tool = any(
        keyword in tool_name_lower
        for keyword in ["get", "list", "query", "search", "batch", "fetch", "history"]
    ) or any(
        keyword in tool_description_lower
        for keyword in ["get", "list", "query", "search", "batch", "fetch", "history"]
    )

    # Read context_id from environment variable (NOT from input parameters)
    context_id = os.environ.get("SLACK_CONTEXT_ID")

    # Determine default behavior based on tool type
    if context_id is None:
        if is_query_tool:
            context_id = "all"  # Query tools (get/list) can access any context
        else:
            if isinstance(all_context_data, dict) and len(all_context_data) > 0:
                context_id = list(all_context_data.keys())[0]
            else:
                context_id = None

    # Get context(s) based on context_id
    if context_id == "all":
        if isinstance(all_context_data, dict):
            context_data = list(all_context_data.values())
        elif isinstance(all_context_data, list):
            context_data = all_context_data
        else:
            context_data = [all_context_data]
    elif isinstance(all_context_data, dict):
        if context_id in all_context_data:
            context_data = all_context_data[context_id]
        else:
            return {
                "success": False,
                "error": f"Context ID '{context_id}' not found",
                "result": None,
            }
    elif isinstance(all_context_data, list):
        if context_id is None:
            context_data = all_context_data[0] if len(all_context_data) > 0 else {}
        else:
            for ctx in all_context_data:
                if isinstance(ctx, dict) and ctx.get("user_id") == context_id:
                    context_data = ctx
                    break
            else:
                return {
                    "success": False,
                    "error": f"Context ID '{context_id}' not found",
                    "result": None,
                }
    else:
        context_data = all_context_data

    # Prepare to search for conversation across contexts if needed
    contexts_to_search = []
    if isinstance(context_data, list):
        contexts_to_search = [ctx for ctx in context_data if isinstance(ctx, dict)]
    elif isinstance(context_data, dict):
        contexts_to_search = [context_data]
    else:
        contexts_to_search = []

    # 3. Validate entity references exist in context (CRITICAL!)
    # Try to locate the conversation in any available context
    conversation_found_in_any = False
    aggregated_messages = []
    for ctx in contexts_to_search:
        conv = get_entity_by_path(ctx, "group_channels", channel)
        if conv:
            conversation_found_in_any = True
            # 4. Perform operation (list) using path-based functions and gather messages
            messages_path = f"group_channels[{channel}].messages"
            # Retrieve ALL entities for pagination first with a large limit
            msgs = list_entities_by_path(ctx, messages_path, {}, 10000)
            if isinstance(msgs, list):
                aggregated_messages.extend(msgs)

    if not conversation_found_in_any:
        return {
            "success": False,
            "error": f"Invalid channel: {channel}",
            "result": None,
        }

    # Normalize and filter by time range
    def msg_ts(message):
        # Prefer Slack 'ts' field; fallback to 'timestamp' or 'time'
        ts_value = message.get("ts", None)
        if ts_value is None:
            ts_value = message.get("timestamp", None)
        if ts_value is None:
            ts_value = message.get("time", None)
        try:
            return float(ts_value) if ts_value is not None else 0.0
        except Exception:
            return 0.0

    filtered_messages = []
    for m in aggregated_messages:
        ts_val = msg_ts(m)
        # Apply filters
        passes = True
        if oldest_ts is not None and latest_ts is not None:
            if inclusive:
                passes = (ts_val >= oldest_ts) and (ts_val <= latest_ts)
            else:
                passes = (ts_val > oldest_ts) and (ts_val < latest_ts)
        elif oldest_ts is not None:
            if inclusive:
                passes = ts_val >= oldest_ts
            else:
                passes = ts_val > oldest_ts
        elif latest_ts is not None:
            if inclusive:
                passes = ts_val <= latest_ts
            else:
                passes = ts_val < latest_ts
        if passes:
            filtered_messages.append(m)

    # Sort chronologically (ascending by timestamp)
    filtered_messages.sort(key=lambda x: msg_ts(x))

    # Decode cursor to get offset
    def decode_offset(cur):
        if not cur:
            return 0
        try:
            decoded = base64.b64decode(cur).decode("utf-8", errors="ignore")
            if decoded.startswith("offset:"):
                off_str = decoded.split("offset:", 1)[1].strip()
                return int(off_str) if off_str.isdigit() else 0
            # Fallback: try integer string
            return int(decoded)
        except Exception:
            return 0

    offset = decode_offset(cursor)
    if offset < 0:
        offset = 0

    # Apply pagination
    total = len(filtered_messages)
    start = min(offset, total)
    end = min(start + limit, total)
    page_messages = filtered_messages[start:end]

    has_more = end < total
    next_cursor = ""
    if has_more:
        next_offset = end
        next_cursor = base64.b64encode(f"offset:{next_offset}".encode("utf-8")).decode(
            "utf-8"
        )

    # 5. Save context if modified (not applicable for this query tool; no modifications made)
    # No save operation required

    # 6. Format and return response
    result_payload = {
        "ok": True,
        "channel": channel,
        "messages": page_messages,
        "has_more": has_more,
        "response_metadata": {"next_cursor": next_cursor},
    }

    return {
        "success": True,
        "error": None,
        "result": {
            "meta": None,
            "content": [
                {
                    "type": "text",
                    "text": json.dumps(result_payload, ensure_ascii=False),
                }
            ],
            "isError": False,
        },
    }
