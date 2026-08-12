import os
from pathlib import Path
import json
from instagram.pycode.dynamic_context_handler import (
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
    try:
        if parameters_used is None:
            parameters_used = {}
        if not isinstance(parameters_used, dict):
            return {
                "success": False,
                "error": "Input must be an object",
                "result": None,
            }

        allowed_keys = {"after", "graph_api_version", "ig_user_id", "limit"}
        unexpected = [k for k in parameters_used.keys() if k not in allowed_keys]
        if unexpected:
            return {
                "success": False,
                "error": f"Unexpected parameter(s): {', '.join(unexpected)}",
                "result": None,
            }

        after = parameters_used.get("after", None)
        graph_api_version = parameters_used.get("graph_api_version", "v21.0")
        ig_user_id = parameters_used.get("ig_user_id", None)
        limit = parameters_used.get("limit", 25)

        if after is not None and not isinstance(after, str):
            return {
                "success": False,
                "error": "Parameter 'after' must be a string or null",
                "result": None,
            }

        if graph_api_version is None:
            graph_api_version = "v21.0"
        elif not isinstance(graph_api_version, str):
            return {
                "success": False,
                "error": "Parameter 'graph_api_version' must be a string or null",
                "result": None,
            }

        if ig_user_id is not None and not isinstance(ig_user_id, (str, int)):
            return {
                "success": False,
                "error": "Parameter 'ig_user_id' must be a string, number, or null",
                "result": None,
            }
        if isinstance(ig_user_id, int):
            ig_user_id = str(ig_user_id)

        if limit is None:
            limit = 25
        if not isinstance(limit, int) or limit < 1 or limit > 100:
            return {
                "success": False,
                "error": "Parameter 'limit' must be an integer between 1 and 100",
                "result": None,
            }

    except Exception as e:
        return {
            "success": False,
            "error": f"Input validation error: {str(e)}",
            "result": None,
        }

    # 2. Load all context data and select context(s) based on context_id from environment variable
    try:
        tool_name = "get_user_media"
        tool_name_lower = tool_name.lower() if tool_name else ""
        context_file_path = Path(__file__).parent.parent / "context.json"
        all_context_data = load_context(
            context_file_path
        )  # Returns Dict[{context_id}: {context_dict}] or List[Dict]

        # Read context_id from environment variable (NOT from input parameters)
        context_id = os.environ.get("INSTAGRAM_CONTEXT_ID")

        # Determine tool type from tool_name or description to set appropriate default
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
                # Try to find context by id field
                for ctx in all_context_data:
                    if isinstance(ctx, dict) and ctx.get("id") == context_id:
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
    except Exception as e:
        return {
            "success": False,
            "error": f"Failed to load context: {str(e)}",
            "result": None,
        }

    # Helper for extracting media ID from item
    def _media_id(item):
        if not isinstance(item, dict):
            return None
        return (
            item.get("id") or item.get("media_id") or item.get("pk") or item.get("code")
        )

    # 3. Validate entity references and collect media
    all_media = []
    target_user = None

    try:
        if isinstance(context_data, list):
            # Multiple contexts (default for query tools)
            if ig_user_id:
                # Find specific user and collect their media
                for user_info in context_data:
                    if (
                        isinstance(user_info, dict)
                        and user_info.get("id") == ig_user_id
                    ):
                        target_user = user_info
                        break
                if not target_user:
                    return {
                        "success": False,
                        "error": f"Invalid ig_user_id: {ig_user_id}",
                        "result": None,
                    }
            else:
                # No ig_user_id provided - use the first user
                if context_data and isinstance(context_data[0], dict):
                    target_user = context_data[0]
                else:
                    return {
                        "success": False,
                        "error": "No users found in context",
                        "result": None,
                    }
            # Collect media from target user
            if "media" in target_user and isinstance(target_user["media"], dict):
                all_media = list(target_user["media"].values())
        else:
            # Single context dict
            if ig_user_id:
                if context_data.get("id") != ig_user_id:
                    return {
                        "success": False,
                        "error": f"Invalid ig_user_id: {ig_user_id}",
                        "result": None,
                    }
            target_user = context_data
            # Collect media from the single user
            if "media" in target_user and isinstance(target_user["media"], dict):
                all_media = list(target_user["media"].values())
    except Exception as e:
        return {
            "success": False,
            "error": f"Error during entity validation/listing: {str(e)}",
            "result": None,
        }

    # 4. Perform operation (list) using pagination logic
    # Implement 'after' cursor as the id of the last seen item
    try:
        start_index = 0
        if after:
            # Find the item with id == after
            idx = -1
            for i, it in enumerate(all_media):
                if _media_id(it) == after:
                    idx = i
                    break
            if idx >= 0:
                start_index = idx + 1
            else:
                # If cursor not found, start from 0
                start_index = 0

        end_index = min(start_index + limit, len(all_media))
        page_items = all_media[start_index:end_index]
        has_next_page = end_index < len(all_media)
        next_cursor = (
            _media_id(page_items[-1]) if page_items and has_next_page else None
        )
        before_cursor = _media_id(page_items[0]) if page_items else None

        # 5. Save context if modified (not applicable for get/list; no modifications)
        # (No changes to save)

        # 6. Format and return response
        result_payload = {
            "graph_api_version": graph_api_version,
            "count": len(page_items),
            "data": page_items,
            "paging": {
                "cursors": {"before": before_cursor, "after": next_cursor},
                "has_next_page": has_next_page,
            },
        }

        formatted_result = {
            "meta": None,
            "content": [
                {"type": "text", "text": json.dumps(result_payload, ensure_ascii=False)}
            ],
            "isError": False,
        }
        return {"success": True, "error": None, "result": formatted_result}
    except Exception as e:
        return {
            "success": False,
            "error": f"Failed to paginate or format result: {str(e)}",
            "result": None,
        }
