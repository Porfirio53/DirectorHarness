from pathlib import Path
import json
from reddit.pycode.dynamic_context_handler import (
    load_context,
    get_entity_by_path,
    list_entities_by_path,
)


def analyze_response_patterns(parameters_used):
    # 1. Basic input format validation (types, required fields, etc.)
    if not isinstance(parameters_used, dict):
        return {
            "success": False,
            "error": "Invalid input: parameters_used must be a dictionary",
            "result": None,
        }

    allowed_keys = {"size", "subreddit"}
    extra_keys = set(parameters_used.keys()) - allowed_keys
    if extra_keys:
        return {
            "success": False,
            "error": f"Invalid input: unexpected keys {sorted(list(extra_keys))}",
            "result": None,
        }

    if "subreddit" not in parameters_used:
        return {
            "success": False,
            "error": "Missing required field: 'subreddit'",
            "result": None,
        }

    subreddit = parameters_used.get("subreddit")
    if not isinstance(subreddit, str) or not subreddit.strip():
        return {
            "success": False,
            "error": "Invalid input: 'subreddit' must be a non-empty string",
            "result": None,
        }
    subreddit = subreddit.strip()

    size = parameters_used.get("size", 5)
    if size is None:
        size = 5
    elif not isinstance(size, int):
        return {
            "success": False,
            "error": "Invalid input: 'size' must be an integer or null",
            "result": None,
        }
    elif size < 0:
        return {
            "success": False,
            "error": "Invalid input: 'size' must be a non-negative integer",
            "result": None,
        }

    # 2. Load context data and use the first top-level key (subreddit)
    context_file_path = Path(__file__).parent.parent / "reddit.json"
    try:
        all_context_data = load_context(context_file_path)
    except Exception as e:
        return {
            "success": False,
            "error": f"Failed to load context: {e}",
            "result": None,
        }

    # Use the first top-level key (subreddit) as the search context
    if not isinstance(all_context_data, dict) or len(all_context_data) == 0:
        return {
            "success": False,
            "error": "Invalid context data format",
            "result": None,
        }

    context_key = list(all_context_data.keys())[0]
    context_data = all_context_data[context_key]

    # Helper to find posts in subreddit context
    def find_posts_in_subreddit(ctx, subreddit_name):
        """
        Find posts in subreddit structure: subreddit.{subreddit_name}.posts
        """
        if not isinstance(ctx, dict):
            return []

        # Try to find the subreddit
        if subreddit_name not in ctx:
            return []

        subreddit_data = ctx[subreddit_name]
        if not isinstance(subreddit_data, dict):
            return []

        # Get posts collection
        posts = subreddit_data.get("posts")
        if posts is None:
            return []

        # Convert posts dict to list
        if isinstance(posts, dict):
            return list(posts.values())
        elif isinstance(posts, list):
            return posts
        else:
            return []

    # 3. Search for posts in subreddit context
    posts = find_posts_in_subreddit(context_data, subreddit)

    if not posts:
        return {
            "success": False,
            "error": f"Subreddit '{subreddit}' not found or has no posts",
            "result": None,
        }

    # 4. Apply size limit
    total_available = len(posts)
    if size == 0:
        limited_posts = posts
    else:
        limited_posts = posts[:size]

    result_payload = {
        "tool": "reddit:retrieve_reddit_post",
        "subreddit": subreddit,
        "requested_size": size,
        "returned_count": len(limited_posts),
        "available_count": total_available,
        "posts": limited_posts,
    }

    # 5. Format and return response
    wrapped = {
        "meta": None,
        "content": [{"type": "text", "text": json.dumps(result_payload, ensure_ascii=False)}],
        "isError": False,
    }
    return {"success": True, "error": None, "result": wrapped}
