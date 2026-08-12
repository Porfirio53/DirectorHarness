from pathlib import Path
import json
import os
import uuid
from datetime import datetime
from wecome.pycode.dynamic_context_handler import (
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
            "error": "Invalid input: parameters_used must be a dict",
            "result": None,
        }
    content = parameters_used.get("content")
    if content is None:
        return {
            "success": False,
            "error": "Missing required field: content",
            "result": None,
        }
    if not isinstance(content, str):
        return {
            "success": False,
            "error": "Invalid type for field 'content': expected string",
            "result": None,
        }

    # 2. Load all context data and select context(s) based on context_id from environment variable
    try:
        context_file_path = Path(json.loads(os.environ.get("WECOM_SANDBOX_PATHS"))[0])
        all_context_data = load_context(context_file_path)
    except Exception as e:
        return {
            "success": False,
            "error": f"Failed to load context: {str(e)}",
            "result": None,
        }

    # Read context_id from environment variable (NOT from input parameters)
    context_id = os.environ.get("WECOME_CONTEXT_ID")

    # Determine tool type from tool_name or description to set appropriate default
    tool_name = "wecome:send_markdown"
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
            elif isinstance(all_context_data, list) and len(all_context_data) > 0:
                # For list format without context_id, use first element
                context_id = None  # Will be handled in list branch
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
                found = False
                for ctx in all_context_data:
                    if isinstance(ctx, dict) and ctx.get("user_id") == context_id:
                        context_data = ctx
                        found = True
                        break
                if not found:
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
            "error": f"Error selecting context: {str(e)}",
            "result": None,
        }

    # Prevent modifications when multiple contexts are selected
    if isinstance(context_data, list):
        return {
            "success": False,
            "error": "Cannot modify multiple contexts with wecome:send_markdown. Set WECOME_CONTEXT_ID to a specific context.",
            "result": None,
        }

    if not isinstance(context_data, dict):
        return {
            "success": False,
            "error": "Invalid context format: expected a dictionary for single context",
            "result": None,
        }

    # 3. Validate entity references exist in context (CRITICAL!)
    # For this tool, no external references needed. Ensure messages container exists or will be created by create_entity_by_path.

    # 4. Perform operation (create) using path-based functions
    try:
        message_id = f"msg_{uuid.uuid4().hex}"
        message_entity = {
            "message_id": message_id,
            "content": content,
            "format": "markdown",
            "status": "sent",
            "sent_at": datetime.utcnow().isoformat() + "Z",
            "tool": tool_name,
        }
        # Create message entity under "messages" path
        create_entity_by_path(context_data, "messages", message_entity, message_id)
    except Exception as e:
        return {
            "success": False,
            "error": f"Failed to send markdown message: {str(e)}",
            "result": None,
        }

    # 5. Save context if modified
    try:
        # Get context_id from environment variable (same as when loading)
        context_id_save = os.environ.get("WECOME_CONTEXT_ID")
        if context_id_save is None:
            # Use first key if dict, or first element if list
            if isinstance(all_context_data, dict) and len(all_context_data) > 0:
                context_id_save = list(all_context_data.keys())[0]

        # Update the modified context back to all_context_data
        if isinstance(all_context_data, dict):
            # Dict format: {context_id: context_dict, ...}
            if (
                context_id_save
                and context_id_save != "all"
                and context_id_save in all_context_data
            ):
                all_context_data[context_id_save] = (
                    context_data  # Update the specific context
                )
        elif isinstance(all_context_data, list):
            # List format (backward compatibility): [{context_dict}, ...]
            if isinstance(context_data, dict):
                user_id = context_data.get("user_id")
                if user_id:
                    for i, ctx in enumerate(all_context_data):
                        if isinstance(ctx, dict) and ctx.get("user_id") == user_id:
                            all_context_data[i] = context_data
                            break
        else:
            # Single context dict
            all_context_data = context_data

        save_context(context_file_path, all_context_data)
    except Exception as e:
        return {
            "success": False,
            "error": f"Failed to save context: {str(e)}",
            "result": None,
        }

    # 6. Format and return response
    tool_result = {}  # As per success examples, the tool returns an empty object
    return {
        "success": True,
        "error": None,
        "result": {
            "meta": None,
            "content": [
                {"type": "text", "text": json.dumps(tool_result), "isError": False}
            ],
            "isError": False,
        },
    }
