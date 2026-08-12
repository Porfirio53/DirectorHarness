from pathlib import Path
import json
from reddit.pycode.dynamic_context_handler import load_context, get_entity_by_path

def analyze_response_patterns(parameters_used):
    # 1. Basic input format validation
    if not isinstance(parameters_used, dict):
        return {"success": False, "error": "Invalid input: parameters_used must be a dictionary", "result": None}

    if "id" not in parameters_used:
        return {"success": False, "error": "Missing required field: id", "result": None}

    fullname = parameters_used.get("id")
    if not isinstance(fullname, str) or not fullname.strip():
        return {"success": False, "error": "Invalid id: must be a non-empty string", "result": None}
    fullname = fullname.strip()

    # Validate Reddit fullname format
    if not (fullname.startswith("t1_") or fullname.startswith("t3_")):
        return {"success": False, "error": "Invalid id format: must start with 't1_' (comment) or 't3_' (post)", "result": None}

    obj_type = "comment" if fullname.startswith("t1_") else "post"
    base36_id = fullname.split("_", 1)[1] if "_" in fullname else fullname

    # 2. Load context data and use the first top-level key (subreddit)
    context_file_path = Path(__file__).parent.parent / "reddit.json"
    try:
        all_context_data = load_context(context_file_path)
    except Exception as e:
        return {"success": False, "error": f"Failed to load context: {e}", "result": None}

    # Use the first top-level key (subreddit) as the search context
    if not isinstance(all_context_data, dict) or len(all_context_data) == 0:
        return {"success": False, "error": "Invalid context data format", "result": None}

    context_key = list(all_context_data.keys())[0]
    context_data = all_context_data[context_key]

    # 3. Search for the entity in subreddit context
    def entity_matches(e):
        """Check if entity matches the target fullname."""
        if not isinstance(e, dict):
            return False
        name = e.get("name")
        fullname_field = e.get("fullname")
        eid = e.get("id")
        alt_ids = [
            e.get("comment_id"),
            e.get("post_id"),
        ]
        if name == fullname:
            return True
        if fullname_field == fullname:
            return True
        if eid == fullname or eid == base36_id:
            return True
        for aid in alt_ids:
            if aid == fullname or aid == base36_id:
                return True
        # Also test constructing fullname from id if present
        if eid and isinstance(eid, str):
            prefix = "t1_" if obj_type == "comment" else "t3_"
            if f"{prefix}{eid}" == fullname:
                return True
        return False

    def search_entity(ctx):
        """
        Search for entity in subreddit structure.
        For posts: subreddit.{subreddit_name}.posts[{article_id}]
        For comments: subreddit.{subreddit_name}.posts[{article_id}].comments[{comment_id}]
        """
        if not isinstance(ctx, dict):
            return None

        # Try each subreddit
        for subreddit_name, subreddit_data in ctx.items():
            if not isinstance(subreddit_data, dict):
                continue

            # Try direct post lookup
            posts_path = f"{subreddit_name}.posts"
            try:
                post_entity = get_entity_by_path(ctx, posts_path, base36_id)
                if post_entity and entity_matches(post_entity):
                    return {
                        "subreddit": subreddit_name,
                        "item": post_entity,
                        "path": posts_path
                    }
            except Exception:
                pass

            # If looking for comment, search in posts' comments
            if obj_type == "comment":
                posts = subreddit_data.get("posts")
                if posts and isinstance(posts, dict):
                    for post_id, post_data in posts.items():
                        if isinstance(post_data, dict):
                            comments = post_data.get("comments")
                            if comments and isinstance(comments, list):
                                # Comments is a list, search manually
                                for comment in comments:
                                    if isinstance(comment, dict) and entity_matches(comment):
                                        return {
                                            "subreddit": subreddit_name,
                                            "post_id": post_id,
                                            "item": comment,
                                            "path": f"{posts_path}[{post_id}].comments"
                                        }

        return None

    found_result = search_entity(context_data)

    if not found_result:
        return {"success": False, "error": f"Item with fullname '{fullname}' not found", "result": None}

    # 4. Format and return response
    payload = {
        "type": obj_type,
        "fullname": fullname,
        "subreddit": found_result.get("subreddit"),
        "data": found_result.get("item"),
    }

    wrapped = {
        "meta": None,
        "content": [
            {
                "type": "text",
                "text": json.dumps(payload,ensure_ascii=False)
            }
        ],
        "isError": False
    }
    return {"success": True, "error": None, "result": wrapped}