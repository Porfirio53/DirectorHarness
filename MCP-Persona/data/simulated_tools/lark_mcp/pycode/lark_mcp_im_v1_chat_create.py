from pathlib import Path
import json
import os
import uuid
from datetime import datetime, timezone
from lark_mcp.dynamic_context_handler import (
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

    data = parameters_used.get("data", {})
    if data is None:
        data = {}
    if not isinstance(data, dict):
        return {
            "success": False,
            "error": "Invalid input: 'data' must be an object",
            "result": None,
        }

    avatar = data.get("avatar")
    name = data.get("name")
    description = data.get("description")
    i18n_names = data.get("i18n_names")
    owner_id = data.get("owner_id")
    user_id_list = data.get("user_id_list")

    # Validate 'avatar'
    if avatar is not None and not isinstance(avatar, str):
        return {
            "success": False,
            "error": "Invalid input: 'avatar' must be a string if provided",
            "result": None,
        }

    # Validate 'name'
    if name is not None and not isinstance(name, str):
        return {
            "success": False,
            "error": "Invalid input: 'name' must be a string if provided",
            "result": None,
        }
    # Default name if not provided or empty
    if not name:
        name = "(no title)"
    # Optional: enforce recommended length
    if len(name) > 60:
        return {
            "success": False,
            "error": "Invalid input: 'name' should not exceed 60 characters",
            "result": None,
        }

    # Validate 'description'
    if description is not None and not isinstance(description, str):
        return {
            "success": False,
            "error": "Invalid input: 'description' must be a string if provided",
            "result": None,
        }
    if description is None:
        description = ""

    # Validate 'i18n_names'
    if i18n_names is not None:
        if not isinstance(i18n_names, dict):
            return {
                "success": False,
                "error": "Invalid input: 'i18n_names' must be an object",
                "result": None,
            }
        allowed_i18n_keys = {"zh_cn", "en_us", "ja_jp"}
        for key in i18n_names.keys():
            if key not in allowed_i18n_keys:
                return {
                    "success": False,
                    "error": f"Invalid input: 'i18n_names' contains unsupported key '{key}'",
                    "result": None,
                }
        for k, v in i18n_names.items():
            if v is not None and not isinstance(v, str):
                return {
                    "success": False,
                    "error": f"Invalid input: 'i18n_names.{k}' must be a string",
                    "result": None,
                }
            if v is not None and len(v) > 60:
                return {
                    "success": False,
                    "error": f"Invalid input: 'i18n_names.{k}' should not exceed 60 characters",
                    "result": None,
                }
    else:
        i18n_names = {}

    # Validate 'owner_id'
    if owner_id is not None and not isinstance(owner_id, str):
        return {
            "success": False,
            "error": "Invalid input: 'owner_id' must be a string if provided",
            "result": None,
        }

    # Validate 'user_id_list'
    if user_id_list is not None:
        if not isinstance(user_id_list, list):
            return {
                "success": False,
                "error": "Invalid input: 'user_id_list' must be an array of strings",
                "result": None,
            }
        if len(user_id_list) > 50:
            return {
                "success": False,
                "error": "Invalid input: 'user_id_list' cannot contain more than 50 users",
                "result": None,
            }
        for idx, uid in enumerate(user_id_list):
            if not isinstance(uid, str):
                return {
                    "success": False,
                    "error": f"Invalid input: 'user_id_list[{idx}]' must be a string",
                    "result": None,
                }
    else:
        user_id_list = []

    tool_name = "lark-mcp:im_v1_chat_create"

    # 2. Load all context data and select context(s) based on context_id from environment variable
    context_file_path = Path(json.loads(os.environ.get("LARK_MCP_SANDBOX_PATHS"))[0])
    all_context_data = load_context(
        context_file_path
    )  # Returns Dict[{context_id}: {context_dict}] or List[Dict]

    # Read context_id from environment variable (NOT from input parameters)
    context_id = os.environ.get("LARK_MCP_CONTEXT_ID")

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

    # Since this is a create tool, we cannot operate on multiple contexts at once
    if isinstance(context_data, list):
        return {
            "success": False,
            "error": "Invalid context selection: cannot modify multiple contexts when LARK_MCP_CONTEXT_ID='all'. Set LARK_MCP_CONTEXT_ID to a specific lark_mcp context id.",
            "result": None,
        }

    # 3. Validate entity references exist in context (CRITICAL!)
    # For chat creation, there is no parent entity to validate. Members will be created under the new chat.
    # No external references to validate within this simulation for owner or users.

    # 4. Perform operation (create) using path-based functions
    now_iso = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")

    # Generate chat_id in format: oc_{32hex}
    chat_id = f"oc_{uuid.uuid4().hex}"

    # Generate tenant_key (16 hex digits)
    tenant_key = uuid.uuid4().hex[:16]

    # Get owner_id from current context's open_id if not provided
    if not owner_id:
        owner_id = context_data.get("open_id", "")
    if not owner_id:
        return {
            "success": False,
            "error": "owner_id is required or not available in context",
            "result": None,
        }

    # Generate default avatar if not provided
    if not avatar:
        avatar_middle = uuid.uuid4().hex[:16]
        avatar = f"https://s1-imfile.feishucdn.com/static-resource/v1/v3_{avatar_middle}~?image_size=100x100&cut_type=&quality=&format=png&sticker_format=.webp"

    # Build chat data matching Lark API format
    chat_type = data.get("type", "private")
    chat_data = {
        "chat_id": chat_id,
        "owner_id": owner_id,
        "tenant_key": tenant_key,
        "add_member_permission": "all_members",
        "at_all_permission": "all_members",
        "avatar": avatar,
        "bot_count": "0",
        "bot_manager_id_list": None,
        "chat_mode": "group",
        "chat_status": "normal",
        "chat_tag": "no_tag",
        "chat_type": chat_type,
        "edit_permission": "only_owner",
        "external": False,
        "group_message_type": "chat",
        "hide_member_count_setting": "all_members",
        "join_message_visibility": "all_members",
        "leave_message_visibility": "only_owner",
        "membership_approval": "no_approval_required",
        "moderation_permission": "only_owner",
        "pin_manage_setting": "all_members",
        "restricted_mode_setting.download_has_permission_setting": "only_owner",
        "restricted_mode_setting.message_has_permission_setting": "all_members",
        "restricted_mode_setting.screenshot_has_permission_setting": "all_members",
        "restricted_mode_setting.status": False,
        "share_card_permission": "allowed",
        "urgent_setting": "only_owner",
        "user_manager_id_list": [],
        "video_conference_setting": "only_owner",
        "messages": {},
        "description": description,
        "name": name,
        "user_count": 1,
        "members": {},  # Initialize as empty dict to ensure members are stored as dict
    }

    try:
        # Create the chat entity
        created_chat = create_entity_by_path(context_data, "chats", chat_data, chat_id)
    except Exception as e:
        return {
            "success": False,
            "error": f"Failed to create chat: {str(e)}",
            "result": None,
        }

    # Create members under the chat
    # Only create the current context user as member
    member_path = f"chats[{chat_id}].members"

    # Get current user info from context
    current_user_id = context_data.get("user_id", "")
    current_user_name = context_data.get("name", "")
    current_user_open_id = context_data.get("open_id", "")

    if not current_user_id:
        return {
            "success": False,
            "error": "user_id not found in current context",
            "result": None,
        }

    # Use user_id as member_id (key in members dict)
    member_id = current_user_id
    member_data = {
        "member_id": member_id,
        "member_id_type": "user_id",
        "name": current_user_name,
        "tenant_key": tenant_key,
        "is_owner": True,
    }

    created_members = []
    try:
        created_member = create_entity_by_path(
            context_data, member_path, member_data, member_id
        )
        created_members.append(
            created_member if isinstance(created_member, dict) else member_data
        )
    except Exception as e:
        return {
            "success": False,
            "error": f"Failed to add member '{current_user_id}' to chat '{chat_id}': {str(e)}",
            "result": None,
        }

    # 5. Save context if modified
    # Get context_id from environment variable (same as when loading)
    context_id = os.environ.get("LARK_MCP_CONTEXT_ID")
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
    # Retrieve the final chat and its members to return
    final_chat = get_entity_by_path(context_data, "chats", chat_id)
    # Get members as dict (not list) - members should be stored as dict in final_chat
    members_dict = None
    if isinstance(final_chat, dict) and "members" in final_chat:
        members_dict = final_chat["members"]
    elif isinstance(chat_data, dict) and "members" in chat_data:
        members_dict = chat_data["members"]
    else:
        # Fallback: get from context directly
        members_dict = get_entity_by_path(context_data, f"chats[{chat_id}]", "members")
    # If still None, use empty dict
    if members_dict is None or not isinstance(members_dict, dict):
        members_dict = {}

    result_payload = {
        "chat": final_chat if isinstance(final_chat, dict) else chat_data,
        "members": members_dict,
        "has_more": False,
        "message": "Group chat created successfully",
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
