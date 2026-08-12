from pathlib import Path
import json
import os
import string
import random
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
    if not isinstance(parameters_used, dict):
        return {
            "success": False,
            "error": "Invalid input: parameters_used must be a dictionary",
            "result": None,
        }
    allowed_keys = {"text", "time", "user"}
    extra_keys = [k for k in parameters_used.keys() if k not in allowed_keys]
    if extra_keys:
        return {
            "success": False,
            "error": f"Unsupported parameter(s): {', '.join(extra_keys)}",
            "result": None,
        }

    text = parameters_used.get("text")
    time_str = parameters_used.get("time")
    user_id_param = parameters_used.get("user")

    if text is None or not isinstance(text, str) or text.strip() == "":
        return {
            "success": False,
            "error": "Field 'text' is required and must be a non-empty string",
            "result": None,
        }

    # Accept both string and integer for time parameter
    if time_str is None:
        return {"success": False, "error": "Field 'time' is required", "result": None}
    if isinstance(time_str, int):
        time_value = time_str
    elif isinstance(time_str, str) and time_str.strip():
        try:
            time_value = int(time_str.strip())
        except ValueError:
            return {
                "success": False,
                "error": "Field 'time' must be a valid timestamp (integer or string)",
                "result": None,
            }
    else:
        return {
            "success": False,
            "error": "Field 'time' is required and must be a non-empty value",
            "result": None,
        }

    # 2. Load all context data and select context(s) based on context_id from environment variable
    from time import time as _time
    from uuid import uuid4

    tool_name = "slack:create_a_reminder"
    context_file_path = Path(json.loads(os.environ.get("SLACK_SANDBOX_PATHS"))[0])
    all_context_data = load_context(
        context_file_path
    )  # Returns Dict[{context_id}: {context_dict}] or List[Dict]

    # Read context_id from environment variable (NOT from input parameters)
    context_id = os.environ.get("SLACK_CONTEXT_ID")

    # Determine tool type from tool_name or description to set appropriate default
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
        # For modify tools, 'all' is not supported
        if not is_query_tool:
            return {
                "success": False,
                "error": "SLACK_CONTEXT_ID 'all' is not allowed for modify tools. Set a specific SLACK_CONTEXT_ID for slack.",
                "result": None,
            }
    elif isinstance(all_context_data, dict):
        # Context is dict format: {context_id: context_dict, ...}
        if context_id in all_context_data:
            context_data = all_context_data[context_id]  # Get single context dict
        else:
            # Context ID not found, return error
            return {
                "success": False,
                "error": f"Context ID '{{context_id}}' not found",
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
                    "error": f"Context ID '{{context_id}}' not found",
                    "result": None,
                }
    else:
        # Single context dict
        context_data = all_context_data

    # 3. Validate entity references exist in context (CRITICAL!)
    # Determine target user_id for the reminder
    target_user_id = (
        user_id_param
        if isinstance(user_id_param, str) and user_id_param.strip()
        else None
    )

    # If not specified, use context's my_user_id
    if not target_user_id:
        if isinstance(context_data, dict):
            target_user_id = context_data.get("my_user_id")
        if not target_user_id:
            return {
                "success": False,
                "error": "No user specified and no context user available",
                "result": None,
            }

    # Validate the user exists - check my_user_id field
    user_found = False
    if isinstance(context_data, dict):
        user_found = context_data.get("my_user_id") == target_user_id

    if not user_found:
        return {
            "success": False,
            "error": f"Invalid user: {target_user_id}",
            "result": None,
        }
    else:
        # If no users list available, ensure target_user_id matches context user_id (if present)
        ctx_user_id = context_data.get("user_id")
        if ctx_user_id and ctx_user_id != target_user_id:
            return {
                "success": False,
                "error": f"Invalid user: {target_user_id}",
                "result": None,
            }
        # Otherwise proceed, assuming single-user context

    # 4. Perform operation (create) using path-based functions
    # Parse time input
    def _parse_time(tstr: str):
        now_ts = int(_time())
        lower = tstr.strip().lower()
        result = {
            "raw": tstr,
            "type": "one_time",
            "scheduled_ts": None,
            "recurrence_rule": None,
        }
        if lower.isdigit():
            val = int(lower)
            if val <= 86400:
                result["type"] = "relative"
                result["scheduled_ts"] = now_ts + val
            else:
                result["type"] = "absolute"
                result["scheduled_ts"] = val
            return result
        if lower.startswith("in "):
            # very simple parser for "in N unit"
            parts = lower.split()
            if len(parts) >= 3 and parts[1].isdigit():
                num = int(parts[1])
                unit = parts[2]
                multiplier = 1
                if unit.startswith("sec"):
                    multiplier = 1
                elif unit.startswith("min"):
                    multiplier = 60
                elif unit.startswith("hour"):
                    multiplier = 3600
                elif unit.startswith("day"):
                    multiplier = 86400
                result["type"] = "relative"
                result["scheduled_ts"] = now_ts + num * multiplier
                return result
        if lower.startswith("every") or lower in ("daily", "weekly", "monthly"):
            result["type"] = "recurring"
            result["recurrence_rule"] = tstr
            return result
        # Natural language fallback without specific parsing
        result["type"] = "natural"
        result["recurrence_rule"] = tstr
        return result

    # Use time_value if it's an integer (direct timestamp), otherwise parse time_str
    if isinstance(time_str, int) or (
        isinstance(time_str, str) and time_str.strip().isdigit()
    ):
        # Direct timestamp (absolute Unix timestamp)
        ts_value = (
            time_value
            if isinstance(time_str, int) or isinstance(time_value, int)
            else int(time_str.strip())
        )
        parsed_time = {
            "raw": str(time_str),
            "type": "absolute",
            "scheduled_ts": ts_value,
            "recurrence_rule": None,
        }
    else:
        # Natural language or relative time, needs parsing
        parsed_time = _parse_time(time_str)

    # Get scheduled_ts from parsed_time for the time field
    scheduled_ts = parsed_time.get("scheduled_ts")
    if scheduled_ts is None:
        return {
            "success": False,
            "error": "Unable to determine scheduled time from time parameter",
            "result": None,
        }

    # Get creator (my_user_id) from context
    creator_user_id = None
    if isinstance(context_data, dict):
        creator_user_id = context_data.get("my_user_id")
    if not creator_user_id:
        return {
            "success": False,
            "error": "No creator user (my_user_id) found in context",
            "result": None,
        }

    # Generate reminder ID: "Rm" + 11 random alphanumeric characters
    random_chars = "".join(random.choices(string.ascii_uppercase + string.digits, k=11))
    reminder_id = f"Rm{random_chars}"

    reminder_entity = {
        "id": reminder_id,
        "text": text.strip(),
        "time": scheduled_ts,  # Simple integer timestamp
        "user": target_user_id,  # user field (not user_id)
        "creator": creator_user_id,  # Always my_user_id
        "complete_ts": 0,
        "recurring": False,
        "recurrence": None,
        "item": None,
    }

    # Create reminder under a Slack reminders collection
    created = create_entity_by_path(
        context_data, "reminders_create", reminder_entity, reminder_id
    )
    if not created:
        return {"success": False, "error": "Failed to create reminder", "result": None}

    # Also create the same reminder in target user's reminders_receive
    # Find target user's context if different from current context
    target_user_context = None
    target_user_context_id = None

    if target_user_id == creator_user_id:
        # Same user: use current context
        target_user_context = context_data
        target_user_context_id = context_id
    else:
        # Different user: find target user's context
        if isinstance(all_context_data, dict):
            # Search through all contexts to find the one with matching my_user_id
            for ctx_id, ctx in all_context_data.items():
                if isinstance(ctx, dict) and ctx.get("my_user_id") == target_user_id:
                    target_user_context = ctx
                    target_user_context_id = ctx_id
                    break
        elif isinstance(all_context_data, list):
            # Search through list of contexts
            for ctx in all_context_data:
                if isinstance(ctx, dict) and ctx.get("my_user_id") == target_user_id:
                    target_user_context = ctx
                    # For list format, we'll need to update by index later
                    break

    if target_user_context is None:
        return {
            "success": False,
            "error": f"Target user context not found for user: {target_user_id}",
            "result": None,
        }

    # Create the same reminder in target user's reminders_receive
    received = create_entity_by_path(
        target_user_context, "reminders_receive", reminder_entity, reminder_id
    )
    if not received:
        return {
            "success": False,
            "error": "Failed to create reminder in reminders_receive",
            "result": None,
        }

    # Update target user's context back to all_context_data if it's different from current context
    if target_user_id != creator_user_id and target_user_context_id:
        if isinstance(all_context_data, dict):
            if target_user_context_id in all_context_data:
                all_context_data[target_user_context_id] = target_user_context
        elif isinstance(all_context_data, list):
            # Update by finding the context with matching my_user_id
            for i, ctx in enumerate(all_context_data):
                if isinstance(ctx, dict) and ctx.get("my_user_id") == target_user_id:
                    all_context_data[i] = target_user_context
                    break

    # 5. Save context if modified (CRITICAL block)
    # Get context_id from environment (same as when loading)
    context_id = os.environ.get("SLACK_CONTEXT_ID")
    if context_id is None:
        # Use first key if dict, or first element if list
        if isinstance(all_context_data, dict) and len(all_context_data) > 0:
            context_id = list(all_context_data.keys())[0]

    # Update the modified context back to all_context_data
    if isinstance(all_context_data, dict):
        # Dict format: {context_id: context_dict, ...}
        if context_id and context_id != "all" and context_id in all_context_data:
            all_context_data[context_id] = context_data  # Update the specific context
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
    result_payload = {"reminder": created}
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
