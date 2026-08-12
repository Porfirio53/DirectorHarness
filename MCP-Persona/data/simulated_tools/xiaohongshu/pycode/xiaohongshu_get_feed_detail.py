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
    tool_name = "xiaohongshu:get_feed_detail"
    if parameters_used is None or not isinstance(parameters_used, dict):
        return {
            "success": False,
            "error": "Invalid input: parameters_used must be a dictionary",
            "result": None,
        }

    # Required fields for this tool
    required_fields = ["feed_id", "xsec_token"]
    for field in required_fields:
        if field not in parameters_used:
            return {
                "success": False,
                "error": f"Missing required field: {field}",
                "result": None,
            }

    feed_id = parameters_used.get("feed_id")
    xsec_token = parameters_used.get("xsec_token")

    if not isinstance(feed_id, str) or not feed_id.strip():
        return {
            "success": False,
            "error": "Invalid feed_id: must be a non-empty string",
            "result": None,
        }

    if not isinstance(xsec_token, str) or not xsec_token.strip():
        return {
            "success": False,
            "error": "Invalid xsec_token: must be a non-empty string",
            "result": None,
        }

    # 2. Load all context data and select context(s) based on context_id from environment variable
    context_file_path = Path(json.loads(os.environ.get("XIAOHONGSHU_SANDBOX_PATHS"))[0])
    try:
        all_context_data = load_context(context_file_path)
    except Exception as e:
        return {
            "success": False,
            "error": f"Failed to load context: {e}",
            "result": None,
        }

    # Read context_id from environment variable (NOT from input parameters)
    context_id = os.environ.get("XIAOHONGSHU_CONTEXT_ID")

    # Determine tool type from tool_name or description to set appropriate default
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

    # Resolve context_data based on the determined context_id
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

    # Helper: safely list entities by path
    def safe_list_entities(ctx, path):
        try:
            return list_entities_by_path(ctx, path, {}, 10000)
        except Exception:
            return []

    # Helper: recursively search for feed by id in arbitrary nested structures
    def recursive_search_for_feed(obj, target_feed_id):
        found_items = []

        def matches_feed(candidate):
            if not isinstance(candidate, dict):
                return False
            cid = (
                candidate.get("feed_id")
                or candidate.get("id")
                or candidate.get("note_id")
                or candidate.get("nid")
            )
            if cid != target_feed_id:
                return False
            # Likely a XHS feed if hints exist, but accept any matching id
            return True

        def walk(o):
            if isinstance(o, dict):
                if matches_feed(o):
                    found_items.append(o)
                for v in o.values():
                    walk(v)
            elif isinstance(o, list):
                for it in o:
                    walk(it)

        walk(obj)
        return found_items

    # Helper: normalize interactions
    def extract_interactions(feed_obj):
        interactions = {"likes": 0, "favorites": 0, "shares": 0}
        # Try common fields
        # Direct fields
        if isinstance(feed_obj, dict):
            for k in ["likes", "liked_count", "like_count"]:
                if isinstance(feed_obj.get(k), int):
                    interactions["likes"] = feed_obj.get(k)
                    break
            for k in ["favorites", "fav_count", "collect_count", "collected_count"]:
                if isinstance(feed_obj.get(k), int):
                    interactions["favorites"] = feed_obj.get(k)
                    break
            for k in ["shares", "share_count", "repost_count"]:
                if isinstance(feed_obj.get(k), int):
                    interactions["shares"] = feed_obj.get(k)
                    break

            # Stats nested
            stats = feed_obj.get("stats") or feed_obj.get("stat") or {}
            if isinstance(stats, dict):
                for k in ["likes", "liked_count", "like_count"]:
                    if isinstance(stats.get(k), int):
                        interactions["likes"] = stats.get(k)
                        break
                for k in ["favorites", "fav_count", "collect_count", "collected_count"]:
                    if isinstance(stats.get(k), int):
                        interactions["favorites"] = stats.get(k)
                        break
                for k in ["shares", "share_count", "repost_count"]:
                    if isinstance(stats.get(k), int):
                        interactions["shares"] = stats.get(k)
                        break
        return interactions

    # Helper: normalize author
    def extract_author(feed_obj):
        author = {}
        if not isinstance(feed_obj, dict):
            return author
        author_obj = (
            feed_obj.get("author")
            or feed_obj.get("user")
            or feed_obj.get("creator")
            or {}
        )
        if isinstance(author_obj, dict):
            author = {
                "user_id": author_obj.get("user_id")
                or author_obj.get("id")
                or author_obj.get("uid"),
                "nickname": author_obj.get("nickname")
                or author_obj.get("name")
                or author_obj.get("username"),
                "avatar": author_obj.get("avatar")
                or author_obj.get("avatar_url")
                or author_obj.get("avatarUrl"),
                "verified": (
                    author_obj.get("verified")
                    if isinstance(author_obj.get("verified"), bool)
                    else bool(
                        author_obj.get("is_verified") or author_obj.get("isVerified")
                    )
                ),
            }
        return author

    # Helper: normalize images
    def extract_images(feed_obj):
        images = []
        if not isinstance(feed_obj, dict):
            return images

        # Common patterns
        media = (
            feed_obj.get("images")
            or feed_obj.get("image_list")
            or feed_obj.get("imgs")
            or feed_obj.get("media")
            or []
        )
        if isinstance(media, list):
            for m in media:
                if isinstance(m, str):
                    images.append({"url": m})
                elif isinstance(m, dict):
                    url = (
                        m.get("url")
                        or m.get("src")
                        or m.get("image_url")
                        or m.get("imageUrl")
                    )
                    if url:
                        images.append(
                            {
                                "url": url,
                                "width": m.get("width"),
                                "height": m.get("height"),
                            }
                        )
        # Sometimes single cover
        cover = (
            feed_obj.get("cover")
            or feed_obj.get("cover_url")
            or feed_obj.get("coverUrl")
        )
        if isinstance(cover, str):
            images.insert(0, {"url": cover})
        elif isinstance(cover, dict):
            url = cover.get("url") or cover.get("src") or cover.get("image")
            if url:
                images.insert(
                    0,
                    {
                        "url": url,
                        "width": cover.get("width"),
                        "height": cover.get("height"),
                    },
                )
        return images

    # Helper: normalize content/text
    def extract_content(feed_obj):
        if not isinstance(feed_obj, dict):
            return ""
        for k in ["content", "desc", "description", "text", "note", "title"]:
            v = feed_obj.get(k)
            if isinstance(v, str) and v.strip():
                return v
        return ""

    # Helper: normalize comments
    def extract_comments(feed_obj):
        comments = []
        if not isinstance(feed_obj, dict):
            return comments
        # Try common placements
        candidates = []
        for k in ["comments", "comment_list", "commentList", "replies"]:
            v = feed_obj.get(k)
            if isinstance(v, list):
                candidates = v
                break
        for c in candidates:
            if not isinstance(c, dict):
                continue
            comments.append(
                {
                    "comment_id": c.get("comment_id") or c.get("id"),
                    "user": extract_author(
                        {
                            "author": c.get("user")
                            or c.get("author")
                            or c.get("creator")
                            or {}
                        }
                    ),
                    "content": c.get("content") or c.get("text") or "",
                    "likes": c.get("likes") or c.get("like_count") or 0,
                    "created_at": c.get("created_at")
                    or c.get("create_time")
                    or c.get("time"),
                }
            )
        return comments

    # Helper: find by possible entity paths and recursive search
    def find_feed_in_single_context(ctx, target_feed_id):
        # Try known likely paths first
        likely_paths = [
            "xiaohongshu.feeds",
            "xiaohongshu.notes",
            "feeds",
            "notes",
            "social.xhs.feeds",
            "xhs.feeds",
            "xhs.notes",
            "xiaohongshu.feed_details",
        ]
        candidates = []

        for path in likely_paths:
            items = safe_list_entities(ctx, path)
            if isinstance(items, list):
                for it in items:
                    if isinstance(it, dict):
                        cid = (
                            it.get("feed_id")
                            or it.get("id")
                            or it.get("note_id")
                            or it.get("nid")
                        )
                        if cid == target_feed_id:
                            candidates.append(it)

        # Fallback to recursive search
        if not candidates:
            candidates = recursive_search_for_feed(ctx, target_feed_id)

        # De-duplicate by id
        seen = set()
        unique_candidates = []
        for c in candidates:
            if not isinstance(c, dict):
                continue
            cid = c.get("feed_id") or c.get("id") or c.get("note_id") or c.get("nid")
            if cid and cid not in seen:
                seen.add(cid)
                unique_candidates.append(c)

        # If multiple found, prefer ones tagged as xiaohongshu
        def is_xhs(obj):
            if not isinstance(obj, dict):
                return False
            platform = obj.get("platform") or obj.get("source") or ""
            return str(platform).lower() in ["xiaohongshu", "xhs", "red"]

        xhs_candidates = [c for c in unique_candidates if is_xhs(c)]
        if xhs_candidates:
            return xhs_candidates[0]
        return unique_candidates[0] if unique_candidates else None

    # 3. Validate entity references exist in context (CRITICAL!)
    found_feed = None
    contexts_to_search = (
        context_data if isinstance(context_data, list) else [context_data]
    )

    for ctx in contexts_to_search:
        if isinstance(ctx, dict):
            feed_obj = find_feed_in_single_context(ctx, feed_id)
            if feed_obj is not None:
                found_feed = feed_obj
                # Continue searching if token mismatch to possibly find a better match
                token_in_feed = (
                    found_feed.get("xsec_token")
                    or found_feed.get("xsecToken")
                    or found_feed.get("token")
                )
                if token_in_feed == xsec_token:
                    break  # Ideal match

    if not found_feed:
        return {"success": False, "error": f"Feed not found: {feed_id}", "result": None}

    # Validate token if present in feed
    token_in_feed = (
        found_feed.get("xsec_token")
        or found_feed.get("xsecToken")
        or found_feed.get("token")
    )
    if token_in_feed is not None and token_in_feed != xsec_token:
        return {
            "success": False,
            "error": "Invalid xsec_token for the specified feed_id",
            "result": None,
        }

    # 4. Perform operation (list/create/update/delete) using path-based functions
    # Since this is a 'get' operation, we prepare the response from found_feed
    result_payload = {
        "feed_id": feed_id,
        "content": extract_content(found_feed),
        "images": extract_images(found_feed),
        "author": extract_author(found_feed),
        "interactions": extract_interactions(found_feed),
        "comments": extract_comments(found_feed),
    }

    # 5. Save context if modified - Not applicable for 'get' operation (no modifications)

    # 6. Format and return response
    try:
        text_payload = json.dumps(result_payload, ensure_ascii=False)
    except Exception:
        # Fallback: best-effort serialization
        text_payload = json.dumps({"feed_id": feed_id})

    response = {
        "meta": None,
        "content": [
            {"type": "text", "text": text_payload, "mime_type": "application/json"}
        ],
        "isError": False,
    }

    return {"success": True, "error": None, "result": response}
