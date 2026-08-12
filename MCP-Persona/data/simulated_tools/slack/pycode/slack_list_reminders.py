from pathlib import Path
import json
import os
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
    if parameters_used is None:
        parameters_used = {}
    if not isinstance(parameters_used, dict):
        return {
            "success": False,
            "error": "Invalid input: parameters_used must be an object (dict)",
            "result": None,
        }
    # Input schema has no properties and additionalProperties is false
    if len(parameters_used) > 0:
        unexpected_keys = list(parameters_used.keys())
        return {
            "success": False,
            "error": f"Unexpected parameters: {unexpected_keys}. This tool accepts no parameters.",
            "result": None,
        }

    # 2. Load all context data and select context(s) based on context_id from environment variable
    context_file_path = Path(json.loads(os.environ.get("SLACK_SANDBOX_PATHS"))[0])
    all_context_data = load_context(
        context_file_path
    )  # Returns Dict[{context_id}: {context_dict}] or List[Dict]

    # Read context_id from environment variable (NOT from input parameters)
    context_id = os.environ.get("SLACK_CONTEXT_ID")

    # Determine tool type from tool_name or description to set appropriate default
    tool_name = "slack:list_reminders"
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

    # 3. Validate entity references exist in context (CRITICAL!)
    # For list operation: ensure the reminders container can be accessed; if missing, treat as empty list
    # No filters or parent references required for this tool.

    # 4. Perform operation (list) using path-based functions
    reminders_aggregated = []
    try:
        if isinstance(context_data, list):
            # Aggregating reminders across all available contexts
            for ctx in context_data:
                if not isinstance(ctx, dict):
                    continue
                reminders_list = list_entities_by_path(ctx, "reminders", {}, 10000)
                if reminders_list is None:
                    reminders_list = []
                if isinstance(reminders_list, list):
                    reminders_aggregated.extend(reminders_list)
                else:
                    # If a single entity or unexpected type is returned, normalize to list
                    reminders_aggregated.extend([reminders_list])
        elif isinstance(context_data, dict):
            reminders_list = list_entities_by_path(context_data, "reminders", {}, 10000)
            if reminders_list is None:
                reminders_list = []
            if isinstance(reminders_list, list):
                reminders_aggregated = reminders_list
            else:
                reminders_aggregated = [reminders_list]
        else:
            # Unknown context format; return empty list
            reminders_aggregated = []
    except Exception as e:
        return {
            "success": False,
            "error": f"Failed to list reminders: {str(e)}",
            "result": None,
        }

    # 5. Save context if modified
    # This is a list operation; no modifications to context are performed, so no save is necessary.

    # 6. Format and return response
    tool_result = {"reminders": reminders_aggregated}
    response = {
        "meta": None,
        "content": [{"type": "text", "text": json.dumps(tool_result)}],
        "isError": False,
    }
    return {"success": True, "error": None, "result": response}
