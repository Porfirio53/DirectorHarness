from pathlib import Path
import os
import json
from datetime import datetime, timedelta
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
            "error": "Input must be a JSON object",
            "result": None,
        }
    allowed_root_keys = {"keyword", "filters"}
    extra_keys = set(parameters_used.keys()) - allowed_root_keys
    if extra_keys:
        return {
            "success": False,
            "error": f"Unknown parameter(s): {', '.join(sorted(extra_keys))}",
            "result": None,
        }
    if "keyword" not in parameters_used:
        return {
            "success": False,
            "error": "Missing required field: keyword",
            "result": None,
        }
    keyword = parameters_used.get("keyword")
    if not isinstance(keyword, str):
        return {
            "success": False,
            "error": "Field 'keyword' must be a string",
            "result": None,
        }
    keyword = keyword.strip()
    if keyword == "":
        return {
            "success": False,
            "error": "Field 'keyword' cannot be empty",
            "result": None,
        }

    filters = parameters_used.get("filters", {})
    if filters is None:
        filters = {}
    if not isinstance(filters, dict):
        return {
            "success": False,
            "error": "Field 'filters' must be an object",
            "result": None,
        }
    allowed_filter_keys = {
        "location",
        "note_type",
        "publish_time",
        "search_scope",
        "sort_by",
    }
    extra_filter_keys = set(filters.keys()) - allowed_filter_keys
    if extra_filter_keys:
        return {
            "success": False,
            "error": f"Unknown filter(s): {', '.join(sorted(extra_filter_keys))}",
            "result": None,
        }

    # Validate filter values
    def _validate_str_field(name, val):
        if val is None:
            return None
        if not isinstance(val, str):
            return {
                "success": False,
                "error": f"Filter '{name}' must be a string",
                "result": None,
            }
        return None

    for k in filters:
        err = _validate_str_field(k, filters.get(k))
        if err:
            return err

    allowed_location = {"不限", "同城", "附近"}
    allowed_note_type = {"不限", "视频", "图文"}
    allowed_publish_time = {"不限", "一天内", "一周内", "半年内"}
    allowed_search_scope = {"不限", "已看过", "未看过", "已关注"}
    allowed_sort_by = {"综合", "最新", "最多点赞", "最多评论", "最多收藏"}

    location = filters.get("location", "不限")
    note_type = filters.get("note_type", "不限")
    publish_time = filters.get("publish_time", "不限")
    search_scope = filters.get("search_scope", "不限")
    sort_by = filters.get("sort_by", "综合")

    if location not in allowed_location:
        return {
            "success": False,
            "error": f"Invalid filters.location: {location}. Allowed: {', '.join(sorted(allowed_location))}",
            "result": None,
        }
    if note_type not in allowed_note_type:
        return {
            "success": False,
            "error": f"Invalid filters.note_type: {note_type}. Allowed: {', '.join(sorted(allowed_note_type))}",
            "result": None,
        }
    if publish_time not in allowed_publish_time:
        return {
            "success": False,
            "error": f"Invalid filters.publish_time: {publish_time}. Allowed: {', '.join(sorted(allowed_publish_time))}",
            "result": None,
        }
    if search_scope not in allowed_search_scope:
        return {
            "success": False,
            "error": f"Invalid filters.search_scope: {search_scope}. Allowed: {', '.join(sorted(allowed_search_scope))}",
            "result": None,
        }
    if sort_by not in allowed_sort_by:
        return {
            "success": False,
            "error": f"Invalid filters.sort_by: {sort_by}. Allowed: {', '.join(sorted(allowed_sort_by))}",
            "result": None,
        }

    # 2. Load all context data and select context(s) based on context_id from environment variable
    tool_name = "xiaohongshu:search_feeds"
    tool_name_lower = tool_name.lower() if tool_name else ""
    context_file_path = Path(json.loads(os.environ.get("XIAOHONGSHU_SANDBOX_PATHS"))[0])
    all_context_data = load_context(context_file_path)

    # Read context_id from environment variable (NOT from input parameters)
    context_id = os.environ.get("XIAOHONGSHU_CONTEXT_ID")

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
                "error": f"Context ID '{{context_id}}' not found",
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
                    "error": f"Context ID '{{context_id}}' not found",
                    "result": None,
                }
    else:
        # Single context dict
        context_data = all_context_data

    # 3. Validate entity references exist in context (CRITICAL!)
    # Search can be performed without login requirement
    # All contexts are valid for searching

    # 4. Perform operation (list/create/update/delete) using path-based functions
    # We'll list possible feed entities from multiple candidate paths and combine them.
    candidate_paths = ["xiaohongshu.feeds", "xiaohongshu.notes", "feeds", "notes"]

    def _normalize_feed_fields(feed):
        """
        Normalize feed field names to match search expectations.

        Maps context.json dot-notation fields (e.g., 'noteCard.desc')
        to simple field names (e.g., 'desc') used in search logic.
        """
        normalized = dict(feed)

        # Map description fields
        if "noteCard.desc" in feed:
            normalized["desc"] = feed["noteCard.desc"]
        if "noteCard.displayTitle" in feed:
            normalized["title"] = feed["noteCard.displayTitle"]

        # Map interaction counts
        if "noteCard.interactInfo.likedCount" in feed:
            normalized["likes"] = feed["noteCard.interactInfo.likedCount"]
        if "noteCard.interactInfo.commentCount" in feed:
            normalized["comments"] = feed["noteCard.interactInfo.commentCount"]
        if "noteCard.interactInfo.collectedCount" in feed:
            normalized["bookmarks"] = feed["noteCard.interactInfo.collectedCount"]

        # Map time fields
        if "time" in feed:
            normalized["publish_time"] = feed["time"]

        # Add aliases for content
        if "desc" in normalized:
            normalized["content"] = normalized["desc"]
            normalized["text"] = normalized["desc"]

        return normalized

    def get_all_feeds_from_context(ctx):
        all_items = []
        for p in candidate_paths:
            try:
                items = list_entities_by_path(ctx, p, {}, 10000) or []
                if isinstance(items, list):
                    # Apply field normalization to each item
                    normalized_items = [_normalize_feed_fields(item) for item in items]
                    all_items.extend(normalized_items)
            except Exception:
                # If list_entities_by_path fails for a path, ignore and continue
                continue
        # Deduplicate by id if present
        seen_ids = set()
        deduped = []
        for it in all_items:
            if not isinstance(it, dict):
                continue
            iid = it.get("id") or it.get("note_id") or it.get("feed_id")
            if iid:
                if iid in seen_ids:
                    continue
                seen_ids.add(iid)
            deduped.append(it)
        return deduped

    # Collect feeds from all contexts
    combined_feeds = []
    contexts = context_data if isinstance(context_data, list) else [context_data]
    for ctx in contexts:
        combined_feeds.extend(get_all_feeds_from_context(ctx))

    # Apply keyword filtering and auxiliary filters
    kw_lower = keyword.lower()

    def contains_kw(val):
        if val is None:
            return False
        if isinstance(val, str):
            return kw_lower in val.lower()
        if isinstance(val, list):
            for v in val:
                if isinstance(v, str) and kw_lower in v.lower():
                    return True
        return False

    def parse_dt(dt_val):
        # Try multiple common formats; return datetime or None
        if dt_val is None:
            return None
        if isinstance(dt_val, (int, float)):
            try:
                return datetime.fromtimestamp(float(dt_val))
            except Exception:
                return None
        if isinstance(dt_val, str):
            s = dt_val.strip()
            # ISO format
            for fmt in (
                "%Y-%m-%dT%H:%M:%S.%fZ",
                "%Y-%m-%dT%H:%M:%S%z",
                "%Y-%m-%dT%H:%M:%S",
                "%Y-%m-%d %H:%M:%S",
                "%Y-%m-%d",
            ):
                try:
                    return datetime.strptime(s, fmt)
                except Exception:
                    pass
            try:
                return datetime.fromisoformat(s)
            except Exception:
                pass
        return None

    def within_publish_window(item_dt, window):
        if item_dt is None:
            return False if window != "不限" else True
        now = datetime.utcnow()
        if window == "不限":
            return True
        if window == "一天内":
            return (now - item_dt) <= timedelta(days=1)
        if window == "一周内":
            return (now - item_dt) <= timedelta(weeks=1)
        if window == "半年内":
            return (now - item_dt) <= timedelta(days=182)
        return True

    def matches_location(item):
        if location == "不限":
            return True
        scope = (
            item.get("location_scope")
            or item.get("scope")
            or item.get("location_range")
        )
        if isinstance(scope, str):
            return scope == location
        # Fallback: check a distance or proximity hint
        if location == "附近":
            dist = item.get("distance_km")
            if isinstance(dist, (int, float)):
                return dist <= 5
        if location == "同城":
            # Compare item city to user's city if available
            item_city = item.get("city")
            user_city = item.get("user_city") or item.get("viewer_city")
            if isinstance(item_city, str) and isinstance(user_city, str):
                return item_city == user_city
        return False

    def matches_note_type(item):
        if note_type == "不限":
            return True
        typ = item.get("note_type") or item.get("type") or item.get("format")
        if isinstance(typ, str):
            return typ == note_type
        return False

    def matches_search_scope(item):
        if search_scope == "不限":
            return True
        if search_scope == "已看过":
            return bool(item.get("viewed") is True or item.get("seen") is True)
        if search_scope == "未看过":
            return not bool(item.get("viewed") is True or item.get("seen") is True)
        if search_scope == "已关注":
            return bool(
                item.get("author_followed") is True
                or item.get("followed_author") is True
            )
        return True

    def keyword_match(item):
        fields = [
            ("title", 3),
            ("content", 2),
            ("desc", 2),
            ("description", 2),
            ("text", 2),
            ("tags", 2),
            ("author", 1),
            ("location", 1),
        ]
        score = 0
        matched = False
        for f, w in fields:
            val = item.get(f)
            if contains_kw(val):
                score += w
                matched = True
        return matched, score

    def relevance_score(item, base_score):
        likes = (
            item.get("likes") or item.get("like_count") or item.get("digg_count") or 0
        )
        comments = item.get("comments") or item.get("comment_count") or 0
        bookmarks = item.get("bookmarks") or item.get("favorite_count") or 0
        try:
            likes = float(likes)
        except Exception:
            likes = 0.0
        try:
            comments = float(comments)
        except Exception:
            comments = 0.0
        try:
            bookmarks = float(bookmarks)
        except Exception:
            bookmarks = 0.0
        return base_score + (likes * 0.001) + (comments * 0.002) + (bookmarks * 0.002)

    filtered = []
    for item in combined_feeds:
        if not isinstance(item, dict):
            continue
        matched, base_kw_score = keyword_match(item)
        if not matched:
            continue
        # publish time evaluation
        dt = parse_dt(
            item.get("publish_time") or item.get("created_at") or item.get("timestamp")
        )
        if not within_publish_window(dt, publish_time):
            continue
        if not matches_location(item):
            continue
        if not matches_note_type(item):
            continue
        if not matches_search_scope(item):
            continue
        # Keep computed base_kw_score for sorting
        item_copy = dict(item)
        item_copy["_kw_score"] = base_kw_score
        item_copy["_publish_dt"] = dt
        filtered.append(item_copy)

    # Sorting
    def sort_key_comprehensive(it):
        return relevance_score(it, it.get("_kw_score", 0.0))

    if sort_by == "综合":
        filtered.sort(key=sort_key_comprehensive, reverse=True)
    elif sort_by == "最新":
        filtered.sort(
            key=lambda it: it.get("_publish_dt") or datetime.fromtimestamp(0),
            reverse=True,
        )
    elif sort_by == "最多点赞":
        filtered.sort(
            key=lambda it: float(
                it.get("likes") or it.get("like_count") or it.get("digg_count") or 0
            ),
            reverse=True,
        )
    elif sort_by == "最多评论":
        filtered.sort(
            key=lambda it: float(it.get("comments") or it.get("comment_count") or 0),
            reverse=True,
        )
    elif sort_by == "最多收藏":
        filtered.sort(
            key=lambda it: float(it.get("bookmarks") or it.get("favorite_count") or 0),
            reverse=True,
        )

    # Clean helper fields from output
    for it in filtered:
        if "_kw_score" in it:
            del it["_kw_score"]
        if "_publish_dt" in it:
            del it["_publish_dt"]

    # Build output format
    result_payload = {
        "search_results": filtered,
        "count": len(filtered),
        "keyword": keyword,
        "applied_filters": {
            "location": location,
            "note_type": note_type,
            "publish_time": publish_time,
            "search_scope": search_scope,
            "sort_by": sort_by,
        },
    }

    # 6. Format and return response
    response = {
        "meta": None,
        "content": [
            {"type": "text", "text": json.dumps(result_payload, ensure_ascii=False)}
        ],
        "isError": False,
    }
    return {"success": True, "error": None, "result": response}
