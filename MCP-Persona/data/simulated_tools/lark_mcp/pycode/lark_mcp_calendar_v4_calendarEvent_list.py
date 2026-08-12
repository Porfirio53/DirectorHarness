from pathlib import Path
import json
import os
from lark_mcp.dynamic_context_handler import load_context, save_context, get_entity_by_path, list_entities_by_path, create_entity_by_path, update_entity_by_path, delete_entity_by_path

def analyze_response_patterns(parameters_used):
    # 1. Basic input format validation
    if not isinstance(parameters_used, dict):
        return {"success": False, "error": "Invalid input: parameters_used must be a dict", "result": None}

    params = parameters_used.get("params", {})
    path_params = parameters_used.get("path", {})
    if params is None:
        params = {}
    if path_params is None:
        path_params = {}
    if not isinstance(params, dict) or not isinstance(path_params, dict):
        return {"success": False, "error": "Invalid input: 'params' and 'path' must be dicts", "result": None}

    # Extract pagination and filtering parameters
    page_size = params.get("page_size")
    anchor_time = params.get("anchor_time")
    page_token = params.get("page_token")
    sync_token = params.get("sync_token")
    start_time = params.get("start_time")
    end_time = params.get("end_time")

    # calendar_id may be in path or params
    calendar_id = path_params.get("calendar_id") or params.get("calendar_id")

    # Validate page_size
    if page_size is not None:
        if isinstance(page_size, (int, float)):
            try:
                page_size = int(page_size)
            except Exception:
                return {"success": False, "error": "Invalid page_size: must be an integer", "result": None}
            if page_size <= 0:
                return {"success": False, "error": "Invalid page_size: must be > 0", "result": None}
        else:
            return {"success": False, "error": "Invalid page_size: must be a number", "result": None}
    else:
        page_size = 100  # default page_size for simulation

    # Validate string parameters
    for name, val in [("anchor_time", anchor_time), ("page_token", page_token), ("sync_token", sync_token), ("start_time", start_time), ("end_time", end_time)]:
        if val is not None and not isinstance(val, str):
            return {"success": False, "error": f"Invalid {name}: must be a string", "result": None}

    # Validate calendar_id presence
    if not calendar_id:
        return {"success": False, "error": "Missing required parameter: calendar_id", "result": None}

    # Validate mutual exclusions and requirements
    if anchor_time and (start_time or end_time):
        return {"success": False, "error": "Invalid parameters: anchor_time cannot be used together with start_time or end_time", "result": None}
    if (start_time and not end_time) or (end_time and not start_time):
        return {"success": False, "error": "Invalid parameters: start_time and end_time must be used together", "result": None}
    if (start_time and end_time) and (page_token or sync_token):
        return {"success": False, "error": "Invalid parameters: start_time/end_time cannot be used together with page_token or sync_token", "result": None}

    # 2. Load all context data and select context(s) based on context_id from environment variable
    context_file_path = Path(json.loads(os.environ.get("LARK_MCP_SANDBOX_PATHS"))[0])
    all_context_data = load_context(context_file_path)  # Returns Dict[{context_id}: {context_dict}] or List[Dict]

    # Determine tool type from tool_name to set appropriate default
    tool_name = "lark-mcp:calendar_v4_calendarEvent_list"
    tool_name_lower = tool_name.lower() if tool_name else ""
    is_query_tool = any(keyword in tool_name_lower for keyword in ["get", "list", "query", "search", "batch"])

    # Read context_id from environment variable
    context_id = os.environ.get("LARK_MCP_CONTEXT_ID")

    # Default behavior based on tool type
    if context_id is None:
        if is_query_tool:
            context_id = "all"
        else:
            if isinstance(all_context_data, dict) and len(all_context_data) > 0:
                context_id = list(all_context_data.keys())[0]
            else:
                context_id = None

    # Get context(s) based on context_id
    if context_id == "all":
        if isinstance(all_context_data, dict):
            context_data = list(all_context_data.values())
        elif isinstance(all_context_data, list):
            context_data = all_context_data
        else:
            context_data = [all_context_data]
    elif isinstance(all_context_data, dict):
        if context_id in all_context_data:
            context_data = all_context_data[context_id]
        else:
            return {"success": False, "error": f"Context ID '{context_id}' not found", "result": None}
    elif isinstance(all_context_data, list):
        if context_id is None:
            context_data = all_context_data[0] if len(all_context_data) > 0 else {}
        else:
            for ctx in all_context_data:
                if isinstance(ctx, dict) and ctx.get("user_id") == context_id:
                    context_data = ctx
                    break
            else:
                return {"success": False, "error": f"Context ID '{context_id}' not found", "result": None}
    else:
        context_data = all_context_data

    # 3. Validate entity references exist in context
    def parse_ts(val):
        if val is None:
            return None
        if isinstance(val, (int, float)):
            return int(val)
        if isinstance(val, str):
            try:
                return int(val.strip())
            except Exception:
                return None
        return None

    def extract_event_times(e):
        # Try various possible structures for start and end
        start_ts = None
        end_ts = None

        # Common flat fields
        st = e.get("start_time")
        et = e.get("end_time")
        start_ts = parse_ts(st) if st is not None else None
        end_ts = parse_ts(et) if et is not None else None

        # Nested fields (e.g., {"start": {"timestamp": "12345"}, "end": {"timestamp": "12346"}})
        if start_ts is None:
            start_obj = e.get("start")
            if isinstance(start_obj, dict):
                start_ts = parse_ts(start_obj.get("timestamp")) or parse_ts(start_obj.get("time")) or parse_ts(start_obj.get("ts"))
        if end_ts is None:
            end_obj = e.get("end")
            if isinstance(end_obj, dict):
                end_ts = parse_ts(end_obj.get("timestamp")) or parse_ts(end_obj.get("time")) or parse_ts(end_obj.get("ts"))

        return start_ts, end_ts

    def extract_event_updated(e):
        upd = e.get("updated_ts")
        ts = parse_ts(upd) if upd is not None else None
        if ts is not None:
            return ts
        upd_obj = e.get("updated") or e.get("last_modified") or e.get("modified")
        if isinstance(upd_obj, dict):
            return parse_ts(upd_obj.get("ts") or upd_obj.get("timestamp") or upd_obj.get("time"))
        return parse_ts(e.get("last_modified_ts") or e.get("modified_ts"))

    def extract_event_original_time(e):
        ot = e.get("original_time")
        ts = parse_ts(ot) if ot is not None else None
        if ts is not None:
            return ts
        # Some events may encode original occurrence in id like "{uid}_{original_time}"
        eid = e.get("event_id") or e.get("id")
        if isinstance(eid, str) and "_" in eid:
            parts = eid.split("_")
            maybe_ts = parts[-1]
            maybe_ts_parsed = parse_ts(maybe_ts)
            return maybe_ts_parsed
        return None

    # Find selected context and calendar
    selected_context = None
    selected_calendar_id = calendar_id

    def find_primary_calendar(ctx):
        calendars = list_entities_by_path(ctx, "calendars", {}, 10000)
        if not isinstance(calendars, list):
            return None
        # primary markers: type == "primary" or is_primary True
        for c in calendars:
            if c.get("type") == "primary" or c.get("is_primary") is True or c.get("primary") is True:
                return c
        return None

    if isinstance(context_data, list):
        # Multiple contexts
        if calendar_id == "primary":
            for ctx in context_data:
                primary_cal = find_primary_calendar(ctx)
                if primary_cal:
                    selected_context = ctx
                    selected_calendar_id = primary_cal.get("calendar_id") or primary_cal.get("id")
                    break
            if not selected_context or not selected_calendar_id:
                return {"success": False, "error": "No primary calendar found across contexts", "result": None}
        else:
            # Find calendar_id across contexts
            found = False
            for ctx in context_data:
                cal = get_entity_by_path(ctx, "calendars", calendar_id)
                if cal:
                    selected_context = ctx
                    selected_calendar_id = calendar_id
                    found = True
                    break
            if not found:
                return {"success": False, "error": f"Invalid calendar_id: {calendar_id}", "result": None}
    elif isinstance(context_data, dict):
        # Single context
        if calendar_id == "primary":
            primary_cal = find_primary_calendar(context_data)
            if not primary_cal:
                return {"success": False, "error": "No primary calendar found", "result": None}
            selected_context = context_data
            selected_calendar_id = primary_cal.get("calendar_id") or primary_cal.get("id")
        else:
            cal = get_entity_by_path(context_data, "calendars", calendar_id)
            if not cal:
                return {"success": False, "error": f"Invalid calendar_id: {calendar_id}", "result": None}
            selected_context = context_data
            selected_calendar_id = calendar_id
    else:
        return {"success": False, "error": "Invalid context data format", "result": None}

    # 4. Perform operation (list) using path-based functions
    events_path = f"calendars[{selected_calendar_id}].events"
    all_events = list_entities_by_path(selected_context, events_path, {}, 10000)
    if not isinstance(all_events, list):
        all_events = []

    # Apply filters
    anchor_ts = parse_ts(anchor_time)
    start_ts = parse_ts(start_time)
    end_ts_val = parse_ts(end_time)
    sync_ts = parse_ts(sync_token)

    def event_passes_filters(e):
        st, et = extract_event_times(e)
        ot = extract_event_original_time(e)
        upd = extract_event_updated(e)

        # If start/end range is provided, include events that intersect the range
        if start_ts is not None and end_ts_val is not None:
            # Intersect if start <= end_time and end >= start_time
            if st is None and et is None:
                return False
            if st is not None and st > end_ts_val:
                return False
            if et is not None and et < start_ts:
                return False
            # Otherwise intersects
            return True

        # If anchor_time is provided
        if anchor_ts is not None:
            # For single events: end_time >= anchor_time
            if et is not None and et >= anchor_ts:
                return True
            # For exceptional events: original_time >= anchor_time OR end_time >= anchor_time
            if ot is not None and ot >= anchor_ts:
                return True
            return False

        # If sync_token is provided (incremental): updated_ts > sync_token
        if sync_ts is not None:
            if upd is not None and upd > sync_ts:
                return True
            return False

        # No filters provided: include all
        return True

    filtered_events = [e for e in all_events if event_passes_filters(e)]

    # Sort events by start_time ascending
    def sort_key(e):
        st, et = extract_event_times(e)
        primary = st if st is not None else (et if et is not None else 0)
        return primary

    filtered_events.sort(key=sort_key)

    # Pagination
    # page_token as offset (stringified integer)
    offset = 0
    if page_token:
        try:
            offset = int(page_token)
            if offset < 0:
                offset = 0
        except Exception:
            # Support format "offset:<int>"
            if isinstance(page_token, str) and page_token.startswith("offset:"):
                try:
                    offset = int(page_token.split("offset:")[1])
                except Exception:
                    offset = 0
            else:
                offset = 0

    items_slice = filtered_events[offset: offset + page_size]
    has_more = (offset + page_size) < len(filtered_events)
    next_page_token = str(offset + page_size) if has_more else ""

    # Determine sync_token to return
    new_sync_token = ""
    if not has_more and not (start_ts is not None and end_ts_val is not None):
        # Provide sync_token at the end of paging queries (when page_token return value is empty)
        # Use max updated_ts across filtered_events (or all events if none have updated_ts)
        candidates = []
        for e in filtered_events:
            upd = extract_event_updated(e)
            if upd is not None:
                candidates.append(upd)
        if not candidates:
            # Fall back to max end_time or start_time
            for e in filtered_events:
                st, et = extract_event_times(e)
                if st is not None:
                    candidates.append(st)
                if et is not None:
                    candidates.append(et)
        if candidates:
            new_sync_token = str(max(candidates))
        else:
            new_sync_token = ""

    # 5. Save context if modified (not applicable for list operation)
    # No modifications performed; skip saving

    # 6. Format and return response
    tool_result = {
        "items": items_slice,
        "has_more": has_more,
        "page_token": next_page_token,
        "sync_token": new_sync_token
    }

    wrapped_result = {
        "meta": None,
        "content": [
            {
                "type": "text",
                "text": json.dumps(tool_result,ensure_ascii=False),
            }
        ],
        "isError": False
    }

    return {"success": True, "error": None, "result": wrapped_result}