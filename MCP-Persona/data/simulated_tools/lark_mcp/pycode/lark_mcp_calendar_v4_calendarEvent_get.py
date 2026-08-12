from pathlib import Path
import json
import os
from lark_mcp.dynamic_context_handler import load_context, save_context, get_entity_by_path, list_entities_by_path, create_entity_by_path, update_entity_by_path, delete_entity_by_path

def analyze_response_patterns(parameters_used):
    # 1. Basic input format validation (types, required fields, etc.)
    if not isinstance(parameters_used, dict):
        return {"success": False, "error": "Invalid input: parameters_used must be a dict", "result": None}

    path_obj = parameters_used.get("path")
    if not isinstance(path_obj, dict):
        return {"success": False, "error": "Invalid input: 'path' must be provided and must be an object", "result": None}

    calendar_id = path_obj.get("calendar_id")
    event_id = path_obj.get("event_id")

    if not isinstance(calendar_id, str) or not calendar_id.strip():
        return {"success": False, "error": "Invalid input: 'calendar_id' must be a non-empty string", "result": None}
    if not isinstance(event_id, str) or not event_id.strip():
        return {"success": False, "error": "Invalid input: 'event_id' must be a non-empty string", "result": None}

    params_obj = parameters_used.get("params", {})
    if params_obj is None:
        params_obj = {}
    if not isinstance(params_obj, dict):
        return {"success": False, "error": "Invalid input: 'params' must be an object if provided", "result": None}

    need_meeting_settings = params_obj.get("need_meeting_settings", False)
    need_attendee = params_obj.get("need_attendee", False)
    max_attendee_num = params_obj.get("max_attendee_num", None)
    user_id_type = params_obj.get("user_id_type", None)

    if need_meeting_settings is not None and not isinstance(need_meeting_settings, bool):
        return {"success": False, "error": "Invalid input: 'need_meeting_settings' must be boolean", "result": None}
    if need_attendee is not None and not isinstance(need_attendee, bool):
        return {"success": False, "error": "Invalid input: 'need_attendee' must be boolean", "result": None}
    if max_attendee_num is not None:
        if not isinstance(max_attendee_num, (int, float)):
            return {"success": False, "error": "Invalid input: 'max_attendee_num' must be a number", "result": None}
        # Convert to int for slicing
        try:
            max_attendee_num = int(max_attendee_num)
        except Exception:
            return {"success": False, "error": "Invalid input: 'max_attendee_num' must be convertible to integer", "result": None}
        if max_attendee_num < 0:
            max_attendee_num = 0

    if user_id_type is not None:
        if user_id_type not in ["open_id", "union_id", "user_id"]:
            return {"success": False, "error": "Invalid input: 'user_id_type' must be one of ['open_id','union_id','user_id']", "result": None}

    tool_name = "lark-mcp:calendar_v4_calendarEvent_get"

    # 2. Load all context data and select context(s) based on context_id from environment variable
    context_file_path = Path(json.loads(os.environ.get("LARK_MCP_SANDBOX_PATHS"))[0])
    all_context_data = load_context(context_file_path)  # Returns Dict[{context_id}: {context_dict}] or List[Dict]

    # Read context_id from environment variable (NOT from input parameters)
    context_id = os.environ.get("LARK_MCP_CONTEXT_ID")

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

    # Helper to resolve calendar_id (including 'primary')
    def resolve_calendar_id_for_context(ctx, cal_id):
        if not isinstance(ctx, dict):
            return None, "Invalid context format"
        if cal_id == "primary":
            calendars = list_entities_by_path(ctx, "calendars", {}, 10000)
            primary_calendars = [c for c in calendars if c.get("type") == "primary" or c.get("is_primary") is True]
            if not primary_calendars:
                return None, "No primary calendar found"
            return primary_calendars[0].get("calendar_id"), None
        else:
            # Verify calendar exists
            cal = get_entity_by_path(ctx, "calendars", cal_id)
            if not cal:
                return None, f"Invalid calendar_id: {cal_id}"
            return cal_id, None

    # 3. Validate entity references exist in context (CRITICAL!) and find event(s)
    matches = []
    if isinstance(context_data, list):
        for ctx in context_data:
            resolved_cal_id, err = resolve_calendar_id_for_context(ctx, calendar_id)
            if err or not resolved_cal_id:
                # Skip this context if calendar invalid; continue searching others
                continue
            ev = get_entity_by_path(ctx, f"calendars[{resolved_cal_id}].events", event_id)
            if ev:
                matches.append((ctx, resolved_cal_id, ev))
    elif isinstance(context_data, dict):
        resolved_cal_id, err = resolve_calendar_id_for_context(context_data, calendar_id)
        if err or not resolved_cal_id:
            return {"success": False, "error": err or "Invalid calendar context", "result": None}
        ev = get_entity_by_path(context_data, f"calendars[{resolved_cal_id}].events", event_id)
        if ev:
            matches.append((context_data, resolved_cal_id, ev))

    if not matches:
        return {"success": False, "error": f"Event not found for calendar_id '{calendar_id}' and event_id '{event_id}'", "result": None}

    # For query tool, return the first matched event
    _, resolved_calendar_id, event_obj = matches[0]

    # 4. Perform operation (list/create/update/delete) using path-based functions
    # Here it's a 'get' operation, no modification. Process params flags.
    try:
        # Deep copy event object to avoid accidental mutations
        event_copy = json.loads(json.dumps(event_obj,ensure_ascii=False))

        # Meeting settings handling
        if need_meeting_settings:
            # Include meeting/pre-meeting settings only if vc_type == 'vc'
            vc_type = event_copy.get("vc_type") or event_copy.get("meeting_type") or event_copy.get("type")
            if str(vc_type).lower() != "vc":
                # Remove any meeting settings if not VC type
                for k in ["meeting_settings", "vc_meeting_settings", "vc_settings"]:
                    if k in event_copy:
                        del event_copy[k]
            # Else keep whatever settings exist
        else:
            # Ensure meeting settings are not returned if flag is false
            for k in ["meeting_settings", "vc_meeting_settings", "vc_settings"]:
                if k in event_copy:
                    del event_copy[k]

        # Attendee handling
        if need_attendee:
            attendees = event_copy.get("attendees", [])
            if isinstance(attendees, list):
                # Limit number of attendees if max_attendee_num is provided
                if isinstance(max_attendee_num, int):
                    attendees = attendees[:max_attendee_num] if max_attendee_num >= 0 else []
                # Optionally adapt attendee id type
                if user_id_type:
                    adapted_attendees = []
                    for att in attendees:
                        # Copy attendee
                        att_copy = dict(att) if isinstance(att, dict) else {}
                        # If requested id type exists, ensure it's present; otherwise leave as-is
                        # We won't remove other fields; just ensure there's a standardized 'id' field
                        requested_id_value = att_copy.get(user_id_type) or att_copy.get("attendee_id") or att_copy.get("id")
                        if requested_id_value is not None:
                            att_copy["id"] = requested_id_value
                            att_copy["id_type"] = user_id_type
                        adapted_attendees.append(att_copy)
                    attendees = adapted_attendees
                event_copy["attendees"] = attendees
            else:
                # Ensure attendees is a list if need_attendee is True
                event_copy["attendees"] = []
        else:
            # Remove attendee info if flag is false
            if "attendees" in event_copy:
                del event_copy["attendees"]

        # Attach resolved calendar_id context for clarity
        event_copy["_resolved_calendar_id"] = resolved_calendar_id

    except Exception as e:
        return {"success": False, "error": f"Failed to process event data: {str(e)}", "result": None}

    # 5. Save context if modified - not applicable for 'get' operation

    # 6. Format and return response
    result_payload = {
        "meta": None,
        "content": [
            {
                "type": "text",
                "text": json.dumps(event_copy,ensure_ascii=False),
            }
        ],
        "isError": False
    }

    return {"success": True, "error": None, "result": result_payload}