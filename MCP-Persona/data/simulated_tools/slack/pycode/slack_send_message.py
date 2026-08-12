from pathlib import Path
import json
import os
import urllib.parse
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
        return {
            "success": False,
            "error": "Invalid input: parameters_used must be a dict",
            "result": None,
        }

    channel = parameters_used.get("channel")
    text = parameters_used.get("text")
    markdown_text = parameters_used.get("markdown_text")
    blocks = parameters_used.get("blocks")
    attachments = parameters_used.get("attachments")
    icon_emoji = parameters_used.get("icon_emoji")
    icon_url = parameters_used.get("icon_url")
    link_names = parameters_used.get("link_names")
    mrkdwn = parameters_used.get("mrkdwn")

    # Required: channel
    if channel is None or not isinstance(channel, str) or not channel.strip():
        return {
            "success": False,
            "error": "Invalid input: 'channel' is required and must be a non-empty string",
            "result": None,
        }

    # Required: one of text, markdown_text, blocks, attachments
    if (
        not (isinstance(text, str) and text.strip())
        and not (isinstance(markdown_text, str) and markdown_text.strip())
        and not (isinstance(blocks, str) and blocks.strip())
        and not (isinstance(attachments, str) and attachments.strip())
    ):
        return {
            "success": False,
            "error": "Invalid input: Provide at least one of 'text', 'markdown_text', 'blocks', or 'attachments'",
            "result": None,
        }

    # Optional fields type checks
    if icon_emoji is not None and not isinstance(icon_emoji, str):
        return {
            "success": False,
            "error": "Invalid input: 'icon_emoji' must be a string",
            "result": None,
        }
    if icon_url is not None and not isinstance(icon_url, str):
        return {
            "success": False,
            "error": "Invalid input: 'icon_url' must be a string",
            "result": None,
        }
    if link_names is not None and not isinstance(link_names, bool):
        return {
            "success": False,
            "error": "Invalid input: 'link_names' must be a boolean",
            "result": None,
        }
    if mrkdwn is not None and not isinstance(mrkdwn, bool):
        return {
            "success": False,
            "error": "Invalid input: 'mrkdwn' must be a boolean",
            "result": None,
        }

    # Decode and parse blocks and attachments
    parsed_blocks = None
    parsed_attachments = None
    if isinstance(blocks, str) and blocks.strip():
        try:
            decoded_blocks = urllib.parse.unquote(blocks)
            parsed_blocks = json.loads(decoded_blocks)
            if not isinstance(parsed_blocks, list):
                return {
                    "success": False,
                    "error": "Invalid input: 'blocks' must decode to a JSON array",
                    "result": None,
                }
        except Exception as e:
            return {
                "success": False,
                "error": f"Invalid input: 'blocks' must be URL-encoded JSON array. Error: {str(e)}",
                "result": None,
            }
    if isinstance(attachments, str) and attachments.strip():
        try:
            decoded_attachments = urllib.parse.unquote(attachments)
            parsed_attachments = json.loads(decoded_attachments)
            if not isinstance(parsed_attachments, list):
                return {
                    "success": False,
                    "error": "Invalid input: 'attachments' must decode to a JSON array",
                    "result": None,
                }
        except Exception as e:
            return {
                "success": False,
                "error": f"Invalid input: 'attachments' must be URL-encoded JSON array. Error: {str(e)}",
                "result": None,
            }

    # Determine message text content
    message_text = None
    if isinstance(markdown_text, str) and markdown_text.strip():
        message_text = markdown_text.strip()
    elif isinstance(text, str) and text.strip():
        message_text = text.strip()

    # 2. Load all context data and select context(s) based on context_id from environment variable
    context_file_path = Path(json.loads(os.environ.get("SLACK_SANDBOX_PATHS"))[0])
    all_context_data = load_context(
        context_file_path
    )  # Returns Dict[{context_id}: {context_dict}] or List[Dict]

    # Read context_id from environment variable (NOT from input parameters)
    context_id = os.environ.get("SLACK_CONTEXT_ID")

    # Determine tool type from tool_name or description to set appropriate default
    tool_name = "slack:send_message"
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

    # For modify tools, 'all' is not supported
    if not is_query_tool and isinstance(context_data, list):
        return {
            "success": False,
            "error": "Invalid SLACK_CONTEXT_ID 'all' for modify tools. Set SLACK_CONTEXT_ID to a specific slack context id.",
            "result": None,
        }

    # 3. Validate entity references exist in context (CRITICAL!)
    # Validate the channel exists in context - check both group_channels and dm_channels
    matched_channel = None
    channel_path = None  # Will be "group_channels" or "dm_channels"

    # First, try to find in group_channels
    channels = list_entities_by_path(context_data, "group_channels", {}, 10000)
    if isinstance(channels, list) and len(channels) > 0:
        for ch in channels:
            if not isinstance(ch, dict):
                continue
            ch_id = ch.get("id") or ch.get("channel_id")
            ch_name = ch.get("name")
            if channel == ch_id or channel == ch_name:
                matched_channel = ch
                channel_path = "group_channels"
                break

    # Try direct get by id in group_channels if not matched by list search
    if matched_channel is None:
        direct_channel = get_entity_by_path(context_data, "group_channels", channel)
        if direct_channel:
            matched_channel = direct_channel
            channel_path = "group_channels"

    # If not found in group_channels, try dm_channels
    if matched_channel is None:
        dm_channels = list_entities_by_path(context_data, "dm_channels", {}, 10000)
        if isinstance(dm_channels, list) and len(dm_channels) > 0:
            for ch in dm_channels:
                if not isinstance(ch, dict):
                    continue
                ch_id = ch.get("id") or ch.get("channel_id")
                ch_name = ch.get("name")
                if channel == ch_id or channel == ch_name:
                    matched_channel = ch
                    channel_path = "dm_channels"
                    break

    # Try direct get by id in dm_channels if not matched by list search
    if matched_channel is None:
        direct_channel = get_entity_by_path(context_data, "dm_channels", channel)
        if direct_channel:
            matched_channel = direct_channel
            channel_path = "dm_channels"

    if matched_channel is None:
        return {
            "success": False,
            "error": f"Invalid channel: '{channel}'. Channel not found in context (checked both group_channels and dm_channels).",
            "result": None,
        }

    channel_id = (
        matched_channel.get("id") or matched_channel.get("channel_id") or channel
    )
    channel_name = matched_channel.get("name")

    # Double-check channel existence via get_entity_by_path
    existing_channel = get_entity_by_path(context_data, channel_path, channel_id)
    if not existing_channel and channel_id != channel:
        return {
            "success": False,
            "error": f"Invalid channel_id: {channel_id}",
            "result": None,
        }

    # 4. Perform operation (create) using path-based functions
    # Get my_user_id and team_id from context
    my_user_id = (
        context_data.get("my_user_id") if isinstance(context_data, dict) else None
    )
    team_id = context_data.get("team_id") if isinstance(context_data, dict) else None

    if not my_user_id:
        return {
            "success": False,
            "error": "No my_user_id found in context",
            "result": None,
        }
    if not team_id:
        return {
            "success": False,
            "error": "No team_id found in context",
            "result": None,
        }

    # Generate ts in format: seconds.microseconds (e.g., "1742821542.057311")
    current_time = time.time()
    ts_seconds = int(current_time)
    ts_microseconds = int((current_time - ts_seconds) * 1000000)
    ts = f"{ts_seconds}.{ts_microseconds:06d}"

    # Build message entity matching context.json format
    message_entity = {
        "ts": ts,
        "type": "message",
        "subtype": "",
        "text": message_text,
        "client_msg_id": "",
        "app_id": "A08PJSKECPN",
        "user": my_user_id,
        "team": team_id,
        "channel": channel_id,
        "bot_id": "B0A0SKA2H08",
        "bot_profile.app_id": "A08PJSKECPN",
        "bot_profile.deleted": False,
        "bot_profile.icons.image_36": "https://avatars.slack-edge.com/2025-11-08/9870540592374_57c442a59ef4a5c11677_36.png",
        "bot_profile.icons.image_48": "https://avatars.slack-edge.com/2025-11-08/9870540592374_57c442a59ef4a5c11677_48.png",
        "bot_profile.icons.image_72": "https://avatars.slack-edge.com/2025-11-08/9870540592374_57c442a59ef4a5c11677_72.png",
        "bot_profile.id": "B0A0SKA2H08",
        "bot_profile.name": "Smithery",
        "bot_profile.team_id": team_id,
        "bot_profile.updated": 1764150707,
        "blocks.block_id": "bBuE",
        "blocks.elements.elements.text": message_text,
        "blocks.elements.elements.type": "text",
        "blocks.elements.type": "rich_text_section",
        "blocks.type": "rich_text",
    }

    # Use ts as the key for the message - use the appropriate channel path
    created_entity = create_entity_by_path(
        context_data, f"{channel_path}[{channel_id}].messages", message_entity, ts
    )
    if not created_entity:
        return {
            "success": False,
            "error": "Failed to create message entity in context",
            "result": None,
        }

    # 4.1 Best-effort: if this is a 1:1 DM ("im"), also sync the same message
    # into the other participant's context under the corresponding DM channel.
    # This must NOT break the original send behavior, so all errors are swallowed.
    try:
        is_im = False
        if isinstance(matched_channel, dict):
            is_im = (matched_channel.get("type") == "im") or (
                matched_channel.get("is_im") is True
            )

        if (
            is_im
            and isinstance(context_data, dict)
            and isinstance(all_context_data, (dict, list))
        ):
            # Identify the other participant
            other_user_id = None
            participants = (
                matched_channel.get("participants")
                if isinstance(matched_channel, dict)
                else None
            )
            if isinstance(participants, list) and my_user_id in participants:
                for p in participants:
                    if isinstance(p, str) and p and p != my_user_id:
                        other_user_id = p
                        break
            if not other_user_id:
                # Fallback: many Slack DM objects have a "user" field for the peer
                peer = (
                    matched_channel.get("user")
                    if isinstance(matched_channel, dict)
                    else None
                )
                if isinstance(peer, str) and peer and peer != my_user_id:
                    other_user_id = peer

            def _find_ctx_by_my_user_id(all_ctx, uid):
                if isinstance(all_ctx, dict):
                    for k, v in all_ctx.items():
                        if isinstance(v, dict) and v.get("my_user_id") == uid:
                            return ("dict", k, v)
                elif isinstance(all_ctx, list):
                    for i, v in enumerate(all_ctx):
                        if isinstance(v, dict) and v.get("my_user_id") == uid:
                            return ("list", i, v)
                return (None, None, None)

            if other_user_id:
                other_kind, other_key, other_ctx = _find_ctx_by_my_user_id(
                    all_context_data, other_user_id
                )
            else:
                other_kind, other_key, other_ctx = (None, None, None)

            if isinstance(other_ctx, dict):
                # Try to locate the DM channel in the other user's context.
                def _get_channel(ctx, path, cid):
                    try:
                        return get_entity_by_path(ctx, path, cid)
                    except Exception:
                        return None

                other_channel = _get_channel(other_ctx, "group_channels", channel_id)
                other_channel_path = "group_channels" if other_channel else None
                if other_channel is None:
                    other_channel = _get_channel(other_ctx, "dm_channels", channel_id)
                    other_channel_path = "dm_channels" if other_channel else None

                # If not found by id, try match by peer user id (sender) heuristics.
                if other_channel is None:

                    def _find_dm_by_peer(ctx, path, peer_user_id, self_id):
                        chans = list_entities_by_path(ctx, path, {}, 10000) or []
                        for ch in chans:
                            if not isinstance(ch, dict):
                                continue
                            if not (
                                (ch.get("type") == "im") or (ch.get("is_im") is True)
                            ):
                                continue
                            # Prefer participants match
                            parts = ch.get("participants") or []
                            if isinstance(parts, list):
                                parts_norm = [
                                    x for x in parts if isinstance(x, str) and x
                                ]
                                if set(parts_norm) == set([self_id, peer_user_id]):
                                    return ch
                            # Fallback: "user" field match
                            if ch.get("user") == peer_user_id:
                                return ch
                        return None

                    other_self_id = other_ctx.get("my_user_id")
                    if isinstance(other_self_id, str) and other_self_id:
                        ch1 = _find_dm_by_peer(
                            other_ctx, "group_channels", my_user_id, other_self_id
                        )
                        ch2 = (
                            None
                            if ch1
                            else _find_dm_by_peer(
                                other_ctx, "dm_channels", my_user_id, other_self_id
                            )
                        )
                        other_channel = ch1 or ch2
                        other_channel_path = (
                            "group_channels"
                            if ch1
                            else ("dm_channels" if ch2 else None)
                        )

                # If still not found, create a DM channel in the other user's context using the same channel_id.
                if other_channel is None:
                    other_self_id = other_ctx.get("my_user_id")
                    now_ts = int(time.time())
                    if isinstance(other_self_id, str) and other_self_id:
                        dm_channel_data = {
                            "id": channel_id,
                            "type": "im",
                            "is_im": True,
                            "is_mpim": False,
                            "is_open": True,
                            "created": now_ts,
                            "last_opened_ts": now_ts,
                            # In the recipient's context, the peer user is the sender (my_user_id)
                            "user": my_user_id,
                            "participants": [other_self_id, my_user_id],
                            "name": None,
                        }
                        other_channel_path = "group_channels"
                        try:
                            create_entity_by_path(
                                other_ctx,
                                other_channel_path,
                                dm_channel_data,
                                channel_id,
                            )
                        except Exception:
                            # If already exists or container differs, we just continue best-effort.
                            pass
                        other_channel = _get_channel(
                            other_ctx, other_channel_path, channel_id
                        )

                # Finally, append the same message under the other user's DM channel.
                if other_channel_path and isinstance(other_channel, dict):
                    other_channel_id = (
                        other_channel.get("id")
                        or other_channel.get("channel_id")
                        or channel_id
                    )
                    try:
                        existing_msg = get_entity_by_path(
                            other_ctx,
                            f"{other_channel_path}[{other_channel_id}].messages",
                            ts,
                        )
                    except Exception:
                        existing_msg = None
                    if existing_msg is None:
                        other_message = dict(message_entity)
                        # Keep channel consistent with the other context's channel id
                        other_message["channel"] = other_channel_id
                        other_team_id = other_ctx.get("team_id")
                        if isinstance(other_team_id, str) and other_team_id:
                            other_message["team"] = other_team_id
                            other_message["bot_profile.team_id"] = other_team_id
                        try:
                            create_entity_by_path(
                                other_ctx,
                                f"{other_channel_path}[{other_channel_id}].messages",
                                other_message,
                                ts,
                            )
                        except Exception:
                            pass

                # Write back the other user's updated context into all_context_data
                if other_kind == "dict" and other_key is not None:
                    all_context_data[other_key] = other_ctx
                elif other_kind == "list" and isinstance(other_key, int):
                    all_context_data[other_key] = other_ctx
    except Exception:
        pass

    # 4.2 Best-effort: for a non-DM channel (e.g., a large group/channel),
    # also sync the message into every user's context that has this channel.
    # This must NOT break the original send behavior, so all errors are swallowed.
    try:
        is_mpim = False
        if isinstance(matched_channel, dict):
            is_mpim = (matched_channel.get("type") == "mpim") or (
                matched_channel.get("is_mpim") is True
            )

        # Only broadcast for non-im, non-mpim channels (i.e. normal group/channel conversations)
        if (not is_im) and (not is_mpim) and isinstance(all_context_data, (dict, list)):

            def _iter_all_contexts(all_ctx):
                if isinstance(all_ctx, dict):
                    for k, v in all_ctx.items():
                        yield ("dict", k, v)
                else:
                    for i, v in enumerate(all_ctx):
                        yield ("list", i, v)

            for kind, key, ctx in _iter_all_contexts(all_context_data):
                if not isinstance(ctx, dict):
                    continue

                # Find the channel in this user's context. Prefer id match; fallback to name match.
                try:
                    ch = get_entity_by_path(ctx, "group_channels", channel_id)
                except Exception:
                    ch = None

                if ch is None and isinstance(channel_name, str) and channel_name:
                    try:
                        chans = (
                            list_entities_by_path(ctx, "group_channels", {}, 10000)
                            or []
                        )
                    except Exception:
                        chans = []
                    for c in chans:
                        if isinstance(c, dict) and c.get("name") == channel_name:
                            ch = c
                            break

                if not isinstance(ch, dict):
                    continue

                target_channel_id = ch.get("id") or ch.get("channel_id") or channel_id

                # Avoid duplicating the same ts message
                try:
                    existing_msg = get_entity_by_path(
                        ctx, f"group_channels[{target_channel_id}].messages", ts
                    )
                except Exception:
                    existing_msg = None
                if existing_msg is not None:
                    continue

                # Insert message (keep the same ts); adjust channel/team fields to match this context where possible.
                msg_for_ctx = dict(message_entity)
                msg_for_ctx["channel"] = target_channel_id
                ctx_team_id = ctx.get("team_id")
                if isinstance(ctx_team_id, str) and ctx_team_id:
                    msg_for_ctx["team"] = ctx_team_id
                    msg_for_ctx["bot_profile.team_id"] = ctx_team_id

                try:
                    create_entity_by_path(
                        ctx,
                        f"group_channels[{target_channel_id}].messages",
                        msg_for_ctx,
                        ts,
                    )
                except Exception:
                    continue

                # Write back updated context
                try:
                    if kind == "dict":
                        all_context_data[key] = ctx
                    else:
                        all_context_data[key] = ctx
                except Exception:
                    pass
    except Exception:
        pass

    # 5. Save context if modified
    # Get context_id from environment variable (same as when loading)
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
    response_result = {
        "ok": True,
        "channel": channel_id,
        "ts": ts,
        "message": {
            "ts": ts,
            "type": "message",
            "text": message_text,
            "channel": channel_id,
            "user": my_user_id,
            "team": team_id,
        },
    }

    wrapped = {
        "meta": None,
        "content": [
            {"type": "text", "text": json.dumps(response_result, ensure_ascii=False)}
        ],
        "isError": False,
    }

    return {"success": True, "error": None, "result": wrapped}
