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
        return {
            "success": False,
            "error": "Invalid input: parameters_used must be a dict",
            "result": None,
        }
    required_fields = ["user_id", "xsec_token"]
    missing = [f for f in required_fields if f not in parameters_used]
    if missing:
        return {
            "success": False,
            "error": f"Missing required field(s): {', '.join(missing)}",
            "result": None,
        }
    extra_keys = set(parameters_used.keys()) - set(required_fields)
    if extra_keys:
        return {
            "success": False,
            "error": f"Unexpected parameter(s): {', '.join(sorted(extra_keys))}",
            "result": None,
        }
    user_id = parameters_used.get("user_id")
    xsec_token = parameters_used.get("xsec_token")
    if not isinstance(user_id, str) or not user_id.strip():
        return {
            "success": False,
            "error": "Invalid user_id: must be a non-empty string",
            "result": None,
        }
    if not isinstance(xsec_token, str) or not xsec_token.strip():
        return {
            "success": False,
            "error": "Invalid xsec_token: must be a non-empty string",
            "result": None,
        }

    # 2. Load all context data and select context(s) based on context_id from environment variable
    tool_name = "xiaohongshu:user_profile"
    description = (
        "获取指定的小红书用户主页，返回用户基本信息，关注、粉丝、获赞量及其笔记内容"
    )
    context_file_path = Path(json.loads(os.environ.get("XIAOHONGSHU_SANDBOX_PATHS"))[0])
    all_context_data = load_context(
        context_file_path
    )  # Returns Dict[{context_id}: {context_dict}] or List[Dict]

    # Read context_id from environment variable (NOT from input parameters)
    context_id = "all"

    # Determine tool type from tool_name or description to set appropriate default
    tool_name_lower = tool_name.lower() if tool_name else ""
    description_lower = description.lower() if description else ""
    is_query_tool = any(
        keyword in tool_name_lower
        for keyword in ["get", "list", "query", "search", "batch"]
    ) or any(
        keyword in description_lower
        for keyword in [
            "get",
            "list",
            "query",
            "search",
            "batch",
            "获取",
            "查询",
            "查看",
        ]
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

    # 3. Validate entity references exist in context (CRITICAL!)
    def find_user_in_context(single_context, target_user_id):
        # First, check if the context itself represents the user (context_id == user_id)
        if (
            isinstance(single_context, dict)
            and single_context.get("user_id") == target_user_id
        ):
            return single_context, "self"

        # Try primary path "users"
        user_entity = get_entity_by_path(single_context, "users", target_user_id)
        if user_entity:
            return user_entity, "users"
        # Fallback: in case users is keyed dict and get_entity_by_path fails, list and search
        try:
            users_list = list_entities_by_path(single_context, "users", {}, 10000)
            if isinstance(users_list, list):
                for u in users_list:
                    if isinstance(u, dict) and (
                        u.get("user_id") == target_user_id
                        or u.get("id") == target_user_id
                    ):
                        return u, "users"
        except Exception:
            pass
        # Try nested xiaohongshu.users path
        user_entity = get_entity_by_path(
            single_context, "xiaohongshu.users", target_user_id
        )
        if user_entity:
            return user_entity, "xiaohongshu.users"
        try:
            users_list = list_entities_by_path(
                single_context, "xiaohongshu.users", {}, 10000
            )
            if isinstance(users_list, list):
                for u in users_list:
                    if isinstance(u, dict) and (
                        u.get("user_id") == target_user_id
                        or u.get("id") == target_user_id
                    ):
                        return u, "xiaohongshu.users"
        except Exception:
            pass
        return None, None

    selected_context = None
    user_entity = None
    user_path_base = None

    if isinstance(context_data, list):
        for ctx in context_data:
            ue, path_base = find_user_in_context(ctx, user_id)
            if ue:
                user_entity = ue
                user_path_base = path_base
                selected_context = ctx
                break
        if not user_entity:
            return {
                "success": False,
                "error": f"Invalid user_id: {user_id}",
                "result": None,
            }
    elif isinstance(context_data, dict):
        ue, path_base = find_user_in_context(context_data, user_id)
        if not ue:
            return {
                "success": False,
                "error": f"Invalid user_id: {user_id}",
                "result": None,
            }
        user_entity = ue
        user_path_base = path_base
        selected_context = context_data
    else:
        return {
            "success": False,
            "error": "Context data is not in a supported format",
            "result": None,
        }

    # Optional: validate xsec_token if present in context
    context_token = None
    try:
        # Try user-level token
        if isinstance(user_entity, dict) and isinstance(
            user_entity.get("xsec_token"), str
        ):
            context_token = user_entity.get("xsec_token")
        # Try context-level mapping
        if context_token is None and isinstance(selected_context, dict):
            tokens_map = selected_context.get("xsec_tokens") or selected_context.get(
                "xiaohongshu", {}
            ).get("xsec_tokens")
            if isinstance(tokens_map, dict):
                context_token = tokens_map.get(user_id)
    except Exception:
        context_token = None
    if context_token is not None and context_token != xsec_token:
        return {
            "success": False,
            "error": "Invalid xsec_token for the specified user_id",
            "result": None,
        }

    # 4. Perform operation (list/create/update/delete) using path-based functions
    # Retrieve notes
    notes = []
    notes_paths_to_try = []
    if user_path_base:
        notes_paths_to_try.append(f"{user_path_base}[{user_id}].notes")
    notes_paths_to_try.extend(
        [
            f"users[{user_id}].notes",
            f"xiaohongshu.users[{user_id}].notes",
            "notes",  # fallback: global notes then filter by author id
        ]
    )

    fetched_notes = None
    for np in notes_paths_to_try:
        try:
            fetched_notes = list_entities_by_path(selected_context, np, {}, 10000)
            if isinstance(fetched_notes, list) and len(fetched_notes) > 0:
                notes = fetched_notes
                break
            elif isinstance(fetched_notes, list):
                # empty list found at a valid path
                notes = fetched_notes
                break
        except Exception:
            continue

    if not notes and isinstance(selected_context, dict):
        # Fallback: global notes filter by author_id or user_id
        try:
            global_notes = list_entities_by_path(selected_context, "notes", {}, 10000)
            if isinstance(global_notes, list):
                notes = [
                    n
                    for n in global_notes
                    if isinstance(n, dict)
                    and (n.get("author_id") == user_id or n.get("user_id") == user_id)
                ]
        except Exception:
            notes = []

    # Build user profile result
    def pick(fields, source):
        return {k: source.get(k) for k in fields if k in source}

    basic_info_fields = [
        "user_id",
        "id",
        "nickname",
        "name",
        "bio",
        "gender",
        "city",
        "province",
        "country",
        "avatar",
        "avatar_url",
        "verified",
        "verified_reason",
    ]
    stats_fields = [
        "following_count",
        "followings",
        "followers",
        "fans_count",
        "follower_count",
        "likes",
        "liked_count",
        "liked_total",
        "liked_num",
    ]
    basic_info = pick(basic_info_fields, user_entity)
    stats_raw = pick(stats_fields, user_entity)

    # Normalize stats
    stats = {
        "following": stats_raw.get("following_count") or stats_raw.get("followings"),
        "followers": stats_raw.get("followers")
        or stats_raw.get("fans_count")
        or stats_raw.get("follower_count"),
        "likes": stats_raw.get("likes")
        or stats_raw.get("liked_count")
        or stats_raw.get("liked_total")
        or stats_raw.get("liked_num"),
    }

    # 5. Save context if modified - not applicable for query tool, so skip

    # 6. Format and return response
    result_payload = {
        "user_profile": {
            "context_id": (
                None
                if isinstance(all_context_data, list)
                else (context_id if context_id != "all" else None)
            ),
            "user": basic_info,
            "stats": stats,
            "notes": notes,
        }
    }

    return {
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
