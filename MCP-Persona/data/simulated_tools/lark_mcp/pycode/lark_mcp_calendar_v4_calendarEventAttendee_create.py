from pathlib import Path
import json
import os
import uuid
import random
from lark_mcp.dynamic_context_handler import load_context, save_context, get_entity_by_path, list_entities_by_path, create_entity_by_path, update_entity_by_path, delete_entity_by_path

def analyze_response_patterns(parameters_used):
    # 1. Basic input format validation (types, required fields, etc.)
    try:
        if not isinstance(parameters_used, dict):
            return {"success": False, "error": "parameters_used must be a dictionary", "result": None}
    except Exception as e:
        return {"success": False, "error": f"Invalid input: {str(e)}", "result": None}

    tool_name = "lark-mcp:calendar_v4_calendarEventAttendee_create"

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

    # For modify tools, ensure we have a single context dict, not list
    if not is_query_tool and isinstance(context_data, list):
        return {"success": False, "error": "Modify tools cannot operate on 'all' contexts. Set a specific LARK_MCP_CONTEXT_ID for lark_mcp.", "result": None}

    if not isinstance(context_data, dict):
        return {"success": False, "error": "Invalid context format. Expected a dictionary for modification operations.", "result": None}

    # 3. Validate entity references exist in context (CRITICAL!)
    # Extract calendar_id and event_id from parameters_used
    path_params = parameters_used.get("path", {}) if isinstance(parameters_used.get("path", {}), dict) else {}
    calendar_id = path_params.get("calendar_id") or parameters_used.get("calendar_id")
    event_id = path_params.get("event_id") or parameters_used.get("event_id")

    if not calendar_id:
        return {"success": False, "error": "calendar_id is required", "result": None}
    if not event_id:
        return {"success": False, "error": "event_id is required", "result": None}

    # Handle "primary" special case for calendar_id
    if calendar_id == "primary":
        calendars = list_entities_by_path(context_data, "calendars", {}, 10000)
        primary_calendars = [c for c in calendars if isinstance(c, dict) and c.get("type") == "primary"]
        if not primary_calendars:
            return {"success": False, "error": "No primary calendar found", "result": None}
        calendar_id = primary_calendars[0].get("calendar_id")

    # Verify calendar exists
    calendar = get_entity_by_path(context_data, "calendars", calendar_id)
    if not calendar:
        return {"success": False, "error": f"Invalid calendar_id: {calendar_id}", "result": None}

    # Verify event exists under the calendar
    event = get_entity_by_path(context_data, f"calendars[{calendar_id}].events", event_id)
    if not event:
        return {"success": False, "error": f"Invalid event_id: {event_id} under calendar_id: {calendar_id}", "result": None}

    # 4. Perform operation (create attendees)
    data = parameters_used.get("data")
    if data is None or not isinstance(data, dict):
        return {"success": False, "error": "data is required and must be an object", "result": None}

    attendees = data.get("attendees")
    if attendees is None or not isinstance(attendees, list):
        return {"success": False, "error": "data.attendees is required and must be an array", "result": None}
    if len(attendees) == 0:
        return {"success": False, "error": "data.attendees must contain at least one attendee", "result": None}

    allowed_types = {"user", "chat", "resource", "third_party"}
    validated_attendees = []
    for idx, att in enumerate(attendees):
        if not isinstance(att, dict):
            return {"success": False, "error": f"attendees[{idx}] must be an object", "result": None}
        att_type = att.get("type")
        if att_type not in allowed_types:
            return {"success": False, "error": f"attendees[{idx}].type must be one of {sorted(list(allowed_types))}", "result": None}

        # Validate required fields per type
        if att_type == "user":
            if not att.get("user_id") or not isinstance(att.get("user_id"), str):
                return {"success": False, "error": f"attendees[{idx}].user_id is required for type 'user'", "result": None}
        elif att_type == "chat":
            if not att.get("chat_id") or not isinstance(att.get("chat_id"), str):
                return {"success": False, "error": f"attendees[{idx}].chat_id is required for type 'chat'", "result": None}
        elif att_type == "resource":
            if not att.get("room_id") or not isinstance(att.get("room_id"), str):
                return {"success": False, "error": f"attendees[{idx}].room_id is required for type 'resource'", "result": None}
            # operate_id optional; cannot validate further without identity context
        elif att_type == "third_party":
            if not att.get("third_party_email") or not isinstance(att.get("third_party_email"), str):
                return {"success": False, "error": f"attendees[{idx}].third_party_email is required for type 'third_party'", "result": None}

        # is_optional validation if present
        if "is_optional" in att and not isinstance(att["is_optional"], bool):
            return {"success": False, "error": f"attendees[{idx}].is_optional must be a boolean if provided", "result": None}

        validated_attendees.append(att)

    created_attendees = []
    try:
        for att in validated_attendees:
            attendee_id = att.get("attendee_id")
            if not attendee_id or not isinstance(attendee_id, str):
                attendee_id = f"user_{random.randint(1000000000000000000, 9999999999999999999)}"

            # Construct entity data to store
            entity_data = dict(att)
            entity_data["attendee_id"] = attendee_id
            # Set default is_organizer to False
            if "is_organizer" not in entity_data:
                entity_data["is_organizer"] = False

            # If type is "user", try to get name from all_context_data
            if att.get("type") == "user" and "name" not in entity_data:
                user_id_to_find = att.get("user_id")
                if user_id_to_find:
                    # Search for user in all_context_data
                    if isinstance(all_context_data, dict):
                        for ctx_id, ctx in all_context_data.items():
                            if isinstance(ctx, dict) and ctx.get("user_id") == user_id_to_find:
                                entity_data["name"] = ctx.get("name", "")
                                break
                    elif isinstance(all_context_data, list):
                        for ctx in all_context_data:
                            if isinstance(ctx, dict) and ctx.get("user_id") == user_id_to_find:
                                entity_data["name"] = ctx.get("name", "")
                                break

            created = create_entity_by_path(
                context_data,
                f"calendars[{calendar_id}].events[{event_id}].attendees",
                entity_data,
                attendee_id
            )
            # Fallback to fetching after creation if create doesn't return entity
            if not created:
                created = get_entity_by_path(
                    context_data,
                    f"calendars[{calendar_id}].events[{event_id}].attendees",
                    attendee_id
                )
            if created:
                created_attendees.append(created)
            else:
                # If still not found, raise an error
                return {"success": False, "error": f"Failed to create attendee with id {attendee_id}", "result": None}
    except Exception as e:
        return {"success": False, "error": f"Error creating attendees: {str(e)}", "result": None}

    # 5. Save context if modified
    try:
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
    except Exception as e:
        return {"success": False, "error": f"Failed to save context: {str(e)}", "result": None}

    # 6. Format and return response
    response_payload = {
        "calendar_id": calendar_id,
        "event_id": event_id,
        "created_attendees": created_attendees
    }
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