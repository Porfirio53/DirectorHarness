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
            "error": "Input must be a JSON object (dict).",
            "result": None,
        }
    feed_id = parameters_used.get("feed_id")
    xsec_token_input = parameters_used.get("xsec_token")
    unfavorite = parameters_used.get("unfavorite", False)

    if feed_id is None:
        return {
            "success": False,
            "error": "Missing required field: feed_id",
            "result": None,
        }
    if xsec_token_input is None:
        return {
            "success": False,
            "error": "Missing required field: xsec_token",
            "result": None,
        }
    if not isinstance(feed_id, str) or feed_id.strip() == "":
        return {
            "success": False,
            "error": "Invalid feed_id: must be a non-empty string",
            "result": None,
        }
    if not isinstance(xsec_token_input, str) or xsec_token_input.strip() == "":
        return {
            "success": False,
            "error": "Invalid xsec_token: must be a non-empty string",
            "result": None,
        }
    if "unfavorite" in parameters_used and not isinstance(unfavorite, bool):
        return {
            "success": False,
            "error": "Invalid unfavorite: must be a boolean",
            "result": None,
        }

    # 2. Load all context data and select context(s) based on context_id from environment variable
    context_file_path = Path(json.loads(os.environ.get("XIAOHONGSHU_SANDBOX_PATHS"))[0])
    all_context_data = load_context(
        context_file_path
    )  # Returns Dict[{context_id}: {context_dict}] or List[Dict]

    # Read context_id from environment variable (NOT from input parameters)
    context_id = os.environ.get("XIAOHONGSHU_CONTEXT_ID")

    # Determine tool type from tool_name or description to set appropriate default
    tool_name = "xiaohongshu:favorite_feed"
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

    # Prevent modifications when multiple contexts are loaded (shouldn't happen for modify tools)
    if isinstance(context_data, list):
        return {
            "success": False,
            "error": "Cannot modify when XIAOHONGSHU_CONTEXT_ID='all'. Set XIAOHONGSHU_CONTEXT_ID to a specific xiaohongshu context.",
            "result": None,
        }

    # 3. Validate entity references exist in context (CRITICAL!)
    # We need to verify the feed exists; attempt via several known paths and fallback to deep search.

    def _entity_matches_id(entity: dict, target_id: str) -> bool:
        if not isinstance(entity, dict):
            return False
        return any(
            str(entity.get(k)) == target_id
            for k in ["id", "feed_id", "note_id", "nid", "noteId", "feedId"]
        )

    def _extract_token_from_entity(entity: dict):
        if not isinstance(entity, dict):
            return None
        for k in [
            "xsecToken",
            "xsec_token",
            "token",
            "xseToken",
            "xsec",
            "xsec_token_v2",
        ]:
            if (
                k in entity
                and isinstance(entity.get(k), str)
                and entity.get(k).strip() != ""
            ):
                return entity.get(k)
        return None

    def _list_entities_by_candidate_paths(ctx: dict):
        candidate_paths = [
            "xiaohongshu.feeds",
            "xiaohongshu.feed_list",
            "xiaohongshu.notes",
            "feeds",
            "feed_list",
            "notes",
            "xiaohongshu.feeds.items",
            "xiaohongshu.feed.items",
        ]
        for p in candidate_paths:
            try:
                ents = list_entities_by_path(ctx, p, {}, 10000)
                if isinstance(ents, list) and ents:
                    yield p, ents
            except Exception:
                # Ignore and try next path
                continue

    def _get_by_candidate_get_path(ctx: dict, target_id: str):
        get_paths = [
            "xiaohongshu.feeds",
            "feeds",
            "xiaohongshu.notes",
            "notes",
        ]
        for p in get_paths:
            try:
                ent = get_entity_by_path(ctx, p, target_id)
                if isinstance(ent, dict) and _entity_matches_id(ent, target_id):
                    return p, ent
            except Exception:
                continue
        return None, None

    def _deep_search_for_feed(ctx, target_id: str):
        # DFS over dict/list to find entity with matching id fields
        stack = [ctx]
        visited = set()
        while stack:
            node = stack.pop()
            node_id = id(node)
            if node_id in visited:
                continue
            visited.add(node_id)
            if isinstance(node, dict):
                if _entity_matches_id(node, target_id):
                    return None, node  # Unknown path, but we found entity
                for v in node.values():
                    if isinstance(v, (dict, list)):
                        stack.append(v)
            elif isinstance(node, list):
                for item in node:
                    if isinstance(item, (dict, list)):
                        stack.append(item)
        return None, None

    # Try using get_entity_by_path first (as per CRITICAL rule)
    found_path, feed_entity = _get_by_candidate_get_path(context_data, feed_id)

    # If not found, try listing candidate paths and searching within
    if feed_entity is None:
        for p, ents in _list_entities_by_candidate_paths(context_data):
            for e in ents:
                if isinstance(e, dict) and _entity_matches_id(e, feed_id):
                    found_path, feed_entity = p, e
                    break
            if feed_entity is not None:
                break

    # If still not found, fallback to deep search
    if feed_entity is None:
        _, feed_entity = _deep_search_for_feed(context_data, feed_id)

    if feed_entity is None:
        return {
            "success": False,
            "error": f"Invalid feed_id: {feed_id}",
            "result": None,
        }

    # Optional token validation if present on entity
    entity_token = _extract_token_from_entity(feed_entity)
    if entity_token is not None and entity_token != xsec_token_input:
        return {
            "success": False,
            "error": "Invalid xsec_token for the provided feed_id",
            "result": None,
        }

    # 4. Perform operation (favorite/unfavorite) using context_data
    # Ensure nested structures exist
    if not isinstance(context_data, dict):
        return {
            "success": False,
            "error": "Context data format is invalid.",
            "result": None,
        }
    if "xiaohongshu" not in context_data or not isinstance(
        context_data.get("xiaohongshu"), dict
    ):
        context_data["xiaohongshu"] = {}

    xhs_section = context_data["xiaohongshu"]
    if "favorite_feeds" not in xhs_section or not isinstance(
        xhs_section.get("favorite_feeds"), list
    ):
        xhs_section["favorite_feeds"] = []

    favorites = xhs_section["favorite_feeds"]

    # Normalize favorites entries to list of dicts with feed_id key
    normalized = []
    changed = False
    for item in favorites:
        if (
            isinstance(item, dict)
            and "feed_id" in item
            and isinstance(item["feed_id"], str)
        ):
            normalized.append(item)
        elif isinstance(item, str):
            normalized.append({"feed_id": item})
            changed = True
    if changed:
        xhs_section["favorite_feeds"] = normalized
        favorites = xhs_section["favorite_feeds"]

    # Determine current existence
    exists_idx = None
    for idx, item in enumerate(favorites):
        if isinstance(item, dict) and item.get("feed_id") == feed_id:
            exists_idx = idx
            break

    if unfavorite:
        # Remove if exists; else skip
        if exists_idx is not None:
            del favorites[exists_idx]
    else:
        # Add if not exists; else skip
        if exists_idx is None:
            favorites.append({"feed_id": feed_id})

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
    result_obj = {}
    wrapped = {
        "meta": None,
        "content": [
            {
                "type": "text",
                "text": json.dumps(result_obj, ensure_ascii=False),
                "isError": False,
                "title": "",
            }
        ],
        "isError": False,
    }
    return {"success": True, "error": None, "result": wrapped}
