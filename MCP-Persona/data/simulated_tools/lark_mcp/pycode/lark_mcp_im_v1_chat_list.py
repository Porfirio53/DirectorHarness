from pathlib import Path
import json
import os
from lark_mcp.dynamic_context_handler import (
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
    params = parameters_used.get("params", {})
    use_uat = parameters_used.get("useUAT", None)

    if params is not None and not isinstance(params, dict):
        return {
            "success": False,
            "error": "Invalid input: 'params' must be an object",
            "result": None,
        }

    # Validate useUAT
    if use_uat is not None and not isinstance(use_uat, bool):
        return {
            "success": False,
            "error": "Invalid input: 'useUAT' must be a boolean",
            "result": None,
        }

    # Validate params.user_id_type
    user_id_type = params.get("user_id_type")
    if user_id_type is not None:
        if not isinstance(user_id_type, str):
            return {
                "success": False,
                "error": "Invalid input: 'user_id_type' must be a string",
                "result": None,
            }
        if user_id_type not in ["open_id", "union_id", "user_id"]:
            return {
                "success": False,
                "error": "Invalid input: 'user_id_type' must be one of ['open_id','union_id','user_id']",
                "result": None,
            }

    # Validate params.sort_type
    sort_type = params.get("sort_type")
    if sort_type is not None:
        if not isinstance(sort_type, str):
            return {
                "success": False,
                "error": "Invalid input: 'sort_type' must be a string",
                "result": None,
            }
        if sort_type not in ["ByCreateTimeAsc", "ByActiveTimeDesc"]:
            return {
                "success": False,
                "error": "Invalid input: 'sort_type' must be one of ['ByCreateTimeAsc','ByActiveTimeDesc']",
                "result": None,
            }

    # Validate params.page_token
    page_token = params.get("page_token")
    if page_token is not None and not isinstance(page_token, str):
        return {
            "success": False,
            "error": "Invalid input: 'page_token' must be a string",
            "result": None,
        }

    # Validate params.page_size
    page_size = params.get("page_size")
    if page_size is not None:
        if not isinstance(page_size, (int, float)):
            return {
                "success": False,
                "error": "Invalid input: 'page_size' must be a number",
                "result": None,
            }
        if page_size <= 0:
            return {
                "success": False,
                "error": "Invalid input: 'page_size' must be positive",
                "result": None,
            }

    # Defaults
    if sort_type is None:
        sort_type = "ByActiveTimeDesc"
    if page_size is None:
        page_size = 50
    try:
        page_size_int = int(page_size)
    except Exception:
        return {
            "success": False,
            "error": "Invalid input: 'page_size' must be convertible to integer",
            "result": None,
        }
    if page_size_int <= 0:
        return {
            "success": False,
            "error": "Invalid input: 'page_size' must be a positive integer",
            "result": None,
        }

    # 2. Load all context data and select context(s) based on context_id from environment variable
    context_file_path = Path(json.loads(os.environ.get("LARK_MCP_SANDBOX_PATHS"))[0])
    all_context_data = load_context(
        context_file_path
    )  # Returns Dict[{context_id}: {context_dict}] or List[Dict]

    # Read context_id from environment variable (NOT from input parameters)
    context_id = os.environ.get("LARK_MCP_CONTEXT_ID")

    # Determine tool type from tool_name or description to set appropriate default
    tool_name = "lark-mcp:im_v1_chat_list"
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
            found_ctx = None
            for ctx in all_context_data:
                if isinstance(ctx, dict) and ctx.get("user_id") == context_id:
                    found_ctx = ctx
                    break
            if found_ctx is None:
                return {
                    "success": False,
                    "error": f"Context ID '{context_id}' not found",
                    "result": None,
                }
            context_data = found_ctx
    else:
        # Single context dict
        context_data = all_context_data

    # Normalize contexts to a list for processing
    if isinstance(context_data, list):
        contexts_list = [ctx for ctx in context_data if isinstance(ctx, dict)]
    elif isinstance(context_data, dict):
        contexts_list = [context_data]
    else:
        contexts_list = []

    # 3. Validate entity references exist in context (CRITICAL!)
    # For list operation, ensure identity reference can be determined for membership filtering.
    # If a single context is selected and user_id_type is provided but missing in context, return error.
    if context_id != "all":
        single_ctx = contexts_list[0] if contexts_list else {}
        if user_id_type is not None:
            if (
                single_ctx is None
                or not isinstance(single_ctx, dict)
                or single_ctx.get(user_id_type) in [None, ""]
            ):
                return {
                    "success": False,
                    "error": f"Context missing required identity field '{user_id_type}'",
                    "result": None,
                }

    # 4. Perform operation (list) using path-based functions
    def extract_identity_map(ctx):
        if not isinstance(ctx, dict):
            return {}
        id_map = {}
        for key in ["user_id", "open_id", "union_id", "bot_id"]:
            val = ctx.get(key)
            if isinstance(val, str) and val.strip() != "":
                id_map[key] = val
        return id_map

    def is_member_match(chat_obj, id_map, preferred_type=None):
        # Check chat members list to see if any matches identity map
        if not isinstance(chat_obj, dict):
            return False
        members = chat_obj.get("members")
        # Also consider alternate fields
        if members is None and isinstance(chat_obj.get("member_list"), list):
            members = chat_obj.get("member_list")
        if not isinstance(members, list):
            # Fallback: check owner or creator matches identity
            for k in ["owner_id", "creator_id", "admin_id", "bot_id"]:
                v = chat_obj.get(k)
                if v and isinstance(v, str):
                    # Try matching any identity value
                    if preferred_type:
                        if (
                            id_map.get(preferred_type)
                            and id_map.get(preferred_type) == v
                        ):
                            return True
                    else:
                        if v in id_map.values():
                            return True
            return False

        # Iterate members
        for m in members:
            if not isinstance(m, dict):
                continue
            # Preferred matching
            if preferred_type:
                mv = m.get(preferred_type)
                if (
                    mv
                    and id_map.get(preferred_type)
                    and mv == id_map.get(preferred_type)
                ):
                    return True
            else:
                for k, v in id_map.items():
                    mv = m.get(k)
                    if mv and mv == v:
                        return True
                # Also allow member_id match to user_id
                if (
                    m.get("member_id")
                    and id_map.get("user_id")
                    and m.get("member_id") == id_map.get("user_id")
                ):
                    return True
        return False

    def sort_key_create_asc(chat_obj):
        # Prefer common fields
        for key in ["create_time", "createTime", "created_at", "createdAt"]:
            val = chat_obj.get(key)
            if val is not None:
                return str(val)
        return ""

    def sort_key_active_desc(chat_obj):
        for key in [
            "active_time",
            "last_active_time",
            "update_time",
            "updated_at",
            "last_message_time",
        ]:
            val = chat_obj.get(key)
            if val is not None:
                return str(val)
        return ""

    # Aggregate chats across contexts
    aggregated_by_id = {}
    aggregated_list = []
    for ctx in contexts_list:
        # Extract identity map for this context
        identity_map = extract_identity_map(ctx)
        # For single context with user_id_type specified, enforce presence
        if context_id != "all" and user_id_type is not None:
            if identity_map.get(user_id_type) is None:
                return {
                    "success": False,
                    "error": f"Context missing required identity field '{user_id_type}'",
                    "result": None,
                }

        chats = list_entities_by_path(ctx, "chats", {}, 10000) or []
        if not isinstance(chats, list):
            chats = []

        for chat in chats:
            # Validate chat structure minimally
            if not isinstance(chat, dict):
                continue
            # Membership validation
            if user_id_type:
                match = is_member_match(chat, identity_map, preferred_type=user_id_type)
            else:
                match = is_member_match(chat, identity_map, preferred_type=None)
            if not match:
                continue

            # Deduplicate by chat_id (preferred) or id or name
            chat_id = None
            for key in ["chat_id", "id", "chatId"]:
                cid = chat.get(key)
                if isinstance(cid, (str, int)) and str(cid).strip() != "":
                    chat_id = str(cid)
                    break
            if chat_id is None:
                # derive synthetic id from name to avoid duplicates
                name = chat.get("name") or chat.get("chat_name") or ""
                chat_id = f"unknown:{name}"

            # If already added, skip or consider merging
            if chat_id in aggregated_by_id:
                continue
            aggregated_by_id[chat_id] = chat
            aggregated_list.append(chat)

    # Apply sorting
    if sort_type == "ByCreateTimeAsc":
        aggregated_list.sort(key=sort_key_create_asc, reverse=False)
    elif sort_type == "ByActiveTimeDesc":
        aggregated_list.sort(key=sort_key_active_desc, reverse=True)

    # Pagination
    start_offset = 0
    if page_token:
        # Expected formats: "offset:<int>" or "<int>"
        token_str = page_token.strip()
        try:
            if token_str.lower().startswith("offset:"):
                start_offset = int(token_str.split(":", 1)[1].strip())
            else:
                start_offset = int(token_str)
            if start_offset < 0:
                return {
                    "success": False,
                    "error": "Invalid page_token: negative offset",
                    "result": None,
                }
        except Exception:
            return {
                "success": False,
                "error": "Invalid page_token: must be parseable integer or 'offset:<int>'",
                "result": None,
            }

    total = len(aggregated_list)
    end_offset = min(start_offset + page_size_int, total)
    paged_items = aggregated_list[start_offset:end_offset]
    has_more = end_offset < total
    next_page_token = str(end_offset) if has_more else None

    # Prepare result object similar to Lark chat list schema
    result_payload = {
        "items": paged_items,
        "has_more": has_more,
        "page_token": next_page_token,
        "page_size": page_size_int,
        "total": total,
    }

    # 5. Save context if modified - Not applicable for list operation (no modifications)

    # 6. Format and return response
    return {
        "success": True,
        "error": None,
        "result": {
            "meta": None,
            "content": [
                {
                    "type": "text",
                    "text": json.dumps(result_payload, ensure_ascii=False),
                }
            ],
            "isError": False,
        },
    }
