import os
import uuid
from datetime import datetime, timezone
from pathlib import Path
import json
from instagram.pycode.dynamic_context_handler import load_context, save_context, get_entity_by_path, list_entities_by_path, create_entity_by_path, update_entity_by_path, delete_entity_by_path

def analyze_response_patterns(parameters_used):
    # 1. Basic input format validation (types, required fields, etc.)
    try:
        if not isinstance(parameters_used, dict):
            return {"success": False, "error": "Invalid input: expected an object", "result": None}

        # Required parameters
        ig_comment_id = parameters_used.get("ig_comment_id")
        message = parameters_used.get("message")
        graph_api_version = parameters_used.get("graph_api_version", "v21.0")

        if ig_comment_id is None:
            return {"success": False, "error": "Missing required field: ig_comment_id", "result": None}
        if not isinstance(ig_comment_id, str) or ig_comment_id.strip() == "":
            return {"success": False, "error": "Invalid ig_comment_id: must be a non-empty string", "result": None}

        if message is None:
            return {"success": False, "error": "Missing required field: message", "result": None}
        if not isinstance(message, str) or message.strip() == "":
            return {"success": False, "error": "Invalid message: must be a non-empty string", "result": None}

        if graph_api_version is not None and not isinstance(graph_api_version, str):
            return {"success": False, "error": "Invalid graph_api_version: must be a string or null", "result": None}

        tool_name = "reply_to_comment"
    except Exception as e:
        return {"success": False, "error": f"Input validation error: {str(e)}", "result": None}

    # 2. Load all context data and select context(s) based on context_id from environment variable
    try:
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

        # For modify tools, ensure we are operating on a single context dict
        if not is_query_tool and isinstance(context_data, list):
            return {"success": False, "error": "Modify operation cannot be performed with INSTAGRAM_CONTEXT_ID='all'. Please specify a single context.", "result": None}

        if not isinstance(context_data, dict):
            return {"success": False, "error": "Invalid context format: expected a dictionary for modify operations", "result": None}
    except Exception as e:
        return {"success": False, "error": f"Failed to load/select context: {str(e)}", "result": None}

    # 3. Validate entity references exist in context (CRITICAL!)
    # Since entity_paths are not predefined, perform a robust recursive search for the comment.
    try:
        target_id = ig_comment_id

        def find_comment(node, target_id_str):
            # Returns reference to the comment dict and the path ancestors list
            visited = set()

            def _search(current, ancestors):
                obj_id = id(current)
                if obj_id in visited:
                    return None
                visited.add(obj_id)

                if isinstance(current, dict):
                    # Check for identifier match
                    potential_keys = ["id", "comment_id", "ig_comment_id"]
                    for k in potential_keys:
                        val = current.get(k)
                        if val is not None and str(val) == target_id_str:
                            # Heuristic: ensure it's a comment-like object
                            # Look for common comment fields
                            if any(kk in current for kk in ["text", "message", "username", "timestamp", "replies", "like_count"]):
                                return current, ancestors
                            # Even if heuristic fails, if keys match, we can still consider it
                            return current, ancestors
                    # Traverse deeper
                    for k, v in current.items():
                        res = _search(v, ancestors + [(current, k)])
                        if res:
                            return res
                elif isinstance(current, list):
                    for idx, item in enumerate(current):
                        res = _search(item, ancestors + [(current, idx)])
                        if res:
                            return res
                return None

            return _search(node, [])

        found = None

        # Attempt 1: Try known paths with get_entity_by_path (if supported by context)
        # We try a few common instagram comment storage patterns
        candidate_paths = [
            "instagram.comments",
            "ig.comments",
            "comments",
            "instagram.media[*].comments",
            "media[*].comments"
        ]
        for path in candidate_paths:
            try:
                entity = get_entity_by_path(context_data, path, target_id)
                if isinstance(entity, dict):
                    # If we got the entity via path function, we still need the actual dict reference in context_data
                    # Use recursive search to get the reference and ancestors
                    found = find_comment(context_data, target_id)
                    if found:
                        break
            except Exception:
                # Ignore path errors and continue
                pass

        # Fallback: deep recursive search if not found via path
        if not found:
            found = find_comment(context_data, target_id)

        if not found:
            return {"success": False, "error": f"Invalid ig_comment_id: {ig_comment_id}", "result": None}

        comment_ref, ancestors_path = found
    except Exception as e:
        return {"success": False, "error": f"Failed to validate ig_comment_id: {str(e)}", "result": None}

    # 4. Perform operation (create reply)
    try:
        # Get current user info from context
        from_user_id = context_data.get("id", "")
        from_user_username = context_data.get("username", "")

        # Create reply object with format matching context.json
        now_iso = datetime.now(timezone.utc).isoformat()
        reply_id = f"{ig_comment_id}_{uuid.uuid4().hex[:10]}"
        reply_obj = {
            "id": reply_id,
            "text": message,
            "timestamp": now_iso,
            "from_user.id": from_user_id,
            "from_user.username": from_user_username,
            "like_count": 0,
            "replies": None,
        }

        # Append to replies dict (replies is a dict in context.json format)
        if "replies" not in comment_ref or comment_ref.get("replies") is None:
            comment_ref["replies"] = {}
        if not isinstance(comment_ref["replies"], dict):
            comment_ref["replies"] = {}
        comment_ref["replies"][reply_id] = reply_obj

        # Update reply_count if present and valid
        if isinstance(comment_ref.get("reply_count"), int):
            comment_ref["reply_count"] += 1

        # 5. Save context if modified
        # Get context_id from environment variable (same as when loading)
        context_id_to_save = os.environ.get("INSTAGRAM_CONTEXT_ID")
        if context_id_to_save is None:
            # Use first key if dict, or first element if list
            if isinstance(all_context_data, dict) and len(all_context_data) > 0:
                context_id_to_save = list(all_context_data.keys())[0]

        # Update the modified context back to all_context_data
        if isinstance(all_context_data, dict):
            # Dict format: {context_id: context_dict, ...}
            if context_id_to_save and context_id_to_save != "all" and context_id_to_save in all_context_data:
                all_context_data[context_id_to_save] = context_data  # Update the specific context
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
            "id": reply_id,
            "parent_id": ig_comment_id,
            "message": message,
            "created_time": now_iso,
            "graph_api_version": graph_api_version,
        }
        formatted_result = {
            "meta": None,
            "content": [
                {
                    "type": "text",
                    "text": json.dumps({"reply": response_payload},ensure_ascii=False)
                }
            ],
            "isError": False
        }
        return {"success": True, "error": None, "result": formatted_result}
    except Exception as e:
        return {"success": False, "error": f"Failed to create reply: {str(e)}", "result": None}