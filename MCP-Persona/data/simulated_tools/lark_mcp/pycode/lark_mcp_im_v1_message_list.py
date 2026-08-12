from pathlib import Path
import json
import os
from lark_mcp.dynamic_context_handler import (
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
    if "params" not in parameters_used or not isinstance(
        parameters_used["params"], dict
    ):
        return {
            "success": False,
            "error": "params is required and must be an object",
            "result": None,
        }
    params = parameters_used["params"]

    allowed_param_keys = {
        "container_id_type",
        "container_id",
        "start_time",
        "end_time",
        "sort_type",
        "page_size",
        "page_token",
    }
    extra_keys = set(params.keys()) - allowed_param_keys
    if extra_keys:
        return {
            "success": False,
            "error": f"Unexpected parameter(s): {', '.join(sorted(extra_keys))}",
            "result": None,
        }

    # Required fields
    container_id_type = params.get("container_id_type")
    container_id = params.get("container_id")
    if not isinstance(container_id_type, str) or not container_id_type:
        return {
            "success": False,
            "error": "container_id_type is required and must be a string",
            "result": None,
        }
    if not isinstance(container_id, str) or not container_id:
        return {
            "success": False,
            "error": "container_id is required and must be a string",
            "result": None,
        }
    if container_id_type not in {"chat", "thread"}:
        return {
            "success": False,
            "error": "container_id_type must be either 'chat' or 'thread'",
            "result": None,
        }

    # Optional fields
    start_time = params.get("start_time")
    end_time = params.get("end_time")
    sort_type = params.get("sort_type") or "ByCreateTimeAsc"
    page_size = params.get("page_size")
    page_token = params.get("page_token")

    if sort_type not in {"ByCreateTimeAsc", "ByCreateTimeDesc"}:
        return {
            "success": False,
            "error": "sort_type must be 'ByCreateTimeAsc' or 'ByCreateTimeDesc'",
            "result": None,
        }

    if start_time is not None and not isinstance(start_time, str):
        return {
            "success": False,
            "error": "start_time must be a string representing seconds",
            "result": None,
        }
    if end_time is not None and not isinstance(end_time, str):
        return {
            "success": False,
            "error": "end_time must be a string representing seconds",
            "result": None,
        }

    # For thread container type, time range isn't supported
    if container_id_type == "thread" and (
        start_time is not None or end_time is not None
    ):
        return {
            "success": False,
            "error": "Time range (start_time/end_time) is not supported for container_id_type 'thread'",
            "result": None,
        }

    # Page size
    if page_size is None:
        page_size = 50
    elif not isinstance(page_size, (int, float)):
        return {"success": False, "error": "page_size must be a number", "result": None}
    else:
        page_size = int(page_size)
        if page_size <= 0:
            return {
                "success": False,
                "error": "page_size must be greater than 0",
                "result": None,
            }

    # Page token -> offset
    def parse_offset(token):
        if token is None or token == "":
            return 0
        if isinstance(token, str) and token.isdigit():
            return int(token)
        return None

    offset = parse_offset(page_token)
    if offset is None or offset < 0:
        return {
            "success": False,
            "error": "Invalid page_token; must be a string integer offset",
            "result": None,
        }

    def parse_time_str(ts):
        try:
            return int(ts) if ts is not None else None
        except Exception:
            return None

    start_ts = parse_time_str(start_time)
    end_ts = parse_time_str(end_time)
    if start_time is not None and start_ts is None:
        return {
            "success": False,
            "error": "start_time must be an integer string (seconds)",
            "result": None,
        }
    if end_time is not None and end_ts is None:
        return {
            "success": False,
            "error": "end_time must be an integer string (seconds)",
            "result": None,
        }

    # 2. Load all context data and select context(s) based on context_id from environment variable
    context_file_path = Path(json.loads(os.environ.get("LARK_MCP_SANDBOX_PATHS"))[0])
    all_context_data = load_context(
        context_file_path
    )  # Returns Dict[{context_id}: {context_dict}] or List[Dict]

    # Read context_id from environment variable (NOT from input parameters)
    context_id = os.environ.get("LARK_MCP_CONTEXT_ID")

    # Determine tool type from tool_name or description to set appropriate default
    tool_name = "lark-mcp:im_v1_message_list"
    tool_name_lower = tool_name.lower() if tool_name else ""
    is_query_tool = any(
        keyword in tool_name_lower
        for keyword in ["get", "list", "query", "search", "batch"]
    )

    # Get context(s) based on context_id
    if context_id is None:
        # Default behavior based on tool type:
        if is_query_tool:
            context_id = "all"  # Query tools (get/list) can access any context
        else:
            # Modify tools: use first context (first key in dict, or first element in list)
            if isinstance(all_context_data, dict) and len(all_context_data) > 0:
                context_id = list(all_context_data.keys())[0]
            else:
                context_id = None

    # Get context(s) based on context_id
    if context_id == "all":
        # Use all contexts (for query tools that need to access any context)
        if isinstance(all_context_data, dict):
            context_data = list(
                all_context_data.values()
            )  # Return list of all context dicts
        elif isinstance(all_context_data, list):
            context_data = all_context_data
        else:
            context_data = [all_context_data]
    elif isinstance(all_context_data, dict):
        # Context is dict format: {context_id: context_dict, ...}
        if context_id in all_context_data:
            context_data = all_context_data[context_id]  # Get single context dict
        else:
            # Context ID not found, return error
            return {
                "success": False,
                "error": f"Context ID '{context_id}' not found",
                "result": None,
            }
    elif isinstance(all_context_data, list):
        # Context is list format (backward compatibility): [{context_dict}, ...]
        if context_id is None:
            context_data = all_context_data[0] if len(all_context_data) > 0 else {}
        else:
            # Try to find context by user_id field
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
        # Single context dict
        context_data = all_context_data

    # 3. Validate entity references exist in context (CRITICAL!)
    # Helper to get message creation time
    def get_message_time(msg):
        for key in [
            "create_time",
            "created_at",
            "timestamp",
            "ts",
            "createTime",
            "createdAt",
        ]:
            if key in msg:
                val = msg.get(key)
                try:
                    if isinstance(val, (int, float)):
                        return int(val)
                    if isinstance(val, str) and val.isdigit():
                        return int(val)
                except Exception:
                    continue
        return 0

    # Helper to gather all contexts for query operations
    def gather_messages_for_chat(chat_id_value):
        messages = []
        if isinstance(context_data, list):
            for ctx in context_data:
                ch = get_entity_by_path(ctx, "chats", chat_id_value)
                if ch:
                    msgs = list_entities_by_path(
                        ctx, f"chats[{chat_id_value}].messages", {}, 10000
                    )
                    if isinstance(msgs, list):
                        messages.extend(msgs)
        elif isinstance(context_data, dict):
            ch = get_entity_by_path(context_data, "chats", chat_id_value)
            if ch:
                msgs = list_entities_by_path(
                    context_data, f"chats[{chat_id_value}].messages", {}, 10000
                )
                if isinstance(msgs, list):
                    messages.extend(msgs)
        else:
            # Single context dict fallback
            ch = get_entity_by_path(context_data, "chats", chat_id_value)
            if ch:
                msgs = list_entities_by_path(
                    context_data, f"chats[{chat_id_value}].messages", {}, 10000
                )
                if isinstance(msgs, list):
                    messages.extend(msgs)
        return messages

    def gather_all_messages():
        messages = []
        if isinstance(context_data, list):
            for ctx in context_data:
                msgs = list_entities_by_path(ctx, "chats[*].messages", {}, 10000)
                if isinstance(msgs, list):
                    messages.extend(msgs)
        elif isinstance(context_data, dict):
            msgs = list_entities_by_path(context_data, "chats[*].messages", {}, 10000)
            if isinstance(msgs, list):
                messages.extend(msgs)
        else:
            msgs = list_entities_by_path(context_data, "chats[*].messages", {}, 10000)
            if isinstance(msgs, list):
                messages.extend(msgs)
        return messages

    # Validation and listing
    messages_to_consider = []
    if container_id_type == "chat":
        # Verify chat exists
        chat_exists = False
        if isinstance(context_data, list):
            for ctx in context_data:
                if get_entity_by_path(ctx, "chats", container_id):
                    chat_exists = True
                    break
        else:
            if get_entity_by_path(context_data, "chats", container_id):
                chat_exists = True
        if not chat_exists:
            return {
                "success": False,
                "error": f"Invalid chat_id: {container_id}",
                "result": None,
            }

        messages_to_consider = gather_messages_for_chat(container_id)

        # Apply time filtering if provided
        if start_ts is not None or end_ts is not None:
            filtered = []
            for m in messages_to_consider:
                ts = get_message_time(m)
                if start_ts is not None and ts < start_ts:
                    continue
                if end_ts is not None and ts > end_ts:
                    continue
                filtered.append(m)
            messages_to_consider = filtered

    elif container_id_type == "thread":
        # Gather all messages across contexts, then filter by thread_id
        all_msgs = gather_all_messages()
        # Validate thread existence
        found_thread = False
        for m in all_msgs:
            t_id = (
                m.get("thread_id")
                or m.get("threadId")
                or m.get("root_id")
                or m.get("rootId")
            )
            if t_id == container_id:
                found_thread = True
                break
        if not found_thread:
            return {
                "success": False,
                "error": f"Invalid thread_id: {container_id}",
                "result": None,
            }
        # Filter messages by thread id
        messages_to_consider = [
            m
            for m in all_msgs
            if (
                m.get("thread_id")
                or m.get("threadId")
                or m.get("root_id")
                or m.get("rootId")
            )
            == container_id
        ]

    # 4. Perform operation (list) using path-based functions and sorting/pagination
    # Sort messages by create time
    reverse = sort_type == "ByCreateTimeDesc"
    messages_sorted = sorted(
        messages_to_consider, key=lambda m: get_message_time(m), reverse=reverse
    )

    total = len(messages_sorted)
    start_idx = offset
    end_idx = min(offset + page_size, total)
    items = messages_sorted[start_idx:end_idx]
    has_more = end_idx < total
    next_page_token = str(end_idx) if has_more else None

    result_payload = {
        "container_id_type": container_id_type,
        "container_id": container_id,
        "items": items,
        "has_more": has_more,
        "page_token": next_page_token,
        "total": total,
        "sort_type": sort_type,
        "applied_time_filter": {
            "start_time": start_time if container_id_type == "chat" else None,
            "end_time": end_time if container_id_type == "chat" else None,
        },
    }

    # 5. Save context if modified (not applicable here as this is a read-only operation)

    # 6. Format and return response
    return {
        "success": True,
        "error": None,
        "result": {
            "meta": None,
            "content": [
                {"type": "text", "text": json.dumps(result_payload, ensure_ascii=False)}
            ],
            "isError": False,
        },
    }
