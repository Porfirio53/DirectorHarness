from pathlib import Path
import json
import os
import uuid
import random
import time
from datetime import datetime
from lark_mcp.dynamic_context_handler import load_context, save_context, get_entity_by_path, list_entities_by_path, create_entity_by_path, update_entity_by_path, delete_entity_by_path


def parse_timestamp_to_unix(timestamp_str: str) -> str:
    """
    Parse timestamp string to Unix timestamp format.

    Args:
        timestamp_str: Input timestamp string (could be Unix timestamp string or date string)

    Returns:
        Unix timestamp as string (e.g., "1691234687")
    """
    # Try to parse as integer first (already a Unix timestamp)
    try:
        int(timestamp_str)
        return timestamp_str
    except ValueError:
        pass

    # Try to parse date string format (e.g., "chosen_date_10:00", "2023-08-05T10:00:00")
    # For simplicity, if it contains "chosen_date" or similar placeholders,
    # generate a timestamp based on current time
    if "chosen_date" in timestamp_str or not timestamp_str.isdigit():
        # Extract time if present, otherwise use current time
        try:
            # Try ISO format
            dt = datetime.fromisoformat(timestamp_str.replace("T", " ").replace("Z", ""))
            return str(int(dt.timestamp()))
        except:
            # Default to current time if parsing fails
            return str(int(time.time()))

    return timestamp_str


def generate_event_id() -> str:
    """
    Generate event ID in format: 32hex_sequence (e.g., "af1682aa0c8b41f8da6e4d9836d1ba43_0")

    Returns:
        Event ID string
    """
    hex_part = uuid.uuid4().hex
    return f"{hex_part}_0"


def generate_random_color() -> int:
    """
    Generate random color integer (negative value like Lark API).

    Returns:
        Random color integer
    """
    return random.randint(-16777216, -1)


def flatten_time_block(time_block: dict) -> dict:
    """
    Convert nested time block to flat format (start_time.timestamp, start_time.timezone).

    Args:
        time_block: Time block with timestamp and timezone

    Returns:
        Flattened time dict with dot-notation keys
    """
    result = {}
    timestamp_value = time_block.get("timestamp", "")
    tz = time_block.get("timezone", "UTC")

    # Convert timestamp to Unix format
    unix_timestamp = parse_timestamp_to_unix(str(timestamp_value))

    result[f"timestamp"] = unix_timestamp
    result[f"timezone"] = tz

    return result


