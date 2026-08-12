import os
from pathlib import Path
import json
from instagram.pycode.dynamic_context_handler import (
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
        return {
            "success": False,
            "error": "Invalid input: parameters_used must be a dictionary",
            "result": None,
        }
    allowed_keys = {"graph_api_version", "ig_user_id"}
    unknown_keys = [k for k in parameters_used.keys() if k not in allowed_keys]
    if unknown_keys:
        return {
            "success": False,
            "error": f"Unknown parameter(s): {', '.join(unknown_keys)}",
            "result": None,
        }

    graph_api_version = parameters_used.get("graph_api_version", "v21.0")
    if graph_api_version is not None and not isinstance(graph_api_version, str):
        return {
            "success": False,
            "error": "Invalid graph_api_version: must be a string or null",
            "result": None,
        }

    ig_user_id = parameters_used.get("ig_user_id")
    if ig_user_id is not None and not isinstance(ig_user_id, (str, int)):
        return {
            "success": False,
            "error": "Invalid ig_user_id: must be a string or null",
            "result": None,
        }
    if isinstance(ig_user_id, int):
        ig_user_id = str(ig_user_id)

    # 2. Load all context data and select context(s) based on context_id from environment variable
    tool_name = "get_user_info"
    try:
        context_file_path = Path(__file__).parent.parent / "context.json"
        all_context_data = load_context(context_file_path)
    except Exception as e:
        return {
            "success": False,
            "error": f"Failed to load context: {e}",
            "result": None,
        }

    # Read context_id from environment variable (NOT from input parameters)
    context_id = os.environ.get("INSTAGRAM_CONTEXT_ID")

    # Determine tool type from tool_name or description to set appropriate default
    tool_name_lower = tool_name.lower() if tool_name else ""
    is_query_tool = any(
        keyword in tool_name_lower
        for keyword in ["get", "list", "query", "search", "batch"]
    )

    # Get context(s) based on context_id
    context_id = "all"  # Query tools (get/list) can access any context

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

    # 3. Validate entity references and collect user info
    # Context structure: each element in context is a user object with id, username, etc.
    found_users = []

    if isinstance(context_data, list):
        # Multiple contexts (default for query tools)
        if ig_user_id:
            # Search for specific user by id
            for user_info in context_data:
                if isinstance(user_info, dict) and user_info.get("id") == ig_user_id:
                    found_users.append(user_info)
                    break
            if not found_users:
                return {
                    "success": False,
                    "error": f"Invalid ig_user_id: {ig_user_id}",
                    "result": None,
                }
        else:
            # List all users
            for user_info in context_data:
                if isinstance(user_info, dict):
                    found_users.append(user_info)
            if not found_users:
                return {
                    "success": False,
                    "error": "No Instagram user info found in any context",
                    "result": None,
                }
    elif isinstance(context_data, dict):
        # Single context
        if ig_user_id:
            if context_data.get("id") != ig_user_id:
                return {
                    "success": False,
                    "error": f"Invalid ig_user_id: {ig_user_id}",
                    "result": None,
                }
            found_users.append(context_data)
        else:
            found_users.append(context_data)
    else:
        return {"success": False, "error": "Invalid context format", "result": None}

    # 4. Perform operation - data already collected above

    # 5. Save context if modified
    # Query tool doesn't modify context; no saving needed.

    # 6. Format and return response
    # Construct result payload
    result_payload = {
        "graph_api_version": graph_api_version,
        "count": len(found_users),
        "users": found_users if not ig_user_id else found_users[0],
    }

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
