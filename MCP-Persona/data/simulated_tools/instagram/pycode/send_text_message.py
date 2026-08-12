import os
import uuid
from datetime import datetime
from pathlib import Path
import json
from instagram.pycode.dynamic_context_handler import load_context, save_context, get_entity_by_path, list_entities_by_path, create_entity_by_path, update_entity_by_path, delete_entity_by_path

def analyze_response_patterns(parameters_used):
    # 1. Basic input format validation (types, required fields, etc.)
    tool_name = "send_text_message"
    allowed_keys = {"graph_api_version", "ig_user_id", "recipient_id", "reply_to_message_id", "text"}
    if not isinstance(parameters_used, dict):
        return {"success": False, "error": "Input must be a JSON object (dict)", "result": None}
    extra_keys = set(parameters_used.keys()) - allowed_keys
    if extra_keys:
        return {"success": False, "error": f"Unexpected parameter(s): {', '.join(sorted(extra_keys))}", "result": None}
    # Defaults
    graph_api_version = parameters_used.get("graph_api_version", "v21.0")
    ig_user_id = parameters_used.get("ig_user_id", None)
    recipient_id = parameters_used.get("recipient_id")
    reply_to_message_id = parameters_used.get("reply_to_message_id", None)
    text = parameters_used.get("text")
    # Type and required validations
    if recipient_id is None:
        return {"success": False, "error": "recipient_id is required", "result": None}
    if not isinstance(recipient_id, str) or recipient_id.strip() == "":
        return {"success": False, "error": "recipient_id must be a non-empty string", "result": None}
    if text is None:
        return {"success": False, "error": "text is required", "result": None}
    if not isinstance(text, str):
        return {"success": False, "error": "text must be a string", "result": None}
    if graph_api_version is not None and not isinstance(graph_api_version, str):
        return {"success": False, "error": "graph_api_version must be a string or null", "result": None}
    if ig_user_id is not None and not isinstance(ig_user_id, str):
        return {"success": False, "error": "ig_user_id must be a string or null", "result": None}
    if reply_to_message_id is not None and not isinstance(reply_to_message_id, str):
        return {"success": False, "error": "reply_to_message_id must be a string or null", "result": None}

    # 2. Load all context data and select context(s) based on context_id from environment variable
    context_file_path = Path(__file__).parent.parent / "context.json"
    all_context_data = load_context(context_file_path)  # Returns Dict[{context_id}: {context_dict}] or List[Dict]
    
    # Read context_id from environment variable (NOT from input parameters)
    context_id = os.environ.get("INSTAGRAM_CONTEXT_ID")
    
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

    # Disallow "all" context for modifying tool
    if not is_query_tool and isinstance(context_data, list):
        return {"success": False, "error": "This tool modifies context and requires a single context. Set INSTAGRAM_CONTEXT_ID to a specific context id.", "result": None}

    # 3. Validate entity references exist in context (CRITICAL!)
    # 3.1 Determine sender_id
    sender_id = ig_user_id if ig_user_id else context_data.get("id")
    if not sender_id:
        return {"success": False, "error": "Cannot determine sender_id", "result": None}

    # 3.2 Validate ig_user_id if provided against current context
    if ig_user_id:
        if context_data.get("id") != ig_user_id:
            return {"success": False, "error": f"Invalid ig_user_id: {ig_user_id}", "result": None}

    # 3.3 Find target conversation by matching participants [sender_id, recipient_id]
    target_conversation_id = None
    target_conversation_ref = None

    if isinstance(context_data, dict):
        conversations = context_data.get("conversations", {})
        if isinstance(conversations, dict):
            for conv_id, conv_obj in conversations.items():
                if isinstance(conv_obj, dict):
                    participants = conv_obj.get("participants", [])
                    if isinstance(participants, list):
                        # Check if both sender_id and recipient_id are in participants
                        participants_set = set(str(p) for p in participants)
                        if str(sender_id) in participants_set and str(recipient_id) in participants_set:
                            target_conversation_id = conv_id
                            target_conversation_ref = conv_obj
                            break

    if not target_conversation_ref:
        return {"success": False, "error": f"Conversation between {sender_id} and {recipient_id} not found", "result": None}

    # 3.4 Validate reply_to_message_id if provided
    if reply_to_message_id:
        messages = target_conversation_ref.get("messages", {})
        if isinstance(messages, dict) and reply_to_message_id not in messages:
            return {"success": False, "error": f"Invalid reply_to_message_id: {reply_to_message_id}", "result": None}

    # 4. Perform operation (create message)
    # 4.1 Find recipient_username from existing messages in the conversation
    recipient_username = None
    messages = target_conversation_ref.get("messages", {})
    if isinstance(messages, dict):
        for msg_obj in messages.values():
            if isinstance(msg_obj, dict):
                # Check if this message has the recipient as the 'to' field
                if str(msg_obj.get("to.id")) == str(recipient_id):
                    recipient_username = msg_obj.get("to.username")
                    break
                # Also check if recipient is the 'from' field (they sent a message before)
                if str(msg_obj.get("from.id")) == str(recipient_id):
                    recipient_username = msg_obj.get("from.username")
                    break

    # 4.2 Get sender username from context
    sender_username = context_data.get("username", "")

    # 4.3 Create message entity with correct format matching context.json
    message_id = f"msg_{uuid.uuid4().hex}"
    timestamp_iso = datetime.utcnow().isoformat() + "Z"
    message_entity = {
        "id": message_id,
        "text": text,
        "attachments": None,
        "created_time": timestamp_iso,
        "from.id": sender_id,
        "from.username": sender_username,
        "to.id": recipient_id,
        "to.username": recipient_username,
    }

    # Add message to conversation.messages
    if "messages" not in target_conversation_ref or not isinstance(target_conversation_ref["messages"], dict):
        target_conversation_ref["messages"] = {}
    target_conversation_ref["messages"][message_id] = message_entity

    # 5. Save context if modified
    # Get context_id from environment variable (same as when loading)
    context_id_save = os.environ.get("INSTAGRAM_CONTEXT_ID")
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
    response_payload = {
        "message": message_entity
    }
    result = {
        "meta": None,
        "content": [
            {
                "type": "text",
                "text": json.dumps(response_payload,ensure_ascii=False)
            }
        ],
        "isError": False
    }
    return {"success": True, "error": None, "result": result}