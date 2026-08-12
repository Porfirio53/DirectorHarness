from pathlib import Path
import json
import os
from typing import Optional
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
    tool_name = "slack:find_channels"
    description = "Find channels in a Slack workspace by any criteria - name, topic, purpose, or description."

    if not isinstance(parameters_used, dict):
        return {
            "success": False,
            "error": "Invalid input: parameters_used must be a dictionary",
            "result": None,
        }

    allowed_keys = {
        "exact_match",
        "exclude_archived",
        "limit",
        "member_only",
        "search_query",
        "types",
    }
    extra_keys = set(parameters_used.keys()) - allowed_keys
    if extra_keys:
        return {
            "success": False,
            "error": f"Invalid input: unexpected parameter(s): {', '.join(sorted(extra_keys))}",
            "result": None,
        }

    # Set defaults
    exact_match = parameters_used.get("exact_match", False)
    exclude_archived = parameters_used.get("exclude_archived", True)
    limit = parameters_used.get("limit", 50)
    member_only = parameters_used.get("member_only", False)
    search_query = parameters_used.get("search_query")
    types = parameters_used.get("types", "public_channel,private_channel")

    # Validate required fields
    if (
        search_query is None
        or not isinstance(search_query, str)
        or search_query.strip() == ""
    ):
        return {
            "success": False,
            "error": "Invalid input: 'search_query' is required and must be a non-empty string",
            "result": None,
        }

    # Validate types
    if not isinstance(exact_match, bool):
        return {
            "success": False,
            "error": "Invalid input: 'exact_match' must be a boolean",
            "result": None,
        }
    if not isinstance(exclude_archived, bool):
        return {
            "success": False,
            "error": "Invalid input: 'exclude_archived' must be a boolean",
            "result": None,
        }
    if not isinstance(member_only, bool):
        return {
            "success": False,
            "error": "Invalid input: 'member_only' must be a boolean",
            "result": None,
        }
    if not isinstance(limit, int):
        return {
            "success": False,
            "error": "Invalid input: 'limit' must be an integer",
            "result": None,
        }
    if limit < 1 or limit > 200:
        return {
            "success": False,
            "error": "Invalid input: 'limit' must be between 1 and 200",
            "result": None,
        }
    if not isinstance(types, str):
        return {
            "success": False,
            "error": "Invalid input: 'types' must be a comma-separated string",
            "result": None,
        }

    allowed_types = {"public_channel", "private_channel", "mpim", "im"}
    requested_types = [t.strip() for t in types.split(",") if t.strip()]
    if not requested_types:
        return {
            "success": False,
            "error": "Invalid input: 'types' must include at least one type",
            "result": None,
        }
    invalid_types = [t for t in requested_types if t not in allowed_types]
    if invalid_types:
        return {
            "success": False,
            "error": f"Invalid input: unknown channel type(s): {', '.join(invalid_types)}",
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
    tool_name_lower = tool_name.lower() if tool_name else ""
    is_query_tool = any(
        keyword in tool_name_lower
        for keyword in ["get", "list", "query", "search", "batch", "find"]
    ) or ("find" in description.lower())

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

    # Helper functions for matching and membership
    def _extract_text_field(value):
        # Slack topic/purpose can be dicts with 'value'
        if isinstance(value, dict):
            return str(value.get("value", "") or "")
        return str(value or "")

    def _infer_channel_type(channel: dict) -> Optional[str]:
        """
        Infer Slack channel type for our simulated context.
        Some contexts store group channels without a 'type' field.

        Rules:
        - If explicit 'type' exists, use it.
        - If is_im/is_mpim flags exist, use them.
        - Else infer from channel id prefix:
          - D... => im
          - C... => public_channel
          - G... => private_channel (or mpim; mpim should be flagged by is_mpim)
        """
        if not isinstance(channel, dict):
            return None

        explicit = channel.get("type")
        if isinstance(explicit, str) and explicit.strip():
            return explicit.strip()

        if channel.get("is_im") is True:
            return "im"
        if channel.get("is_mpim") is True:
            return "mpim"

        ch_id = channel.get("id") or channel.get("channel_id")
        if isinstance(ch_id, str) and ch_id:
            if ch_id.startswith("D"):
                return "im"
            if ch_id.startswith("C"):
                return "public_channel"
            if ch_id.startswith("G"):
                return "private_channel"

        return None

    def _matches_query(channel, q, exact):
        fields = [
            _extract_text_field(channel.get("name")),
            _extract_text_field(channel.get("channel_name")),
            _extract_text_field(channel.get("topic")),
            _extract_text_field(channel.get("purpose")),
            _extract_text_field(channel.get("description")),
        ]
        q_lower = q.lower()
        if exact:
            return any(f.lower() == q_lower for f in fields)
        else:
            return any(q_lower in f.lower() for f in fields)

    def _is_member(channel, user_id):
        # Direct membership flags
        if isinstance(channel.get("is_member"), bool):
            if channel.get("is_member") is True:
                return True
            # If explicitly False, keep checking other indicators before returning False
        members = channel.get("members") or channel.get("member_ids")
        if isinstance(members, list) and user_id is not None:
            if user_id in members:
                return True
        # IM channels may have 'user' representing the counterpart; membership implies the current user participates
        if channel.get("type") == "im":
            if user_id is not None:
                # In IM, usually 'members' includes both users; fallback to 'user' if present
                im_user = channel.get("user")
                if im_user == user_id:
                    return True
                if isinstance(members, list) and user_id in members:
                    return True
        # MPIM uses members list
        if (
            channel.get("type") == "mpim"
            and isinstance(members, list)
            and user_id is not None
        ):
            return user_id in members
        return False

    def _get_user_id_from_context(ctx):
        # Try common locations
        uid = ctx.get("user_id")
        if uid is None and isinstance(ctx.get("slack"), dict):
            uid = ctx["slack"].get("user_id") or ctx["slack"].get("self_id")
        return uid

    def _collect_channels_for_context(ctx):
        # Retrieve all slack channels for a given context
        try:
            channels = list_entities_by_path(ctx, "group_channels", {}, 10000)
            if channels is None:
                channels = []
        except Exception:
            channels = []
        # Apply filters
        uid = _get_user_id_from_context(ctx)
        result = []
        for ch in channels:
            # Type filter
            ch_type = _infer_channel_type(ch)
            if ch_type not in requested_types:
                continue
            # Archived filter
            if exclude_archived:
                # Slack channels may have is_archived or archived properties
                if ch.get("is_archived") is True or ch.get("archived") is True:
                    continue
            # Member-only filter
            if member_only:
                if not _is_member(ch, uid):
                    continue
            # Search query
            if not _matches_query(ch, search_query, exact_match):
                continue
            result.append(ch)
        return result

    # 3. Validate entity references exist in context (CRITICAL!)
    # For this tool, there are no parent entities to validate beyond ensuring the channels path exists.
    # We will not error if Slack channels are missing; we'll return an empty list.

    # 4. Perform operation (list) using path-based functions
    if isinstance(context_data, list):
        aggregated = []
        for ctx in context_data:
            if isinstance(ctx, dict):
                aggregated.extend(_collect_channels_for_context(ctx))
        # Deduplicate by channel id if present
        seen_ids = set()
        deduped = []
        for ch in aggregated:
            ch_id = ch.get("id") or ch.get("channel_id")
            if ch_id is None:
                # If no id, include as-is
                deduped.append(ch)
            else:
                if ch_id not in seen_ids:
                    deduped.append(ch)
                    seen_ids.add(ch_id)
        all_filtered = deduped
    elif isinstance(context_data, dict):
        all_filtered = _collect_channels_for_context(context_data)
    else:
        all_filtered = []

    # Sorting could be applied if needed; leave original order.
    total_count = len(all_filtered)
    limited = all_filtered[:limit]
    has_more = total_count > limit

    result_payload = {
        "channels": limited,
        "total": total_count,
        "count": len(limited),
        "has_more": has_more,
        "applied_filters": {
            "exact_match": exact_match,
            "exclude_archived": exclude_archived,
            "member_only": member_only,
            "types": requested_types,
            "limit": limit,
            "search_query": search_query,
        },
    }

    # 5. Save context if modified (not applicable for query tools; no modification performed)

    # 6. Format and return response
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
