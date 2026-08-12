from pathlib import Path
import json
import os
import uuid
import time
from lark_mcp.dynamic_context_handler import load_context, save_context, get_entity_by_path, list_entities_by_path, create_entity_by_path, update_entity_by_path, delete_entity_by_path

def analyze_response_patterns(parameters_used):
    # 1. Basic input format validation (types, required fields, etc.)
    if parameters_used is None or not isinstance(parameters_used, dict):
        return {"success": False, "error": "Invalid input: parameters_used must be a dictionary", "result": None}
    data = parameters_used.get("data")
    if data is None or not isinstance(data, dict):
        return {"success": False, "error": "Invalid input: 'data' field must be provided as a dictionary", "result": None}
    receive_id = data.get("receive_id")
    receive_id_type = data.get("receive_id_type", "open_id")
    msg_type = data.get("msg_type")
    content_str = data.get("content")
    uuid_in = data.get("uuid")

    # Validate receive_id_type
    allowed_receive_id_types = {"open_id", "chat_id", "user_id", "union_id", "email", "phone"}
    if receive_id_type not in allowed_receive_id_types:
        return {"success": False, "error": f"Invalid receive_id_type: '{receive_id_type}'. Allowed: {sorted(list(allowed_receive_id_types))}", "result": None}

    if not isinstance(receive_id, str) or not receive_id.strip():
        return {"success": False, "error": "Invalid input: 'receive_id' must be a non-empty string", "result": None}
    if not isinstance(msg_type, str) or not msg_type.strip():
        return {"success": False, "error": "Invalid input: 'msg_type' must be a non-empty string", "result": None}
    if not isinstance(content_str, str) or not content_str.strip():
        return {"success": False, "error": "Invalid input: 'content' must be a non-empty JSON string", "result": None}

    allowed_msg_types = [
        "text", "post", "image", "file", "audio", "media", "sticker",
        "interactive", "share_chat", "share_user", "system"
    ]
    if msg_type not in allowed_msg_types:
        return {"success": False, "error": f"Invalid msg_type: '{msg_type}'. Allowed: {', '.join(allowed_msg_types)}", "result": None}

    # Validate content is a valid JSON string
    try:
        json.loads(content_str)
    except Exception as e:
        return {"success": False, "error": f"Invalid content: must be a valid JSON string. Error: {str(e)}", "result": None}

    # Size limits: text <= 150KB, interactive/post <= 30KB
    content_size_bytes = len(content_str.encode("utf-8"))
    if msg_type == "text" and content_size_bytes > 150 * 1024:
        return {"success": False, "error": f"Content too large for 'text' message: {content_size_bytes} bytes (max 153600)", "result": None}
    if msg_type in ["interactive", "post"] and content_size_bytes > 30 * 1024:
        return {"success": False, "error": f"Content too large for '{msg_type}' message: {content_size_bytes} bytes (max 30720)", "result": None}

    # 2. Load all context data and select context(s) based on context_id from environment variable
    context_file_path = Path(json.loads(os.environ.get("LARK_MCP_SANDBOX_PATHS"))[0])
    all_context_data = load_context(context_file_path)  # Returns Dict[{context_id}: {context_dict}] or List[Dict]

    # Read context_id from environment variable (NOT from input parameters)
    context_id = os.environ.get("LARK_MCP_CONTEXT_ID")

    # Determine tool type from tool_name or description to set appropriate default
    tool_name = "lark-mcp:im_v1_message_create"
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
            found_ctx = None
            for ctx in all_context_data:
                if isinstance(ctx, dict) and ctx.get("user_id") == context_id:
                    found_ctx = ctx
                    break
            if found_ctx is None:
                return {"success": False, "error": f"Context ID '{context_id}' not found", "result": None}
            context_data = found_ctx
    else:
        # Single context dict
        context_data = all_context_data

    # 3. Validate entity references exist in context (CRITICAL!)
    # For this tool, we need to validate that the receiving chat or member exists.
    # First, try: receive_id corresponds to a chat_id
    chat = get_entity_by_path(context_data, "chats", receive_id)

    # If not found, try to locate a chat by member_id (user recipient)
    selected_chat_id = None
    if chat and isinstance(chat, dict):
        selected_chat_id = chat.get("chat_id")
    else:
        # Search chats that contain member with member_id == receive_id
        chats = list_entities_by_path(context_data, "chats", {}, 10000)
        for ch in chats:
            cid = ch.get("chat_id")
            if not cid:
                continue
            members = list_entities_by_path(context_data, f"chats[{cid}].members", {}, 10000)
            for m in members:
                if m.get("member_id") == receive_id:
                    selected_chat_id = cid
                    break
            if selected_chat_id:
                break

    if not selected_chat_id:
        return {"success": False, "error": f"Invalid receive_id: '{receive_id}'. No matching chat or member found in context", "result": None}

    # 4. Perform operation (create) using path-based functions
    # Deduplication by uuid within 1 hour
    now_ts = int(time.time())
    if isinstance(uuid_in, str) and uuid_in.strip():
        try:
            existing_messages = list_entities_by_path(context_data, "chats[*].messages", {}, 10000)
        except Exception:
            # Fallback in case wildcard path is unsupported: aggregate per chat
            existing_messages = []
            chats = list_entities_by_path(context_data, "chats", {}, 10000)
            for ch in chats:
                cid = ch.get("chat_id")
                if not cid:
                    continue
                msgs = list_entities_by_path(context_data, f"chats[{cid}].messages", {}, 10000)
                existing_messages.extend(msgs)

        for msg in existing_messages:
            if msg.get("uuid") == uuid_in:
                create_time_str = msg.get("create_time")
                try:
                    create_time = int(create_time_str) if isinstance(create_time_str, str) else 0
                except (ValueError, TypeError):
                    create_time = 0
                if create_time and (now_ts - create_time) <= 3600:
                    # Return existing message due to deduplication
                    existing_result = {
                        "message": msg,
                        "deduplicated": True
                    }
                    return {
                        "success": True,
                        "error": None,
                        "result": {
                            "meta": None,
                            "content": [
                                {"type": "text", "text": json.dumps(existing_result,ensure_ascii=False)}
                            ],
                            "isError": False
                        }
                    }

    # Create new message
    # Get sender info from current context
    sender_open_id = context_data.get("open_id", "")
    sender_tenant_key = context_data.get("tenant_key", "")
    if not sender_tenant_key:
        # Generate tenant_key if not available in context
        sender_tenant_key = uuid.uuid4().hex[:16]

    message_id = "om_" + uuid.uuid4().hex
    message_entity = {
        "message_id": message_id,
        "deleted": False,
        "msg_type": msg_type,
        "sender.id": sender_open_id,
        "sender.id_type": "open_id",
        "sender.sender_type": "user",
        "sender.tenant_key": sender_tenant_key,
        "updated": False,
        "body.content": content_str,
        "create_time": str(now_ts),
        "update_time": str(now_ts),
        "uuid": uuid_in if isinstance(uuid_in, str) and uuid_in.strip() else None,
    }

    try:
        created = create_entity_by_path(context_data, f"chats[{selected_chat_id}].messages", message_entity, message_id)
    except Exception as e:
        return {"success": False, "error": f"Failed to create message: {str(e)}", "result": None}

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

    try:
        save_context(context_file_path, all_context_data)
    except Exception as e:
        return {"success": False, "error": f"Failed to save context: {str(e)}", "result": None}

    # 6. Format and return response
    output_result = {
        "message": created,
        "deduplicated": False
    }
    return {
        "success": True,
        "error": None,
        "result": {
            "meta": None,
            "content": [
                {"type": "text", "text": json.dumps(output_result,ensure_ascii=False)}
            ],
            "isError": False
        }
    }