import os
from pathlib import Path
import json
from instagram.pycode.dynamic_context_handler import load_context, save_context, get_entity_by_path, list_entities_by_path, create_entity_by_path, update_entity_by_path, delete_entity_by_path

def analyze_response_patterns(parameters_used):
    # 1. Basic input format validation (types, required fields, etc.)
    tool_name = "list_all_conversations"
    if parameters_used is None:
        parameters_used = {}
    if not isinstance(parameters_used, dict):
        return {"success": False, "error": "Invalid input: parameters_used must be an object", "result": None}

    allowed_keys = {"after", "graph_api_version", "ig_user_id", "limit"}
    extra_keys = set(parameters_used.keys()) - allowed_keys
    if extra_keys:
        return {"success": False, "error": f"Invalid input: unexpected fields {sorted(list(extra_keys))}", "result": None}

    after = parameters_used.get("after", None)
    graph_api_version = parameters_used.get("graph_api_version", "v21.0")
    ig_user_id = parameters_used.get("ig_user_id", None)
    limit = parameters_used.get("limit", 25)

    # Validate graph_api_version
    if graph_api_version is None:
        graph_api_version = "v21.0"
    elif not isinstance(graph_api_version, str):
        return {"success": False, "error": "Invalid input: graph_api_version must be a string or null", "result": None}

    # Validate after
    if after is not None and not isinstance(after, str):
        return {"success": False, "error": "Invalid input: 'after' must be a string or null", "result": None}

    # Validate ig_user_id
    if ig_user_id is not None and not isinstance(ig_user_id, str):
        return {"success": False, "error": "Invalid input: 'ig_user_id' must be a string or null", "result": None}

    # Validate limit
    if limit is None:
        limit = 25
    elif not isinstance(limit, int):
        return {"success": False, "error": "Invalid input: 'limit' must be an integer or null", "result": None}
    elif not (1 <= limit <= 200):
        return {"success": False, "error": "Invalid input: 'limit' must be between 1 and 200", "result": None}

    # 2. Load all context data and select context(s) based on context_id from environment variable
    context_file_path = Path(__file__).parent.parent / "context.json"
    all_context_data = load_context(context_file_path)

    # Read context_id from os.environ.get("INSTAGRAM_CONTEXT_ID")
    context_id = os.environ.get("INSTAGRAM_CONTEXT_ID")

    # Determine tool type from tool_name or description to set appropriate default
    tool_name_lower = tool_name.lower() if tool_name else ""
    is_query_tool = any(keyword in tool_name_lower for keyword in ["get", "list", "query", "search", "batch"])

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
            context_data = list(all_context_data.values())  # Return list of all context dicts
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
            return {"success": False, "error": f"Context ID '{context_id}' not found", "result": None}
    elif isinstance(all_context_data, list):
        # Context is list format (backward compatibility): [{context_dict}, ...]
        if context_id is None:
            context_data = all_context_data[0] if len(all_context_data) > 0 else {}
        else:
            # Try to find context by id field
            for ctx in all_context_data:
                if isinstance(ctx, dict) and ctx.get("id") == context_id:
                    context_data = ctx
                    break
            else:
                return {"success": False, "error": f"Context ID '{context_id}' not found", "result": None}
    else:
        # Single context dict
        context_data = all_context_data

    # Normalize contexts to a list for unified processing
    if isinstance(context_data, list):
        contexts = [ctx for ctx in context_data if isinstance(ctx, dict)]
    else:
        contexts = [context_data if isinstance(context_data, dict) else {}]

    # 3. Validate entity references exist in context (CRITICAL!)
    # Validate ig_user_id exists if provided
    if ig_user_id is not None:
        found_any = False
        for ctx in contexts:
            if isinstance(ctx, dict) and ctx.get("id") == ig_user_id:
                found_any = True
                break
        if not found_any:
            return {"success": False, "error": f"Invalid ig_user_id: {ig_user_id}", "result": None}

    # 4. Perform operation (list) - collect conversations from user objects
    all_conversations = []

    # Helper for deduplication
    def conversation_unique_id(conv):
        if not isinstance(conv, dict):
            return None
        for key in ("id", "thread_id", "conversation_id"):
            if key in conv and isinstance(conv[key], (str, int)):
                return f"{key}:{conv[key]}"
        return None

    # Collect conversations across contexts
    seen_ids = set()
    for ctx in contexts:
        if not isinstance(ctx, dict):
            continue
        # Filter by ig_user_id if provided
        if ig_user_id is not None and ctx.get("id") != ig_user_id:
            continue
        # Access conversations directly from user object
        if "conversations" in ctx and isinstance(ctx["conversations"], dict):
            for conv in ctx["conversations"].values():
                uid = conversation_unique_id(conv)
                if uid:
                    if uid in seen_ids:
                        continue
                    seen_ids.add(uid)
                all_conversations.append(conv)

    # 5. Apply pagination based on 'after' and 'limit'
    def find_index_by_after(token, items):
        # Search by id match first
        if token is None:
            return 0
        # Find by id in known keys
        for idx, item in enumerate(items):
            if isinstance(item, dict):
                for key in ("id", "thread_id", "conversation_id"):
                    val = item.get(key)
                    if val is not None and str(val) == token:
                        return idx + 1  # start after the matched item
        # Fallback: if token is a valid non-negative integer within range, use as index
        try:
            idx_num = int(token)
            if 0 <= idx_num < len(items):
                return idx_num
        except Exception:
            pass
        return None  # Invalid token

    start_index = find_index_by_after(after, all_conversations)
    if start_index is None:
        return {"success": False, "error": f"Invalid 'after' cursor: {after}", "result": None}

    page_size = limit if isinstance(limit, int) else 25
    page_items = all_conversations[start_index:start_index + page_size]

    # Determine next cursor
    remaining = len(all_conversations) - (start_index + len(page_items))
    has_more = remaining > 0
    next_after = None
    if has_more and len(page_items) > 0:
        last_item = page_items[-1]
        # Prefer using id-based cursor
        last_id = None
        if isinstance(last_item, dict):
            for key in ("id", "thread_id", "conversation_id"):
                if key in last_item:
                    last_id = str(last_item[key])
                    break
        next_after = last_id if last_id is not None else str(start_index + len(page_items))

    # 6. Format and return response
    api_result = {
        "data": page_items,
        "paging": {
            "cursors": {"after": next_after} if next_after is not None else {},
            "has_more": has_more
        },
        "summary": {
            "total_count": len(all_conversations)
        },
        "graph_api_version": graph_api_version,
        "ig_user_id": ig_user_id if ig_user_id is not None else "me"
    }

    response = {
        "success": True,
        "error": None,
        "result": {
            "meta": None,
            "content": [
                {
                    "type": "text",
                    "text": json.dumps(api_result,ensure_ascii=False)
                }
            ],
            "isError": False
        }
    }

    return response