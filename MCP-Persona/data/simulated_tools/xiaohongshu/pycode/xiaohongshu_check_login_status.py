from pathlib import Path
import os
import json
from xiaohongshu.pycode.dynamic_context_handler import (
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
    if parameters_used is None:
        parameters_used = {}
    if not isinstance(parameters_used, dict):
        return {"success": False, "error": "Input must be an object", "result": None}
    if len(parameters_used.keys()) > 0:
        return {
            "success": False,
            "error": "No input parameters are expected for this tool",
            "result": None,
        }

    # 2. Load all context data and select context(s) based on context_id from environment variable
    try:
        context_file_path = Path(
            json.loads(os.environ.get("XIAOHONGSHU_SANDBOX_PATHS"))[0]
        )
    except Exception as e:
        return {
            "success": False,
            "error": f"Failed to resolve context file path: {e}",
            "result": None,
        }

    try:
        all_context_data = load_context(context_file_path)
    except Exception as e:
        return {
            "success": False,
            "error": f"Failed to load context: {e}",
            "result": None,
        }

    # Read context_id from environment variable (NOT from input parameters)
    context_id = os.environ.get("XIAOHONGSHU_CONTEXT_ID")

    # Determine tool type from tool_name or description to set appropriate default
    tool_name = "xiaohongshu:check_login_status"
    tool_description = "检查小红书登录状态"
    tool_name_lower = tool_name.lower() if tool_name else ""
    description_lower = tool_description.lower() if tool_description else ""
    # Treat 'check' and 'status' as query indicators in addition to the common ones
    is_query_tool = any(
        keyword in (tool_name_lower + " " + description_lower)
        for keyword in [
            "get",
            "list",
            "query",
            "search",
            "batch",
            "check",
            "status",
            "login",
        ]
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
    try:
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
    except Exception as e:
        return {
            "success": False,
            "error": f"Error selecting context: {e}",
            "result": None,
        }

    # 3. Validate entity references exist in context (CRITICAL!) - not applicable for this tool
    # This tool checks login status; there are no specific entity references to validate.

    # 4. Perform operation (list/create/update/delete) using path-based functions
    # For xiaohongshu:check_login_status, the expected successful output examples are empty objects {}.
    # We'll return {} to simulate a successful check.
    # If in future context structure includes explicit login state, such checks could be added here.

    try:
        result_payload = {}
    except Exception as e:
        return {
            "success": False,
            "error": f"Failed to compute result: {e}",
            "result": None,
        }

    # 5. Save context if modified - not needed for this read-only check operation

    # 6. Format and return response
    try:
        response = {
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
        return response
    except Exception as e:
        return {
            "success": False,
            "error": f"Failed to format response: {e}",
            "result": None,
        }
