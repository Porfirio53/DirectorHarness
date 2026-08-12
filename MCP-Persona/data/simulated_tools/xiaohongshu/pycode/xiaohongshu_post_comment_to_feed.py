from pathlib import Path
import os
import json
import uuid
from datetime import datetime, timezone
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
        return {
            "success": False,
            "error": "Invalid input: parameters_used must be a dictionary",
            "result": None,
        }
    required_fields = ["feed_id", "xsec_token", "content"]
    missing = [f for f in required_fields if f not in parameters_used]
    if missing:
        return {
            "success": False,
            "error": f"Missing required fields: {', '.join(missing)}",
            "result": None,
        }
    feed_id = parameters_used.get("feed_id")
    xsec_token = parameters_used.get("xsec_token")
    content = parameters_used.get("content")

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
    if not isinstance(content, str) or not content.strip():
        return {
            "success": False,
            "error": "content must be a non-empty string",
            "result": None,
        }

    feed_id = feed_id.strip()
    xsec_token = xsec_token.strip()
    content = content.strip()

    # 2. Load all context data and select context(s) based on context_id from environment variable
    context_file_path = Path(json.loads(os.environ.get("XIAOHONGSHU_SANDBOX_PATHS"))[0])
    all_context_data = load_context(
        context_file_path
    )  # Returns Dict[{context_id}: {context_dict}] or List[Dict]

    # Read context_id from environment variable (NOT from input parameters)
    context_id = os.environ.get("XIAOHONGSHU_CONTEXT_ID")

    # Determine tool type from tool_name or description to set appropriate default
    tool_name = "xiaohongshu:post_comment_to_feed"
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
        # For a modifying tool, do not allow "all"
        return {
            "success": False,
            "error": "Context ID cannot be 'all' for modifying tools. Please set XIAOHONGSHU_CONTEXT_ID to a specific xiaohongshu context.",
            "result": None,
        }
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
            found = False
            for ctx in all_context_data:
                if isinstance(ctx, dict) and ctx.get("user_id") == context_id:
                    context_data = ctx
                    found = True
                    break
            if not found:
                return {
                    "success": False,
                    "error": f"Context ID '{context_id}' not found",
                    "result": None,
                }
    else:
        # Single context dict
        context_data = all_context_data

    # 3. Validate entity references exist in context (CRITICAL!)
    # We expect feeds to be under one of the following paths
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
            id_keys = ["id", "feed_id", "note_id"]
            matched = False
            for k in id_keys:
                if k in f and str(f.get(k)) == feed_id:
                    selected_feed_id_for_path = str(f.get(k))
                    matched = True
                    break
            if not matched:
                # Also allow matching with implicit id if no key matched
                implicit_id = f.get("id") or f.get("feed_id") or f.get("note_id")
                if implicit_id is not None and str(implicit_id) == feed_id:
                    selected_feed_id_for_path = str(implicit_id)
                    matched = True
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
                    feed_candidate.get("id") or feed_candidate.get("feed_id") or feed_id
                )
                break

    if not feed_match or not selected_feed_path or not selected_feed_id_for_path:
        return {
            "success": False,
            "error": f"Invalid feed_id: {feed_id}. Feed not found in context.",
            "result": None,
        }

    # Validate xsec_token matches expected token in feed entity
    token_keys = ["xsecToken", "xsec_token", "token"]
    feed_token = None
    for tk in token_keys:
        if tk in feed_match and isinstance(feed_match.get(tk), (str, int)):
            feed_token = str(feed_match.get(tk))
            break
    if feed_token is None:
        return {
            "success": False,
            "error": "Feed does not contain an xsec token for validation",
            "result": None,
        }
    if str(feed_token) != xsec_token:
        return {
            "success": False,
            "error": "Invalid xsec_token: token does not match the feed's token",
            "result": None,
        }

    # 4. Perform operation (create) using path-based functions
    # Generate 24-character hex ID (matching context.json format)
    comment_id = uuid.uuid4().hex[:24]
    timestamp_ms = int(datetime.now(timezone.utc).timestamp() * 1000)

    # Get user info from current context
    user_id = context_data.get("user_id", "")
    nickname = context_data.get("nickname", "")
    nick_name = context_data.get("nickName", "")
    avatar = context_data.get("avatar", "")
    ip_location = context_data.get("ipLocation", "")

    # Construct comment entity matching context.json format
    comment_data = {
        "id": comment_id,
        "noteId": feed_id,
        "content": content,
        "ipLocation": ip_location,
        "likeCount": "0",
        "createTime": timestamp_ms,
        "liked": False,
        "userInfo": {
            "userId": user_id,
            "nickname": nickname,
            "nickName": nick_name,
            "avatar": avatar,
        },
        "subCommentCount": "0",
        "subComments": {},
        "showTags": [],
    }

    comments_path = f"{selected_feed_path}[{selected_feed_id_for_path}].comments"
    try:
        created_comment = create_entity_by_path(
            context_data, comments_path, comment_data, comment_id
        )
        # Some handlers may return None; if so, assume created_comment is comment_data
        if not created_comment:
            created_comment = comment_data
    except Exception as e:
        return {
            "success": False,
            "error": f"Failed to create comment: {e}",
            "result": None,
        }

    # 5. Save context if modified
    # Get context_id from environment variable (same as when loading)
    context_id_save = os.environ.get("XIAOHONGSHU_CONTEXT_ID")
    if context_id_save is None:
        # Use first key if dict, or first element if list
        if isinstance(all_context_data, dict) and len(all_context_data) > 0:
            context_id_save = list(all_context_data.keys())[0]

    # Update the modified context back to all_context_data
    if isinstance(all_context_data, dict):
        # Dict format: {context_id: context_dict, ...}
        if (
            context_id_save
            and context_id_save != "all"
            and context_id_save in all_context_data
        ):
            all_context_data[context_id_save] = (
                context_data  # Update the specific context
            )
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
    result_payload = {"feed_id": feed_id, "comment": created_comment}
    response = {
        "meta": None,
        "content": [
            {"type": "text", "text": json.dumps(result_payload, ensure_ascii=False)}
        ],
        "isError": False,
    }
    return {"success": True, "error": None, "result": response}
