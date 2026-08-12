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
            "error": "Input must be an object/dict",
            "result": None,
        }
    if "article" not in parameters_used:
        return {
            "success": False,
            "error": "Missing required field: 'article'",
            "result": None,
        }
    article = parameters_used.get("article")
    if not isinstance(article, str) or not article.strip():
        return {
            "success": False,
            "error": "Field 'article' must be a non-empty string",
            "result": None,
        }
    article = article.strip()
    if article.startswith("t3_"):
        return {
            "success": False,
            "error": "Invalid article format: provide base-36 ID without 't3_' prefix",
            "result": None,
        }
    # simple base-36 check
    for ch in article:
        if ch.lower() not in "0123456789abcdefghijklmnopqrstuvwxyz":
            return {
                "success": False,
                "error": f"Invalid base-36 character in article ID: '{ch}'",
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

    # Helper to find post and comments in subreddit context
    def find_comments_in_subreddit(ctx, article_id):
        """
        Find comments in subreddit structure: subreddit.{subreddit_name}.posts.{article_id}.comments
        """
        if not isinstance(ctx, dict):
            return (False, [])

        # Try each subreddit under the subreddit key
        for subreddit_name, subreddit_data in ctx.items():
            if not isinstance(subreddit_data, dict):
                continue

            # Try to find the post in this subreddit's posts collection
            posts_path = f"{subreddit_name}.posts"
            try:
                post_entity = get_entity_by_path(ctx, posts_path, article_id)
            except Exception:
                post_entity = None

            if post_entity:
                # Post found, now get comments
                comments_path = f"{posts_path}[{article_id}].comments"
                try:
                    comments = list_entities_by_path(ctx, comments_path, {}, 10000)
                except Exception:
                    comments = None

                if comments is None:
                    comments = []

                # Ensure comments is a list
                if not isinstance(comments, list):
                    comments = (
                        list(comments)
                        if isinstance(comments, (set, tuple))
                        else [comments]
                    )

                return (True, comments)

        return (False, [])

    # 3. Search for comments in subreddit context
    found, comments = find_comments_in_subreddit(context_data, article)

    if not found:
        return {
            "success": False,
            "error": f"Invalid article ID or no comments found for article: {article}",
            "result": None,
        }

    # 4. Format and return response
    result_payload = {"article": article, "count": len(comments), "comments": comments}
    wrapped = {
        "meta": None,
        "content": [{"type": "text", "text": json.dumps(result_payload, ensure_ascii=False)}],
        "isError": False,
    }
    return {"success": True, "error": None, "result": wrapped}