def analyze_response_patterns(parameters_used):
    # 1. Basic input format validation (types, required fields, etc.)
    if not isinstance(parameters_used, dict):
        return {"success": False, "error": "Invalid input: parameters_used must be a dictionary", "result": None}

    tool_name = "lark-mcp:calendar_v4_calendarEvent_create"
    tool_name_lower = tool_name.lower() if tool_name else ""
    is_query_tool = any(keyword in tool_name_lower for keyword in ["get", "list", "query", "search", "batch"])

    # Extract and validate path and calendar_id
    path_params = parameters_used.get("path", {})
    if not isinstance(path_params, dict):
        return {"success": False, "error": "Invalid input: 'path' must be a dictionary", "result": None}

    calendar_id = path_params.get("calendar_id")
    if not calendar_id:
        return {"success": False, "error": "calendar_id is required", "result": None}

    # Extract and validate data
    data = parameters_used.get("data")
    if data is None or not isinstance(data, dict):
        return {"success": False, "error": "Invalid input: 'data' must be provided as a dictionary", "result": None}

    # Required fields for event creation
    summary = data.get("summary")
    if summary is None or not isinstance(summary, str) or summary.strip() == "":
        return {"success": False, "error": "Invalid input: 'summary' (Event title) is required and must be a non-empty string", "result": None}

    start_time = data.get("start_time")
    end_time = data.get("end_time")
    if not isinstance(start_time, dict):
        return {"success": False, "error": "Invalid input: 'start_time' must be a dictionary", "result": None}
    if not isinstance(end_time, dict):
        return {"success": False, "error": "Invalid input: 'end_time' must be a dictionary", "result": None}

    # Validate mutually exclusive fields for start_time and end_time
    def validate_time_block(block, name):
        date = block.get("date")
        timestamp = block.get("timestamp")
        if date and timestamp:
            return f"Invalid input: '{name}' cannot specify both 'date' and 'timestamp' simultaneously"
        if date is None and timestamp is None:
            return f"Invalid input: '{name}' must specify either 'date' or 'timestamp'"
        if date is not None and not isinstance(date, str):
            return f"Invalid input: '{name}.date' must be a string"
        if timestamp is not None and not isinstance(timestamp, str):
            return f"Invalid input: '{name}.timestamp' must be a string"
        tz = block.get("timezone")
        if tz is not None and not isinstance(tz, str):
            return f"Invalid input: '{name}.timezone' must be a string"
        return None

    err = validate_time_block(start_time, "start_time")
    if err:
        return {"success": False, "error": err, "result": None}
    err = validate_time_block(end_time, "end_time")
    if err:
        return {"success": False, "error": err, "result": None}

    need_notification = data.get("need_notification", True)
    if not isinstance(need_notification, bool):
        return {"success": False, "error": "Invalid input: 'need_notification' must be a boolean", "result": None}

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

    # Validate context usage for modify tools
    if not is_query_tool and isinstance(context_data, list):
        return {"success": False, "error": "Modify tools cannot use 'all' contexts. Set LARK_MCP_CONTEXT_ID to a specific lark_mcp context id.", "result": None}

    if not isinstance(context_data, dict):
        return {"success": False, "error": "Invalid context data structure for modification", "result": None}

    # 3. Validate entity references exist in context (CRITICAL!)
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

    # 4. Perform operation (create) using path-based functions
    event_id = generate_event_id()

    # Generate creation time (current Unix timestamp)
    create_time = str(int(time.time()))

    # Flatten time blocks to match Lark API format
    start_time_flat = flatten_time_block(start_time)
    end_time_flat = flatten_time_block(end_time)

    # Get user information from context
    user_id = context_data.get("user_id", "unknown")
    user_name = context_data.get("name", "Unknown User")

    # Generate attendee ID (random format)
    attendee_id = f"user_{random.randint(1000000000000000000, 9999999999999999999)}"

    # Build attendees structure
    attendees = {
        attendee_id: {
            "attendee_id": attendee_id,
            "user_id": user_id,
            "name": user_name,
            "is_organizer": True
        }
    }

    # Build app_link
    # Format: https://applink.feishu.cn/client/calendar/event/detail?calendarId={calendar_id}&key={event_id}&originalTime={create_time}&startTime={start_time.timestamp}
    app_link = f"https://applink.feishu.cn/client/calendar/event/detail?calendarId={calendar_id}&key={event_id}&originalTime={create_time}&startTime={start_time_flat['timestamp']}"

    # Prepare event data matching Lark API format
    event_data = {
        "event_id": event_id,
        "summary": summary,
        "description": data.get("description"),
        "color": generate_random_color(),
        "visibility": data.get("visibility", "default"),
        "free_busy_status": "busy",
        "status": "tentative",
        "is_exception": False,
        "attendee_ability": "can_invite_others",
        "recurrence": "",
        "create_time": create_time,
        "start_time.timestamp": start_time_flat["timestamp"],
        "start_time.timezone": start_time_flat["timezone"],
        "end_time.timestamp": end_time_flat["timestamp"],
        "end_time.timezone": end_time_flat["timezone"],
        "attendees": attendees,
        "app_link": app_link,
    }

    # Include any additional provided fields that are not validated explicitly but present in 'data'
    for k, v in data.items():
        if k not in event_data and k not in ["start_time", "end_time", "summary", "description", "need_notification"]:
            event_data[k] = v

    # Create event in the specified calendar
    events_path = f"calendars[{calendar_id}].events"
    created_event = create_entity_by_path(context_data, events_path, event_data, event_id)
    if not created_event:
        # Try to fetch the created event to confirm creation
        created_event = get_entity_by_path(context_data, events_path, event_id)
        if not created_event:
            return {"success": False, "error": "Failed to create event due to unknown error", "result": None}

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
        "meta": None,
        "content": [
            {
                "type": "text",
                "text": json.dumps(created_event,ensure_ascii=False)
            }
        ],
        "isError": False
    }

    return {"success": True, "error": None, "result": result_payload}