from pathlib import Path
import json
import os
import re
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
    required_fields = {"channel", "name", "timestamp"}
    missing = [f for f in required_fields if f not in parameters_used]
    if missing:
        return {
            "success": False,
            "error": f"Missing required fields: {', '.join(missing)}",
            "result": None,
        }
    # AdditionalProperties false: reject unknown fields
    allowed_fields = required_fields
    extra_fields = [k for k in parameters_used.keys() if k not in allowed_fields]
    if extra_fields:
        return {
            "success": False,
            "error": f"Unexpected fields: {', '.join(extra_fields)}",
            "result": None,
        }
    channel = parameters_used.get("channel")
    name = parameters_used.get("name")
    timestamp = parameters_used.get("timestamp")
    if not isinstance(channel, str) or not channel.strip():
        return {
            "success": False,
            "error": "Field 'channel' must be a non-empty string",
            "result": None,
        }
    if not isinstance(name, str) or not name.strip():
        return {
            "success": False,
            "error": "Field 'name' must be a non-empty string",
            "result": None,
        }
    if not isinstance(timestamp, str) or not timestamp.strip():
        return {
            "success": False,
            "error": "Field 'timestamp' must be a non-empty string",
            "result": None,
        }
    # Validate emoji name format: base name + optional ::skin-tone-[2-6]
    # Base name: lower-case letters, digits, underscores, hyphens (Slack-like)
    emoji_pattern = r"^(?P<base>[a-z0-9_]+)(::skin-tone-(?P<tone>[2-6]))?$"
    m = re.match(emoji_pattern, name.strip())
    if not m:
        return {
            "success": False,
            "error": "Invalid emoji 'name' format. Use e.g., 'thumbsup' or 'wave::skin-tone-3'",
            "result": None,
        }

    # 2. Load all context data and select context(s) based on context_id from environment variable
    context_file_path = Path(json.loads(os.environ.get("SLACK_SANDBOX_PATHS"))[0])
    all_context_data = load_context(context_file_path)
    tool_name = "slack:add_reaction_to_an_item"
    tool_name_lower = tool_name.lower() if tool_name else ""
    is_query_tool = any(
        keyword in tool_name_lower
        for keyword in ["get", "list", "query", "search", "batch"]
    )
    context_id = os.environ.get("SLACK_CONTEXT_ID")
    if context_id is None:
        if is_query_tool:
            context_id = "all"
        else:
            if isinstance(all_context_data, dict) and len(all_context_data) > 0:
                context_id = list(all_context_data.keys())[0]
            else:
                context_id = None
    if context_id == "all":
        if isinstance(all_context_data, dict):
            context_data = list(all_context_data.values())
        elif isinstance(all_context_data, list):
            context_data = all_context_data
        else:
            context_data = [all_context_data]
        # Modify tools should target a single context, not 'all'
        return {
            "success": False,
            "error": "Modify operation requires a specific SLACK_CONTEXT_ID, not 'all'. Set SLACK_CONTEXT_ID to a specific slack context id.",
            "result": None,
        }
    elif isinstance(all_context_data, dict):
        if context_id in all_context_data:
            context_data = all_context_data[context_id]
        else:
            return {
                "success": False,
                "error": f"Context ID '{context_id}' not found",
                "result": None,
            }
    elif isinstance(all_context_data, list):
        if context_id is None:
            context_data = all_context_data[0] if len(all_context_data) > 0 else {}
        else:
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
        context_data = all_context_data

    # 3. Validate entity references exist in context (CRITICAL!)
    # Validate channel exists under path "slack.channels"
    channel_entity = get_entity_by_path(context_data, "group_channels", channel)
    if not channel_entity:
        return {
            "success": False,
            "error": f"Invalid channel: {channel}",
            "result": None,
        }
    # Validate message exists under path "slack.channels[{channel}].messages"
    message_entity = get_entity_by_path(
        context_data, f"group_channels[{channel}].messages", timestamp
    )
    if not message_entity:
        return {
            "success": False,
            "error": f"Message not found at timestamp: {timestamp}",
            "result": None,
        }

    # 4. Perform operation (add reaction)
    reactions = message_entity.get("reactions")
    updated_reactions = None

    if reactions is None:
        # Initialize as dict with count
        updated_reactions = {name: 1}
    elif isinstance(reactions, dict):
        # Count-based dict
        current_count = reactions.get(name, 0)
        reactions[name] = current_count + 1
        updated_reactions = reactions
    elif isinstance(reactions, list):
        # Could be a list of dicts with {"name": ..., "count": ...} or list of strings
        if len(reactions) > 0 and isinstance(reactions[0], dict):
            found = False
            for r in reactions:
                if r.get("name") == name:
                    r["count"] = int(r.get("count", 0)) + 1
                    found = True
                    break
            if not found:
                reactions.append({"name": name, "count": 1})
            updated_reactions = reactions
        else:
            # List of strings; append name
            reactions.append(name)
            updated_reactions = reactions
    else:
        # Unknown format; reset to dict
        updated_reactions = {name: 1}

    # Apply the update using update_entity_by_path
    updates = {"reactions": updated_reactions}
    update_success = update_entity_by_path(
        context_data, f"group_channels[{channel}].messages", timestamp, updates
    )
    if not update_success:
        return {
            "success": False,
            "error": "Failed to update reactions for the message",
            "result": None,
        }

    # 5. Save context if modified (CRITICAL)
    # Get context_id from environment variable (same as when loading)
    context_id_save = os.environ.get("SLACK_CONTEXT_ID")
    if context_id_save is None:
        if isinstance(all_context_data, dict) and len(all_context_data) > 0:
            context_id_save = list(all_context_data.keys())[0]

    if isinstance(all_context_data, dict):
        if (
            context_id_save
            and context_id_save != "all"
            and context_id_save in all_context_data
        ):
            all_context_data[context_id_save] = context_data
    elif isinstance(all_context_data, list):
        if isinstance(context_data, dict):
            user_id = context_data.get("user_id")
            if user_id:
                for i, ctx in enumerate(all_context_data):
                    if isinstance(ctx, dict) and ctx.get("user_id") == user_id:
                        all_context_data[i] = context_data
                        break
    else:
        all_context_data = context_data

    save_context(context_file_path, all_context_data)

    # 6. Format and return response
    # Normalize reactions for output
    def normalize_reactions_output(r):
        if r is None:
            return []
        if isinstance(r, dict):
            return [{"name": k, "count": int(v)} for k, v in r.items()]
        if isinstance(r, list):
            if len(r) > 0 and isinstance(r[0], dict):
                return [
                    {"name": d.get("name"), "count": int(d.get("count", 1))}
                    for d in r
                    if isinstance(d, dict)
                ]
            else:
                # list of strings (no counts)
                # convert to counts
                counts = {}
                for item in r:
                    counts[item] = counts.get(item, 0) + 1
                return [{"name": k, "count": v} for k, v in counts.items()]
        return []

    result_payload = {
        "ok": True,
        "channel": channel,
        "timestamp": timestamp,
        "reaction": name,
        "current_reactions": normalize_reactions_output(updated_reactions),
    }

    return {
        "success": True,
        "error": None,
        "result": {
            "meta": None,
            "content": [
                {
                    "type": "text",
                    "text": json.dumps(result_payload, ensure_ascii=False),
                }
            ],
            "isError": False,
        },
    }
