from pathlib import Path
import json
import os
import time
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
        return {"success": False, "error": "Input must be a dictionary", "result": None}

    allowed_keys = {"channel", "return_im", "users"}
    unexpected_keys = set(parameters_used.keys()) - allowed_keys
    if unexpected_keys:
        return {
            "success": False,
            "error": f"Unexpected parameters: {', '.join(sorted(unexpected_keys))}",
            "result": None,
        }

    channel_param = parameters_used.get("channel", None)
    users_param = parameters_used.get("users", None)
    return_im = parameters_used.get("return_im", False)

    if channel_param is not None and not isinstance(channel_param, str):
        return {
            "success": False,
            "error": "Parameter 'channel' must be a string if provided",
            "result": None,
        }
    if users_param is not None and not isinstance(users_param, str):
        return {
            "success": False,
            "error": "Parameter 'users' must be a string if provided",
            "result": None,
        }
    if return_im is not None and not isinstance(return_im, bool):
        return {
            "success": False,
            "error": "Parameter 'return_im' must be a boolean if provided",
            "result": None,
        }

    if (channel_param is None and users_param is None) or (
        channel_param is not None and users_param is not None
    ):
        return {
            "success": False,
            "error": "Provide either 'channel' or 'users', but not both",
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
    tool_name = "slack:open_dm"
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
            # Context ID not found, return error
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

    # If context_id == "all" but this is a modify tool, we cannot proceed
    if context_id == "all" and not is_query_tool:
        return {
            "success": False,
            "error": "SLACK_CONTEXT_ID=all is not supported for modifying tools. Provide a specific SLACK_CONTEXT_ID for slack.",
            "result": None,
        }

    # Helper: ensure slack structures exist
    if not isinstance(context_data, dict):
        return {
            "success": False,
            "error": "Selected context is not a valid dictionary structure",
            "result": None,
        }

    # Use my_user_id as self_user_id (current user)
    self_user_id = context_data.get("my_user_id")
    if not self_user_id or not isinstance(self_user_id, str):
        return {
            "success": False,
            "error": "my_user_id not found in context",
            "result": None,
        }

    # Ensure channels container exists (may be dict keyed by channel_id)
    slack_channels_path = "group_channels"
    existing_channels = (
        list_entities_by_path(context_data, slack_channels_path, {}, 10000) or []
    )
    if not isinstance(existing_channels, list):
        existing_channels = []

    def find_channel_by_id(channel_id):
        if not channel_id:
            return None
        return get_entity_by_path(context_data, slack_channels_path, channel_id)

    def find_channel_by_name(name):
        if not name:
            return None
        chans = (
            list_entities_by_path(context_data, slack_channels_path, {}, 10000) or []
        )
        for ch in chans:
            if ch and ch.get("name") == name:
                return ch
        return None

    def generate_new_id(prefix):
        # Ensure uniqueness against existing channels
        base = int(time.time() * 1000)
        candidate = f"{prefix}{base}"
        existing_ids = {
            ch.get("id") for ch in (existing_channels or []) if isinstance(ch, dict)
        }
        # If collision, increment
        while candidate in existing_ids:
            base += 1
            candidate = f"{prefix}{base}"
        return candidate

    def match_dm_channel(user_id):
        # DM channels type "im" with participants containing exactly self and user_id (order doesn't matter)
        chans = (
            list_entities_by_path(context_data, slack_channels_path, {}, 10000) or []
        )
        for ch in chans:
            if not isinstance(ch, dict):
                continue
            if ch.get("type") == "im":
                parts = ch.get("participants") or []
                if set(parts) == set([self_user_id, user_id]):
                    return ch
        return None

    def match_mpim_channel(user_ids_ordered):
        # MPIM channels type "mpim" with participants == [self_user_id] + user_ids_ordered
        expected = [self_user_id] + user_ids_ordered
        chans = (
            list_entities_by_path(context_data, slack_channels_path, {}, 10000) or []
        )
        for ch in chans:
            if not isinstance(ch, dict):
                continue
            if ch.get("type") == "mpim":
                parts = ch.get("participants") or []
                if parts == expected:
                    return ch
        return None

    # 3. Validate entity references exist in context (CRITICAL!)
    # For 'channel' path: verify it exists and is of DM/MPIM type
    if channel_param is not None:
        # Try ID first
        ch = find_channel_by_id(channel_param)
        if ch is None:
            # Try by name
            ch = find_channel_by_name(channel_param)

        if ch is None:
            return {
                "success": False,
                "error": f"Invalid channel reference: '{channel_param}'",
                "result": None,
            }

        if ch.get("type") not in ("im", "mpim"):
            return {
                "success": False,
                "error": f"Channel '{channel_param}' is not a DM or MPIM",
                "result": None,
            }

        # 4. Perform operation: open/resume
        already_open = bool(ch.get("is_open"))
        no_op = already_open  # If already open, no operation was needed
        if not already_open:
            updates = {"is_open": True, "last_opened_ts": int(time.time())}
            update_entity_by_path(
                context_data, slack_channels_path, ch.get("id"), updates
            )
        result_channel = get_entity_by_path(
            context_data, slack_channels_path, ch.get("id")
        )
        api_result = {
            "ok": True,
            "channel": {
                "id": result_channel.get("id")
            },  # Only return id for channel parameter
            "no_op": no_op,
            "already_open": already_open,
            "response_metadata": {"warnings": ["missing_charset"]},
            "warning": "missing_charset",
        }

    else:
        # Using users_param to open DM/MPIM
        raw = users_param.strip()
        if not raw:
            return {
                "success": False,
                "error": "Parameter 'users' must not be empty",
                "result": None,
            }
        user_ids = [u.strip() for u in raw.split(",") if u.strip()]

        # Validate counts
        if len(user_ids) == 1:
            # DM
            pass
        elif 2 <= len(user_ids) <= 8:
            # MPIM
            pass
        else:
            return {
                "success": False,
                "error": "Parameter 'users' must contain 1 for DM or 2-8 for MPIM user IDs",
                "result": None,
            }

        # Validate existence of each user and they are not self
        distinct_user_ids = []
        seen = set()
        for uid in user_ids:
            if uid == self_user_id:
                return {
                    "success": False,
                    "error": "Do not include self user in 'users' list",
                    "result": None,
                }
            if uid in seen:
                # For MPIM, we require distinct user IDs
                continue
            seen.add(uid)
            distinct_user_ids.append(uid)

        # Verify each user exists (basic format check)
        for uid in distinct_user_ids:
            # Basic validation: user IDs typically start with 'U' in Slack
            if not uid or not isinstance(uid, str) or not uid.startswith("U"):
                return {
                    "success": False,
                    "error": f"Invalid user_id format in 'users': {uid}",
                    "result": None,
                }

        # Now search existing DM/MPIM
        now_ts = int(time.time())
        if len(distinct_user_ids) == 1:
            target_uid = distinct_user_ids[0]
            existing = match_dm_channel(target_uid)
            if existing:
                already_open = bool(existing.get("is_open"))
                update_entity_by_path(
                    context_data,
                    slack_channels_path,
                    existing.get("id"),
                    {"is_open": True, "last_opened_ts": now_ts},
                )
                result_channel = get_entity_by_path(
                    context_data, slack_channels_path, existing.get("id")
                )
            else:
                # Create new DM channel
                new_id = generate_new_id("D")
                channel_data = {
                    "id": new_id,
                    "type": "im",
                    "is_im": True,
                    "is_mpim": False,
                    "is_open": True,
                    "created": now_ts,
                    "last_opened_ts": now_ts,
                    "user": target_uid,
                    "participants": [self_user_id, target_uid],
                    "name": None,
                }
                create_entity_by_path(
                    context_data, slack_channels_path, channel_data, new_id
                )
                result_channel = get_entity_by_path(
                    context_data, slack_channels_path, new_id
                )
                already_open = False

            # Build API result
            no_op = already_open  # If already open, no operation was needed
            if return_im:
                api_result = {
                    "ok": True,
                    "channel": result_channel,  # Full channel object when return_im=True
                    "no_op": no_op,
                    "already_open": already_open,
                    "response_metadata": {"warnings": ["missing_charset"]},
                    "warning": "missing_charset",
                }
            else:
                api_result = {
                    "ok": True,
                    "channel": {"id": result_channel.get("id")},
                    "no_op": no_op,
                    "already_open": already_open,
                    "response_metadata": {"warnings": ["missing_charset"]},
                    "warning": "missing_charset",
                }

        else:
            # MPIM
            ordered_ids = distinct_user_ids  # preserve order
            existing = match_mpim_channel(ordered_ids)
            if existing:
                already_open = bool(existing.get("is_open"))
                update_entity_by_path(
                    context_data,
                    slack_channels_path,
                    existing.get("id"),
                    {"is_open": True, "last_opened_ts": now_ts},
                )
                result_channel = get_entity_by_path(
                    context_data, slack_channels_path, existing.get("id")
                )
            else:
                # Create new MPIM channel
                new_id = generate_new_id("G")
                # Generate a simple name using user IDs
                simple_name = "mpim-" + "-".join(ordered_ids)
                channel_data = {
                    "id": new_id,
                    "type": "mpim",
                    "is_im": False,
                    "is_mpim": True,
                    "is_open": True,
                    "created": now_ts,
                    "last_opened_ts": now_ts,
                    "name": simple_name,
                    "participants": [self_user_id] + ordered_ids,
                }
                create_entity_by_path(
                    context_data, slack_channels_path, channel_data, new_id
                )
                result_channel = get_entity_by_path(
                    context_data, slack_channels_path, new_id
                )
                already_open = False

            no_op = already_open  # If already open, no operation was needed
            api_result = {
                "ok": True,
                "channel": result_channel,  # Full channel object for MPIM
                "no_op": no_op,
                "already_open": already_open,
                "response_metadata": {"warnings": ["missing_charset"]},
                "warning": "missing_charset",
            }

    # 5. Save context if modified
    # Get context_id from environment variable (same as when loading)
    context_id_save = os.environ.get("SLACK_CONTEXT_ID")
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
            user_id_field = context_data.get("user_id")
            if user_id_field:
                for i, ctx in enumerate(all_context_data):
                    if isinstance(ctx, dict) and ctx.get("user_id") == user_id_field:
                        all_context_data[i] = context_data
                        break
    else:
        # Single context dict
        all_context_data = context_data

    save_context(context_file_path, all_context_data)

    # 6. Format and return response
    formatted = {
        "meta": None,
        "content": [{"type": "text", "text": json.dumps(api_result)}],
        "isError": False,
    }
    return {"success": True, "error": None, "result": formatted}
