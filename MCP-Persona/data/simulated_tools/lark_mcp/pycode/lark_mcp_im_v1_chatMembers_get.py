from pathlib import Path
import json
import os
from lark_mcp.dynamic_context_handler import load_context, save_context, get_entity_by_path, list_entities_by_path, create_entity_by_path, update_entity_by_path, delete_entity_by_path

def analyze_response_patterns(parameters_used):
    # 1. Basic input format validation (types, required fields, etc.)
    if parameters_used is None:
        parameters_used = {}
    if not isinstance(parameters_used, dict):
        return {"success": False, "error": "Invalid input: parameters_used must be an object", "result": None}

    path_input = parameters_used.get("path", {})
    params_input = parameters_used.get("params", {})
    use_uat = parameters_used.get("useUAT", None)

    if path_input is None or not isinstance(path_input, dict):
        return {"success": False, "error": "Invalid input: 'path' must be an object", "result": None}

    chat_id = path_input.get("chat_id")
    if not chat_id or not isinstance(chat_id, str):
        return {"success": False, "error": "chat_id is required and must be a string", "result": None}

    # Validate params
    if params_input is not None and not isinstance(params_input, dict):
        return {"success": False, "error": "Invalid input: 'params' must be an object if provided", "result": None}

    member_id_type = None
    page_size = None
    page_token = None

    if isinstance(params_input, dict):
        member_id_type = params_input.get("member_id_type")
        if member_id_type is not None and member_id_type not in ["open_id", "union_id", "user_id"]:
            return {"success": False, "error": "member_id_type must be one of: open_id, union_id, user_id", "result": None}

        page_size = params_input.get("page_size", None)
        if page_size is not None:
            if not isinstance(page_size, (int, float)):
                return {"success": False, "error": "page_size must be a number", "result": None}
            if page_size <= 0:
                return {"success": False, "error": "page_size must be a positive number", "result": None}
            page_size = int(page_size)
        else:
            page_size = 50  # default page size

        page_token = params_input.get("page_token", None)
        if page_token is not None and not isinstance(page_token, str):
            return {"success": False, "error": "page_token must be a string", "result": None}

    # 2. Load all context data and select context(s) based on context_id from environment variable
    try:
        context_file_path = Path(json.loads(os.environ.get("LARK_MCP_SANDBOX_PATHS"))[0])
    except Exception as e:
        return {"success": False, "error": f"Failed to resolve context file path: {e}", "result": None}

    try:
        all_context_data = load_context(context_file_path)
    except Exception as e:
        return {"success": False, "error": f"Failed to load context: {e}", "result": None}

    # Read context_id from environment variable (NOT from input parameters)
    context_id = os.environ.get("LARK_MCP_CONTEXT_ID")

    # Determine tool type from tool_name or description to set appropriate default
    tool_name = "lark-mcp:im_v1_chatMembers_get"
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
            return {"success": False, "error": f"Context ID '{{context_id}}' not found", "result": None}
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
                return {"success": False, "error": f"Context ID '{{context_id}}' not found", "result": None}
    else:
        # Single context dict
        context_data = all_context_data

    # 3. Validate entity references exist in context (CRITICAL!)
    # We must find the chat by chat_id in the selected context(s)
    found_context = None
    try:
        if isinstance(context_data, list):
            for ctx in context_data:
                try:
                    chat_obj = get_entity_by_path(ctx, "chats", chat_id)
                except Exception:
                    chat_obj = None
                if chat_obj:
                    found_context = ctx
                    break
            if found_context is None:
                return {"success": False, "error": f"Invalid chat_id: {chat_id}", "result": None}
        elif isinstance(context_data, dict):
            chat_obj = get_entity_by_path(context_data, "chats", chat_id)
            if not chat_obj:
                return {"success": False, "error": f"Invalid chat_id: {chat_id}", "result": None}
            found_context = context_data
        else:
            # Unsupported context format
            return {"success": False, "error": "Invalid context data format", "result": None}
    except Exception as e:
        return {"success": False, "error": f"Error validating chat_id: {e}", "result": None}

    # 4. Perform operation (list)
    # List all members under the chat
    try:
        all_members = list_entities_by_path(found_context, f"chats[{chat_id}].members", {}, 10000)
    except Exception as e:
        return {"success": False, "error": f"Failed to list members for chat_id {chat_id}: {e}", "result": None}

    # Filter out robot members if possible
    filtered_members = []
    for m in all_members:
        # Heuristic filters for bots/robots
        is_bot = False
        if isinstance(m, dict):
            if m.get("is_bot") is True:
                is_bot = True
            elif str(m.get("member_type", "")).lower() in ("bot", "robot"):
                is_bot = True
            elif str(m.get("type", "")).lower() in ("bot", "robot"):
                is_bot = True
        if not is_bot:
            filtered_members.append(m)

    # Pagination
    total = len(filtered_members)
    start_index = 0
    if page_token:
        try:
            start_index = int(page_token)
            if start_index < 0:
                return {"success": False, "error": "page_token must be a non-negative integer string", "result": None}
        except ValueError:
            return {"success": False, "error": "Invalid page_token format", "result": None}

    end_index = start_index + page_size
    page_items = filtered_members[start_index:end_index]
    has_more = end_index < total
    next_token = str(end_index) if has_more else ""

    # Optionally handle member_id_type (no transformation due to context unknowns)
    # We'll annotate the result with the requested member_id_type for clarity
    response_payload = {
        "members": page_items,
        "has_more": has_more,
        "page_token": next_token,
        "total": total,
        "member_id_type": member_id_type if member_id_type else None,
        "chat_id": chat_id
    }

    # 5. Save context if modified (not applicable for list operation; no changes made)

    # 6. Format and return response
    result_wrapper = {
        "meta": None,
        "content": [
            {
                "type": "text",
                "text": json.dumps(response_payload, ensure_ascii=False)
            }
        ],
        "isError": False
    }
    return {"success": True, "error": None, "result": result_wrapper}