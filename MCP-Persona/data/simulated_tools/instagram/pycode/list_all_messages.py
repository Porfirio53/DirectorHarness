import os
from pathlib import Path
import json
from instagram.pycode.dynamic_context_handler import load_context, save_context, get_entity_by_path, list_entities_by_path, create_entity_by_path, update_entity_by_path, delete_entity_by_path

def analyze_response_patterns(parameters_used):
    # 1. Basic input format validation (types, required fields, etc.)
    if not isinstance(parameters_used, dict):
        return {"success": False, "error": "Invalid input: parameters_used must be a dictionary", "result": None}

    allowed_keys = {"after", "conversation_id", "graph_api_version", "limit"}
    unexpected_keys = set(parameters_used.keys()) - allowed_keys
    if unexpected_keys:
        return {"success": False, "error": f"Unexpected parameter(s): {', '.join(sorted(unexpected_keys))}", "result": None}

    # Required field: conversation_id
    if "conversation_id" not in parameters_used:
        return {"success": False, "error": "conversation_id is required", "result": None}
    conversation_id = parameters_used.get("conversation_id")
    if not isinstance(conversation_id, str) or not conversation_id.strip():
        return {"success": False, "error": "conversation_id must be a non-empty string", "result": None}
    conversation_id = conversation_id.strip()

    # Optional: after (string or None)
    after = parameters_used.get("after", None)
    if after is not None and not isinstance(after, str):
        return {"success": False, "error": "after must be a string or null", "result": None}

    # Optional: graph_api_version (string or None), default "v21.0"
    graph_api_version = parameters_used.get("graph_api_version", "v21.0")
    if graph_api_version is None:
        graph_api_version = "v21.0"
    if not isinstance(graph_api_version, str):
        return {"success": False, "error": "graph_api_version must be a string or null", "result": None}

    # Optional: limit (int in [1, 200] or None), default 25
    limit = parameters_used.get("limit", 25)
    if limit is None:
        limit = 25
    if not isinstance(limit, int):
        return {"success": False, "error": "limit must be an integer or null", "result": None}
    if limit < 1 or limit > 200:
        return {"success": False, "error": "limit must be between 1 and 200", "result": None}

    # 2. Load all context data and select context(s) based on context_id from environment variable
    context_file_path = Path(__file__).parent.parent / "context.json"
    all_context_data = load_context(context_file_path)  # Returns Dict[{context_id}: {context_dict}] or List[Dict]

    # Read context_id from environment variable (NOT from input parameters)
    context_id = os.environ.get("INSTAGRAM_CONTEXT_ID")

    # Determine tool type from tool_name or description to set appropriate default
    tool_name = "list_all_messages"
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
            # Try to find context by id field
            for ctx in all_context_data:
                if isinstance(ctx, dict) and ctx.get("id") == context_id:
                    context_data = ctx
                    break
            else:
                return {"success": False, "error": f"Context ID '{context_id}' not found", "result": None}
    else:
        # Single context dict
        context_data = all_context_data

    # Normalize contexts to a list for multi-context search
    contexts_list = context_data if isinstance(context_data, list) else [context_data]

    # 3. Validate entity references exist in context (CRITICAL!)
    # Attempt to locate the conversation in one or more contexts
    # Conversation structure: user.conversations.{conversation_id}
    found_convs = []
    for ctx in contexts_list:
        if not isinstance(ctx, dict):
            continue
        conversations_dict = ctx.get("conversations", {})
        if isinstance(conversations_dict, dict) and conversation_id in conversations_dict:
            found_convs.append((ctx, conversations_dict[conversation_id]))

    if not found_convs:
        return {"success": False, "error": f"Invalid conversation_id: {conversation_id}", "result": None}

    # 4. Perform operation (list) with pagination
    # Gather all messages across found contexts
    aggregated_messages = []
    for ctx, conv in found_convs:
        if isinstance(conv, dict):
            messages_dict = conv.get("messages", {})
            if isinstance(messages_dict, dict):
                aggregated_messages.extend(messages_dict.values())
            elif isinstance(messages_dict, list):
                aggregated_messages.extend(messages_dict)

    # Build index mapping for cursor handling
    # Cursor "after" refers to message id; if not found but provided, return error
    start_index = 0
    if after is not None:
        index_found = None
        for i, msg in enumerate(aggregated_messages):
            msg_id = msg.get("id")
            if isinstance(msg_id, (str, int)) and str(msg_id) == str(after):
                index_found = i
                break
        if index_found is None:
            return {"success": False, "error": f"Invalid cursor 'after': {after}", "result": None}
        start_index = index_found + 1

    # Slice according to limit
    end_index = min(start_index + limit, len(aggregated_messages))
    page_messages = aggregated_messages[start_index:end_index]

    has_more = end_index < len(aggregated_messages)
    next_after = None
    if has_more and len(page_messages) > 0:
        last_msg = page_messages[-1]
        last_id = last_msg.get("id")
        if last_id is not None:
            next_after = str(last_id)

    # 5. Save context if modified (not applicable for list operations)
    # No context modification is performed here

    # 6. Format and return response
    output = {
        "conversation_id": conversation_id,
        "graph_api_version": graph_api_version,
        "messages": page_messages,
        "paging": {
            "has_more": has_more,
            "next_after": next_after,
            "total": len(aggregated_messages),
            "returned": len(page_messages),
            "start_index": start_index
        }
    }

    return {
        "success": True,
        "error": None,
        "result": {
            "meta": None,
            "content": [
                {
                    "type": "text",
                    "text": json.dumps(output)
                }
            ],
            "isError": False
        }
    }