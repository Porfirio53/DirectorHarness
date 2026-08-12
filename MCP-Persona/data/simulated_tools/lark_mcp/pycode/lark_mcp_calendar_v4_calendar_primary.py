from pathlib import Path
import json
import os
from lark_mcp.dynamic_context_handler import load_context, save_context, get_entity_by_path, list_entities_by_path, create_entity_by_path, update_entity_by_path, delete_entity_by_path

def analyze_response_patterns(parameters_used):
    # 1. Basic input format validation (types, required fields, etc.)
    if parameters_used is None:
        parameters_used = {}
    if not isinstance(parameters_used, dict):
        return {"success": False, "error": "Input must be a dictionary", "result": None}
    params = parameters_used.get("params")
    use_uat = parameters_used.get("useUAT")
    if params is not None and not isinstance(params, dict):
        return {"success": False, "error": "params must be an object", "result": None}
    if use_uat is not None and not isinstance(use_uat, bool):
        return {"success": False, "error": "useUAT must be a boolean", "result": None}
    if params:
        user_id_type = params.get("user_id_type")
        if user_id_type is not None and not isinstance(user_id_type, str):
            return {"success": False, "error": "user_id_type must be a string", "result": None}
        if user_id_type is not None and user_id_type not in ["open_id", "union_id", "user_id"]:
            return {"success": False, "error": "user_id_type must be one of ['open_id', 'union_id', 'user_id']", "result": None}

    tool_name = "lark-mcp:calendar_v4_calendar_primary"

    # 2. Load all context data and select context(s) based on context_id from environment variable
    context_file_path = Path(json.loads(os.environ.get("LARK_MCP_SANDBOX_PATHS"))[0])
    all_context_data = load_context(context_file_path)  # Returns Dict[{context_id}: {context_dict}] or List[Dict]

    # Read context_id from environment variable (NOT from input parameters)
    context_id = os.environ.get("LARK_MCP_CONTEXT_ID")

    # Determine tool type from tool_name or description to set appropriate default
    tool_name_lower = tool_name.lower() if tool_name else ""
    is_query_tool = any(keyword in tool_name_lower for keyword in ["get", "list", "query", "search", "batch", "primary"])

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

    # Context selection
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
            return {"success": False, "error": f"Context ID '{context_id}' not found", "result": None}
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
                return {"success": False, "error": f"Context ID '{context_id}' not found", "result": None}
    else:
        # Single context dict
        context_data = all_context_data

    # 3. Validate entity references exist in context (CRITICAL!)
    # For this tool, we need to query the "primary" calendar of the current identity
    def find_primary_calendar(ctx_dict):
        # List all calendars
        calendars = list_entities_by_path(ctx_dict, "calendars", {}, 10000)
        if not isinstance(calendars, list):
            calendars = []
        if len(calendars) == 0:
            return None
        # Try different conventions for marking primary calendar
        primary_candidates = [c for c in calendars if c.get("type") == "primary" or c.get("is_primary") is True or c.get("primary") is True]
        if primary_candidates:
            return primary_candidates[0]
        # Fallback to a calendar explicitly referenced by a field if present
        primary_calendar_id = ctx_dict.get("primary_calendar_id")
        if primary_calendar_id:
            cal = get_entity_by_path(ctx_dict, "calendars", primary_calendar_id)
            if cal:
                return cal
        # If none of the above, no primary calendar found
        return None

    # 4. Perform operation (list/create/update/delete) using path-based functions
    # Since this is a query tool, we only retrieve data and do not modify the context
    try:
        if isinstance(context_data, list):
            # Multiple contexts: gather primary calendars from each
            primary_results = []
            if isinstance(all_context_data, dict):
                # Preserve context IDs
                for cid, ctx in all_context_data.items():
                    if not isinstance(ctx, dict):
                        continue
                    primary_cal = find_primary_calendar(ctx)
                    if primary_cal:
                        primary_results.append({"context_id": cid, "calendar": primary_cal})
            else:
                # List of contexts without context_id keys
                for ctx in context_data:
                    if not isinstance(ctx, dict):
                        continue
                    primary_cal = find_primary_calendar(ctx)
                    if primary_cal:
                        # Try to include user_id if available
                        primary_results.append({"user_id": ctx.get("user_id"), "calendar": primary_cal})
            if len(primary_results) == 0:
                return {"success": False, "error": "No primary calendar found across available contexts", "result": None}
            api_result = {"primary_calendars": primary_results}
        else:
            # Single context dict
            if not isinstance(context_data, dict):
                return {"success": False, "error": "Invalid context format", "result": None}
            primary_calendar = find_primary_calendar(context_data)
            if not primary_calendar:
                return {"success": False, "error": "No primary calendar found", "result": None}
            api_result = primary_calendar
    except Exception as e:
        return {"success": False, "error": f"Failed to query primary calendar: {str(e)}", "result": None}

    # 5. Save context if modified - not applicable for query tool (no modifications)

    # 6. Format and return response
    response_payload = {
        "meta": None,
        "content": [
            {
                "type": "text",
                "text": json.dumps(api_result,ensure_ascii=False),
            }
        ],
        "isError": False
    }
    return {"success": True, "error": None, "result": response_payload}