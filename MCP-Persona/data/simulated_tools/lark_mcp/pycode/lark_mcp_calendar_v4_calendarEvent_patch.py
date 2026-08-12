from pathlib import Path
import json
import os
from lark_mcp.dynamic_context_handler import load_context, save_context, get_entity_by_path, list_entities_by_path, create_entity_by_path, update_entity_by_path, delete_entity_by_path

def analyze_response_patterns(parameters_used):
    # 1. Basic input format validation (types, required fields, etc.)
    if not isinstance(parameters_used, dict):
        return {"success": False, "error": "Input parameters must be a dictionary", "result": None}
    path_params = parameters_used.get("path", {})
    if path_params is None:
        path_params = {}
    if not isinstance(path_params, dict):
        return {"success": False, "error": "path must be a dictionary", "result": None}

    calendar_id = path_params.get("calendar_id") or parameters_used.get("calendar_id")
    event_id = path_params.get("event_id") or parameters_used.get("event_id")
    data = parameters_used.get("data", {})
    if data is None:
        data = {}
    if not isinstance(data, dict):
        return {"success": False, "error": "data must be a dictionary", "result": None}

    if not calendar_id:
        return {"success": False, "error": "calendar_id is required", "result": None}
    if not event_id:
        return {"success": False, "error": "event_id is required", "result": None}

    # Validate fields in data
    validation_errors = []
    if "summary" in data and not isinstance(data["summary"], str):
        validation_errors.append("summary must be a string")
    if "description" in data and not isinstance(data["description"], str):
        validation_errors.append("description must be a string")
    if "need_notification" in data and not isinstance(data["need_notification"], bool):
        validation_errors.append("need_notification must be a boolean")
    if "start_time" in data and not isinstance(data["start_time"], dict):
        validation_errors.append("start_time must be an object")
    if "end_time" in data and not isinstance(data["end_time"], dict):
        validation_errors.append("end_time must be an object")

    # Ensure start_time and end_time update together
    if ("start_time" in data) != ("end_time" in data):
        validation_errors.append("start_time and end_time must both be provided to update event times")

    # Validate time fields structure
    def validate_time_obj(name, tobj):
        errs = []
        if tobj is None:
            return errs
        has_date = "date" in tobj and tobj.get("date") is not None
        has_ts = "timestamp" in tobj and tobj.get("timestamp") is not None
        if has_date and has_ts:
            errs.append(f"{name} cannot specify both 'date' and 'timestamp'")
        if "date" in tobj and tobj.get("date") is not None and not isinstance(tobj.get("date"), str):
            errs.append(f"{name}.date must be a string")
        if "timestamp" in tobj and tobj.get("timestamp") is not None and not isinstance(tobj.get("timestamp"), (str, int)):
            errs.append(f"{name}.timestamp must be a string or integer representing seconds")
        if "timezone" in tobj and tobj.get("timezone") is not None and not isinstance(tobj.get("timezone"), str):
            errs.append(f"{name}.timezone must be a string")
        return errs

    if "start_time" in data:
        validation_errors.extend(validate_time_obj("start_time", data.get("start_time")))
    if "end_time" in data:
        validation_errors.extend(validate_time_obj("end_time", data.get("end_time")))

    if validation_errors:
        return {"success": False, "error": "; ".join(validation_errors), "result": None}

    # 2. Load all context data and select context(s) based on context_id from environment variable
    context_file_path = Path(json.loads(os.environ.get("LARK_MCP_SANDBOX_PATHS"))[0])
    all_context_data = load_context(context_file_path)  # Returns Dict[{context_id}: {context_dict}] or List[Dict]
    # Read context_id from environment variable (NOT from input parameters)
    context_id = os.environ.get("LARK_MCP_CONTEXT_ID")
    # Determine tool type from tool_name or description to set appropriate default
    tool_name = "lark-mcp:calendar_v4_calendarEvent_patch"
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

    # For modify tool, context_data must be a single dict, not a list from "all"
    if isinstance(context_data, list):
        return {"success": False, "error": "Updating with LARK_MCP_CONTEXT_ID='all' is not supported. Provide a specific LARK_MCP_CONTEXT_ID for lark_mcp.", "result": None}

    # 3. Validate entity references exist in context (CRITICAL!)
    # Handle "primary" calendar_id special case
    if calendar_id == "primary":
        calendars = list_entities_by_path(context_data, "calendars", {}, 10000)
        primary_calendars = [c for c in calendars if c.get("type") == "primary" or c.get("is_primary") is True]
        if not primary_calendars:
            return {"success": False, "error": "No primary calendar found", "result": None}
        calendar_id = primary_calendars[0].get("calendar_id")

    # Verify calendar exists
    calendar = get_entity_by_path(context_data, "calendars", calendar_id)
    if not calendar:
        return {"success": False, "error": f"Invalid calendar_id: {calendar_id}", "result": None}

    # Verify event exists
    event = get_entity_by_path(context_data, f"calendars[{calendar_id}].events", event_id)
    if not event:
        return {"success": False, "error": f"Event not found: {event_id} in calendar {calendar_id}", "result": None}

    # 4. Perform operation (update) using path-based functions
    updates = {}
    # Only include fields that are provided
    for field in ["summary", "description", "need_notification"]:
        if field in data:
            updates[field] = data[field]

    if "start_time" in data and "end_time" in data:
        updates["start_time"] = data["start_time"]
        updates["end_time"] = data["end_time"]

    # If no recognized fields provided, nothing to update
    if not updates:
        return {"success": False, "error": "No valid fields provided to update", "result": None}

    try:
        update_entity_by_path(context_data, f"calendars[{calendar_id}].events", event_id, updates)
        # Retrieve updated event
        updated_event = get_entity_by_path(context_data, f"calendars[{calendar_id}].events", event_id)
    except Exception as e:
        return {"success": False, "error": f"Update failed: {str(e)}", "result": None}

    # 5. Save context if modified
    # Get context_id from environment variable (same as when loading)
    context_id_save = os.environ.get("LARK_MCP_CONTEXT_ID")
    if context_id_save is None:
        # Use first key if dict, or first element if list
        if isinstance(all_context_data, dict) and len(all_context_data) > 0:
            context_id_save = list(all_context_data.keys())[0]

    # Update the modified context back to all_context_data
    if isinstance(all_context_data, dict):
        # Dict format: {context_id: context_dict, ...}
        if context_id_save and context_id_save != "all" and context_id_save in all_context_data:
            all_context_data[context_id_save] = context_data  # Update the specific context
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

    # 6. Format and return response
    result_payload = {
        "calendar_id": calendar_id,
        "event_id": event_id,
        "updated_event": updated_event
    }
    response = {
        "meta": None,
        "content": [
            {
                "type": "text",
                "text": json.dumps(result_payload, ensure_ascii=False),
            }
        ],
        "isError": False
    }
    return {"success": True, "error": None, "result": response}