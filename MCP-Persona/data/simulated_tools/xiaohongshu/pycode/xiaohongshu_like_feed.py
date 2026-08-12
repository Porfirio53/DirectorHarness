from pathlib import Path
import os
import json
from xiaohongshu.pycode.dynamic_context_handler import (
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
        return {"success": False, "error": "Input must be a dictionary", "result": None}
    allowed_keys = {"feed_id", "unlike", "xsec_token"}
    extra_keys = set(parameters_used.keys()) - allowed_keys
    if extra_keys:
        return {
            "success": False,
            "error": f"Unexpected parameter(s): {', '.join(sorted(extra_keys))}",
            "result": None,
        }
    if "feed_id" not in parameters_used:
        return {
            "success": False,
            "error": "Missing required field: feed_id",
            "result": None,
        }
    if "xsec_token" not in parameters_used:
        return {
            "success": False,
            "error": "Missing required field: xsec_token",
            "result": None,
        }
    feed_id = parameters_used.get("feed_id")
    xsec_token = parameters_used.get("xsec_token")
    unlike = parameters_used.get("unlike", False)
    if not isinstance(feed_id, str) or not feed_id.strip():
        return {
            "success": False,
            "error": "feed_id must be a non-empty string",
            "result": None,
        }
    if not isinstance(xsec_token, str) or not xsec_token.strip():
        return {
            "success": False,
            "error": "xsec_token must be a non-empty string",
            "result": None,
        }
    if not isinstance(unlike, bool):
        return {"success": False, "error": "unlike must be a boolean", "result": None}

    # 2. Load all context data and select context(s) based on context_id from environment variable
    context_file_path = Path(json.loads(os.environ.get("XIAOHONGSHU_SANDBOX_PATHS"))[0])
    all_context_data = load_context(context_file_path)
    tool_name = "xiaohongshu:like_feed"
    tool_name_lower = tool_name.lower() if tool_name else ""
    is_query_tool = any(
        keyword in tool_name_lower
        for keyword in ["get", "list", "query", "search", "batch"]
    )

    context_id = os.environ.get("XIAOHONGSHU_CONTEXT_ID")
    if context_id is None:
        if is_query_tool:
            context_id = "all"
        else:
            if isinstance(all_context_data, dict) and len(all_context_data) > 0:
                context_id = list(all_context_data.keys())[0]
            else:
                context_id = None

    if context_id == "all":
        if isinstance(all_context_data, dict):
            context_data = list(all_context_data.values())
        elif isinstance(all_context_data, list):
            context_data = all_context_data
        else:
            context_data = [all_context_data]
    elif isinstance(all_context_data, dict):
        if context_id in all_context_data:
            context_data = all_context_data[context_id]
        else:
            return {
                "success": False,
                "error": f"Context ID '{context_id}' not found",
                "result": None,
            }
    elif isinstance(all_context_data, list):
        if context_id is None:
            context_data = all_context_data[0] if len(all_context_data) > 0 else {}
        else:
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
        context_data = all_context_data

    # Prevent modify on multiple contexts
    if not is_query_tool and isinstance(context_data, list):
        return {
            "success": False,
            "error": "Modify operation cannot target multiple contexts. Set XIAOHONGSHU_CONTEXT_ID to a specific xiaohongshu context.",
            "result": None,
        }

    # 3. Validate entity references exist in context (CRITICAL!)
    # Try multiple candidate paths where feeds might be stored
    candidate_feed_paths = ["xiaohongshu.feeds", "feeds", "xiaohongshu.notes"]
    feed_match = None
    selected_feed_path = None
    selected_feed_id_for_path = None  # canonical id value used by path-based functions

    # Try to locate the feed via listing
    for path in candidate_feed_paths:
        try:
            feeds = list_entities_by_path(context_data, path, {}, 10000)
        except Exception:
            feeds = []
        if not isinstance(feeds, list):
            continue
        for f in feeds:
            if not isinstance(f, dict):
                continue
            # Candidate keys that might store the feed's identifier
            id_keys = ["id", "feed_id"]
            matched = False
            for k in id_keys:
                if k in f and str(f.get(k)) == feed_id:
                    selected_feed_id_for_path = str(f.get(k))
                    matched = True
                    break
            if matched:
                feed_match = f
                selected_feed_path = path
                break
        if feed_match:
            break

    # If not found via listing, try direct get_entity_by_path using provided feed_id
    if not feed_match:
        for path in candidate_feed_paths:
            try:
                feed_candidate = get_entity_by_path(context_data, path, feed_id)
            except Exception:
                feed_candidate = None
            if isinstance(feed_candidate, dict):
                feed_match = feed_candidate
                selected_feed_path = path
                # Try to determine canonical id value
                selected_feed_id_for_path = str(
                    feed_match.get("id") or feed_match.get("feed_id") or feed_id
                )
                break

    if not feed_match or not selected_feed_path or not selected_feed_id_for_path:
        return {
            "success": False,
            "error": f"Invalid feed_id: {feed_id}. Feed not found in context.",
            "result": None,
        }

    # Validate xsec_token matches
    stored_token = feed_match.get("xsecToken") or feed_match.get("xsec_token")
    if stored_token is None or stored_token != xsec_token:
        return {
            "success": False,
            "error": "Invalid xsec_token for the specified feed_id",
            "result": None,
        }

    # 4. Perform operation (like/unlike) using path-based functions
    modified = False
    current_liked = bool(feed_match.get("liked", False))
    if unlike:
        # Cancel like if currently liked; else skip
        if current_liked:
            update_ok = update_entity_by_path(
                context_data,
                selected_feed_path,
                selected_feed_id_for_path,
                {"liked": False},
            )
            if not update_ok:
                return {
                    "success": False,
                    "error": "Failed to update feed like status",
                    "result": None,
                }
            modified = True
    else:
        # Like if currently not liked; else skip
        if not current_liked:
            update_ok = update_entity_by_path(
                context_data,
                selected_feed_path,
                selected_feed_id_for_path,
                {"liked": True},
            )
            if not update_ok:
                return {
                    "success": False,
                    "error": "Failed to update feed like status",
                    "result": None,
                }
            modified = True

    # 5. Save context if modified
    if modified:
        context_id_save = os.environ.get("XIAOHONGSHU_CONTEXT_ID")
        if context_id_save is None:
            if isinstance(all_context_data, dict) and len(all_context_data) > 0:
                context_id_save = list(all_context_data.keys())[0]

        if isinstance(all_context_data, dict):
            if (
                context_id_save
                and context_id_save != "all"
                and context_id_save in all_context_data
            ):
                all_context_data[context_id_save] = context_data
        elif isinstance(all_context_data, list):
            if isinstance(context_data, dict):
                user_id = context_data.get("user_id")
                if user_id:
                    for i, ctx in enumerate(all_context_data):
                        if isinstance(ctx, dict) and ctx.get("user_id") == user_id:
                            all_context_data[i] = context_data
                            break
        else:
            all_context_data = context_data

        save_context(context_file_path, all_context_data)

    # 6. Format and return response
    result_payload = {}
    response = {
        "success": True,
        "error": None,
        "result": {
            "meta": None,
            "content": [
                {"type": "text", "text": json.dumps(result_payload, ensure_ascii=False)}
            ],
            "isError": False,
        },
    }
    return response
