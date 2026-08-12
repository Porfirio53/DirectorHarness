import os
import time
from pathlib import Path
import json
from reddit.pycode.dynamic_context_handler import load_context, save_context


def analyze_response_patterns(parameters_used):
    # 1. Basic input format validation (types, required fields, etc.)
    if not isinstance(parameters_used, dict):
        return {
            "success": False,
            "error": "parameters_used must be a dictionary",
            "result": None,
        }
    required_fields = {"text", "thing_id"}
    allowed_fields = {"text", "thing_id"}
    missing = [f for f in required_fields if f not in parameters_used]
    if missing:
        return {
            "success": False,
            "error": f"Missing required fields: {', '.join(missing)}",
            "result": None,
        }
    extra = [f for f in parameters_used.keys() if f not in allowed_fields]
    if extra:
        return {
            "success": False,
            "error": f"Unexpected parameters: {', '.join(extra)}",
            "result": None,
        }
    text = parameters_used.get("text")
    thing_id = parameters_used.get("thing_id")

    if not isinstance(text, str):
        return {
            "success": False,
            "error": "Field 'text' must be a string",
            "result": None,
        }
    if not isinstance(thing_id, str):
        return {
            "success": False,
            "error": "Field 'thing_id' must be a string",
            "result": None,
        }
    if text.strip() == "":
        return {
            "success": False,
            "error": "Field 'text' cannot be empty",
            "result": None,
        }
    if not (thing_id.startswith("t3_") or thing_id.startswith("t1_")):
        return {
            "success": False,
            "error": "thing_id must start with 't3_' (post) or 't1_' (comment)",
            "result": None,
        }

    # 2. Load context data
    context_file_path = Path(__file__).parent.parent / "reddit.json"
    try:
        all_context_data = load_context(context_file_path)
    except Exception as e:
        return {
            "success": False,
            "error": f"Failed to load context: {e}",
            "result": None,
        }

    if not isinstance(all_context_data, dict) or len(all_context_data) == 0:
        return {
            "success": False,
            "error": "Invalid context data format",
            "result": None,
        }

    # Get subreddit and users from context
    subreddit_data = all_context_data.get("subreddit", {})
    users_data = all_context_data.get("users", {})

    if not isinstance(subreddit_data, dict) or not subreddit_data:
        return {
            "success": False,
            "error": "Subreddit data not found or invalid",
            "result": None,
        }

    if not isinstance(users_data, dict) or not users_data:
        return {
            "success": False,
            "error": "Users data not found or invalid",
            "result": None,
        }

    # 3. Determine current user
    current_user_id = os.environ.get("REDDIT_CONTEXT_ID")
    if current_user_id and current_user_id in users_data:
        current_user = users_data[current_user_id]
    else:
        # Default to first user
        first_user_id = list(users_data.keys())[0]
        current_user = users_data[first_user_id]
        current_user_id = first_user_id

    author_name = current_user.get("name", "unknown")

    # 4. Find parent post/comment in subreddit data
    # Extract base36 ID from thing_id (remove t3_ or t1_ prefix)
    base36_id = thing_id.split("_", 1)[1] if "_" in thing_id else thing_id

    parent_post = None
    parent_post_key = None
    parent_subreddit_name = None

    # Search for the parent post in subreddit structure
    for subreddit_name, subreddit_info in subreddit_data.items():
        if not isinstance(subreddit_info, dict):
            continue
        posts = subreddit_info.get("posts")
        if not isinstance(posts, dict):
            continue

        # Check if thing_id matches a post
        if thing_id.startswith("t3_"):
            # Direct post lookup
            if base36_id in posts:
                parent_post = posts[base36_id]
                parent_post_key = base36_id
                parent_subreddit_name = subreddit_name
                break
            # Try with prefix
            if thing_id in posts:
                parent_post = posts[thing_id]
                parent_post_key = thing_id
                parent_subreddit_name = subreddit_name
                break

        # Check if thing_id matches a comment in posts
        if thing_id.startswith("t1_"):
            for post_id, post_data in posts.items():
                if not isinstance(post_data, dict):
                    continue
                comments = post_data.get("comments")
                if not isinstance(comments, list):
                    continue
                for comment in comments:
                    if not isinstance(comment, dict):
                        continue
                    comment_id = comment.get("id", "")
                    if comment_id == thing_id or comment_id == base36_id:
                        parent_post = post_data
                        parent_post_key = post_id
                        parent_subreddit_name = subreddit_name
                        break
                if parent_post:
                    break
            if parent_post:
                break

    if not parent_post:
        return {
            "success": False,
            "error": f"Parent post/comment not found for thing_id: {thing_id}",
            "result": None,
        }

    # 5. Create new comment
    # Generate base36 ID (Reddit uses base36 encoding)
    import random
    import string

    base36_chars = string.digits + string.ascii_lowercase
    random_suffix = "".join(random.choices(base36_chars, k=7))
    new_comment_id = f"t1_{random_suffix}"
    created_utc = int(time.time())

    comment_data = {
        "id": new_comment_id,
        "author": author_name,
        "body": text,
        "parent_id": thing_id,
        "created_utc": created_utc,
        "replies": [],
    }

    # 6. Add comment to parent post's comments array
    comments_list = parent_post.get("comments", [])
    if not isinstance(comments_list, list):
        comments_list = []
    comments_list.append(comment_data)

    # Update parent post with new comment
    parent_post["comments"] = comments_list

    # Update the context data
    subreddit_data[parent_subreddit_name]["posts"][parent_post_key] = parent_post
    all_context_data["subreddit"] = subreddit_data

    # 7. Save context
    try:
        save_context(context_file_path, all_context_data)
    except Exception as e:
        return {
            "success": False,
            "error": f"Failed to save context: {e}",
            "result": None,
        }

    # 8. Format and return response
    result_payload = {
        "comment": comment_data,
        "parent_id": thing_id,
        "author": author_name,
        "author_id": current_user_id,
    }

    response = {
        "meta": None,
        "content": [{"type": "text", "text": json.dumps(result_payload, ensure_ascii=False)}],
        "isError": False,
    }
    return {"success": True, "error": None, "result": response}
