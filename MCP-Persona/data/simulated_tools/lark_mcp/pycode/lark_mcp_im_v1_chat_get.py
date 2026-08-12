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
    tool_name = "lark-mcp:im_v1_chat_get"
    allowed_user_id_types = {"open_id", "union_id", "user_id"}

    if not isinstance(parameters_used, dict):
        return {"success": False, "error": "Input must be a dictionary", "result": None}

    path_input = parameters_used.get("path")
    if not isinstance(path_input, dict):
        return {"success": False, "error": "path must be an object", "result": None}

    chat_id = path_input.get("chat_id")
    if not isinstance(chat_id, str) or not chat_id.strip():
        return {
            "success": False,
            "error": "chat_id is required and must be a non-empty string",
            "result": None,
        }

    params_input = parameters_used.get("params")
    if params_input is not None:
        if not isinstance(params_input, dict):
            return {
                "success": False,
                "error": "params must be an object",
                "result": None,
            }
        user_id_type = params_input.get("user_id_type")
        if user_id_type is not None and user_id_type not in allowed_user_id_types:
            return {
                "success": False,
                "error": f"user_id_type must be one of {sorted(list(allowed_user_id_types))}",
                "result": None,
            }

    use_uat = parameters_used.get("useUAT")
    if use_uat is not None and not isinstance(use_uat, bool):
        return {"success": False, "error": "useUAT must be a boolean", "result": None}

    # 2. Load all context data and select context(s) based on context_id from environment variable
    context_file_path = Path(json.loads(os.environ.get("LARK_MCP_SANDBOX_PATHS"))[0])
    all_context_data = load_context(
        context_file_path
    )  # Returns Dict[{context_id}: {context_dict}] or List[Dict]

    # Read context_id from environment variable (NOT from input parameters)
    context_id = os.environ.get("LARK_MCP_CONTEXT_ID")

    # Determine tool type from tool_name or description to set appropriate default
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
    found_chat = None
    if isinstance(context_data, list):
        # Searching across multiple contexts
        for ctx in context_data:
            if not isinstance(ctx, dict):
                continue
            chat = get_entity_by_path(ctx, "chats", chat_id)
            if chat:
                found_chat = chat
                break
        if not found_chat:
            return {
                "success": False,
                "error": f"Invalid chat_id: {chat_id}",
                "result": None,
            }
    else:
        # Single context
        found_chat = get_entity_by_path(context_data, "chats", chat_id)
        if not found_chat:
            return {
                "success": False,
                "error": f"Invalid chat_id: {chat_id}",
                "result": None,
            }

    # 4. Perform operation (get) using path-based functions
    # For a GET operation, we simply return the found chat entity.
    result_payload = found_chat

    # 5. Save context if modified
    # Not applicable for GET; no modifications performed.

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
