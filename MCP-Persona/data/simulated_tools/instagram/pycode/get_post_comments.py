import os
from pathlib import Path
import json
from instagram.pycode.dynamic_context_handler import load_context, save_context, get_entity_by_path, list_entities_by_path, create_entity_by_path, update_entity_by_path, delete_entity_by_path

def analyze_response_patterns(parameters_used):
    # 1. Basic input format validation (types, required fields, etc.)
    if not isinstance(parameters_used, dict):
        return {"success": False, "error": "Input must be a JSON object/dict", "result": None}

    allowed_keys = {"after", "graph_api_version", "ig_post_id", "limit"}
    extra_keys = set(parameters_used.keys()) - allowed_keys
    if extra_keys:
        return {"success": False, "error": f"Unexpected parameter(s): {', '.join(sorted(extra_keys))}", "result": None}

    ig_post_id = parameters_used.get("ig_post_id")
    if not isinstance(ig_post_id, str) or not ig_post_id.strip():
        return {"success": False, "error": "ig_post_id is required and must be a non-empty string", "result": None}

    after = parameters_used.get("after", None)
    if after is not None and not isinstance(after, str):
        return {"success": False, "error": "after must be a string or null", "result": None}

    graph_api_version = parameters_used.get("graph_api_version", "v21.0")
    if graph_api_version is None:
        graph_api_version = "v21.0"
    elif not isinstance(graph_api_version, str):
        return {"success": False, "error": "graph_api_version must be a string or null", "result": None}

    limit = parameters_used.get("limit", 25)
    if limit is None:
        limit = 25
    else:
        if not isinstance(limit, int):
            return {"success": False, "error": "limit must be an integer between 1 and 100", "result": None}
        if limit < 1 or limit > 100:
            return {"success": False, "error": "limit must be between 1 and 100", "result": None}

    # 2. Load all context data and select context(s) based on context_id from environment variable
    context_file_path = Path(__file__).parent.parent / "context.json"
    all_context_data = load_context(context_file_path)  # Returns Dict[{context_id}: {context_dict}] or List[Dict]

    # Read context_id from environment variable (NOT from input parameters)
    context_id = os.environ.get("INSTAGRAM_CONTEXT_ID")

    # Determine tool type from tool_name or description to set appropriate default
    tool_name = "get_post_comments"
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

    # 3. Validate entity references exist in context (CRITICAL!)
    # For this tool: Validate that the ig_post_id exists in the context
    # Post structure: posts are stored as media items under each user: user.media.{media_id}
    target_media_info = None

    if isinstance(context_data, list):
        # Multiple contexts - search for post across all users
        for user_info in context_data:
            if not isinstance(user_info, dict):
                continue
            media_dict = user_info.get("media", {})
            if isinstance(media_dict, dict) and ig_post_id in media_dict:
                target_media_info = media_dict[ig_post_id]
                break
    else:
        # Single context
        media_dict = context_data.get("media", {})
        if isinstance(media_dict, dict) and ig_post_id in media_dict:
            target_media_info = media_dict[ig_post_id]

    if not target_media_info:
        return {"success": False, "error": f"Invalid ig_post_id: {ig_post_id}", "result": None}

    # 4. Perform operation (list) - extract comments from media
    all_comments = []
    if isinstance(target_media_info, dict):
        comments_data = target_media_info.get("comments")
        if isinstance(comments_data, dict):
            all_comments = list(comments_data.values())
        elif isinstance(comments_data, list):
            all_comments = comments_data

    if not isinstance(all_comments, list):
        all_comments = []

    total_count = len(all_comments)

    # Pagination handling with 'after' cursor
    start_index = 0
    if after:
        # Try to treat 'after' as a comment ID
        idx_by_id = None
        for i, c in enumerate(all_comments):
            cid = None
            if isinstance(c, dict):
                cid = c.get("id") or c.get("comment_id") or c.get("pk") or c.get("uid")
            elif isinstance(c, str):
                cid = c
            if cid is not None and str(cid) == after:
                idx_by_id = i
                break
        if idx_by_id is not None:
            start_index = idx_by_id + 1
        else:
            # Try idx cursor format
            if after.startswith("idx:"):
                try:
                    n = int(after.split(":", 1)[1])
                    if n >= -1 and n < total_count:
                        start_index = n + 1
                    else:
                        return {"success": False, "error": "Invalid cursor: index out of range", "result": None}
                except Exception:
                    return {"success": False, "error": "Invalid cursor format", "result": None}
            else:
                return {"success": False, "error": "Invalid cursor: comment not found", "result": None}

    end_index = min(start_index + limit, total_count)
    page_comments = all_comments[start_index:end_index]
    has_more = end_index < total_count

    next_cursor = None
    if has_more and page_comments:
        last_item = page_comments[-1]
        next_id = None
        if isinstance(last_item, dict):
            for key in ("id", "comment_id", "pk", "uid"):
                if key in last_item and last_item.get(key) is not None:
                    next_id = str(last_item[key])
                    break
        elif isinstance(last_item, str):
            next_id = last_item
        next_cursor = next_id if next_id else f"idx:{end_index-1}"

    # 5. Save context if modified (not applicable for a get/list tool)

    # 6. Format and return response
    payload = {
        "ig_post_id": ig_post_id,
        "graph_api_version": graph_api_version,
        "count": len(page_comments),
        "total": total_count,
        "limit": limit,
        "paging": {
            "cursors": {
                "after": next_cursor
            },
            "has_more": has_more
        },
        "data": page_comments
    }

    wrapped_result = {
        "meta": None,
        "content": [
            {
                "type": "text",
                "text": json.dumps(payload,ensure_ascii=False),
                "name": "get_post_comments"
            }
        ],
        "isError": False
    }

    return {"success": True, "error": None, "result": wrapped_result}