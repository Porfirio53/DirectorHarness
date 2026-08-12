import os
from pathlib import Path
import json
from instagram.pycode.dynamic_context_handler import load_context, save_context, get_entity_by_path, list_entities_by_path, create_entity_by_path, update_entity_by_path, delete_entity_by_path

def analyze_response_patterns(parameters_used):
    # 1. Basic input format validation (types, required fields, etc.)
    try:
        if not isinstance(parameters_used, dict):
            return {"success": False, "error": "Invalid input: parameters_used must be a dictionary", "result": None}

        allowed_keys = {"conversation_id", "graph_api_version"}
        extra_keys = set(parameters_used.keys()) - allowed_keys
        if extra_keys:
            return {"success": False, "error": f"Unknown parameter(s): {', '.join(sorted(extra_keys))}", "result": None}

        if "conversation_id" not in parameters_used:
            return {"success": False, "error": "Missing required field: conversation_id", "result": None}

        conversation_id = parameters_used.get("conversation_id")
        if not isinstance(conversation_id, str) or not conversation_id.strip():
            return {"success": False, "error": "Invalid conversation_id: must be a non-empty string", "result": None}
        conversation_id = conversation_id.strip()

        graph_api_version = parameters_used.get("graph_api_version", "v21.0")
        if graph_api_version is None:
            graph_api_version = "v21.0"
        if not isinstance(graph_api_version, str):
            return {"success": False, "error": "Invalid graph_api_version: must be a string or null", "result": None}
    except Exception as e:
        return {"success": False, "error": f"Input validation error: {str(e)}", "result": None}

    # 2. Load all context data and select context(s) based on context_id from environment variable
    try:
        context_file_path = Path(__file__).parent.parent / "context.json"
        all_context_data = load_context(context_file_path)  # Returns Dict[{context_id}: {context_dict}] or List[Dict]

        # Read context_id from environment variable (NOT from input parameters)
        context_id = os.environ.get("INSTAGRAM_CONTEXT_ID")

        # Determine tool type from tool_name or description to set appropriate default
        tool_name = "get_conversation"
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
    except Exception as e:
        return {"success": False, "error": f"Failed to load/select context: {str(e)}", "result": None}

    # 3. Validate entity references exist in context (CRITICAL!) and locate conversation
    # Conversation structure: user.conversations.{conversation_id}
    try:
        found_conversation = None
        found_context_id = None

        if context_id == "all":
            if isinstance(all_context_data, dict):
                # Search across all contexts with their IDs
                for cid_key, ctx in all_context_data.items():
                    if isinstance(ctx, dict):
                        convs = ctx.get("conversations", {})
                        if isinstance(convs, dict) and conversation_id in convs:
                            found_conversation = convs[conversation_id]
                            found_context_id = cid_key
                            break
            elif isinstance(all_context_data, list):
                for idx, ctx in enumerate(all_context_data):
                    if isinstance(ctx, dict):
                        convs = ctx.get("conversations", {})
                        if isinstance(convs, dict) and conversation_id in convs:
                            found_conversation = convs[conversation_id]
                            # Infer context identifier
                            candidate_ctx_id = ctx.get("id") or ctx.get("user_id") or ctx.get("account_id")
                            found_context_id = candidate_ctx_id if candidate_ctx_id is not None else f"index:{idx}"
                            break
        else:
            # Single context
            if isinstance(context_data, dict):
                convs = context_data.get("conversations", {})
                if isinstance(convs, dict) and conversation_id in convs:
                    found_conversation = convs[conversation_id]
                    found_context_id = context_id

        if not found_conversation:
            return {"success": False, "error": f"Conversation with id '{conversation_id}' not found", "result": None}
    except Exception as e:
        return {"success": False, "error": f"Error locating conversation: {str(e)}", "result": None}

    # 4. Perform operation (list/create/update/delete) using path-based functions
    # This is a 'get' operation; we already have the entity. We can assemble response data.
    try:
        response_payload = {
            "tool": "get_conversation",
            "graph_api_version": graph_api_version,
            "conversation_id": conversation_id,
            "context_id": found_context_id,
            "conversation": found_conversation
        }
    except Exception as e:
        return {"success": False, "error": f"Failed to assemble response: {str(e)}", "result": None}

    # 5. Save context if modified (not applicable for 'get' operation; ensure no save is attempted)

    # 6. Format and return response
    try:
        result_wrapper = {
            "meta": None,
            "content": [
                {"type": "text", "text": json.dumps(response_payload,ensure_ascii=False)}
            ],
            "isError": False
        }
        return {"success": True, "error": None, "result": result_wrapper}
    except Exception as e:
        return {"success": False, "error": f"Failed to format response: {str(e)}", "result": None}