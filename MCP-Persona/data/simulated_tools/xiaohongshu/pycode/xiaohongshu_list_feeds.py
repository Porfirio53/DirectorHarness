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
    if parameters_used is None:
        parameters_used = {}
    if not isinstance(parameters_used, dict):
        return {
            "success": False,
            "error": "Invalid input: parameters_used must be a dict",
            "result": None,
        }

    # Optional pagination parameters
    page_size = parameters_used.get("page_size")
    page_token = parameters_used.get("page_token")
    try:
        if page_size is not None and not isinstance(page_size, int):
            return {
                "success": False,
                "error": "Invalid input: page_size must be an integer",
                "result": None,
            }
        if page_token is not None and not isinstance(page_token, (str, int)):
            return {
                "success": False,
                "error": "Invalid input: page_token must be a string or integer",
                "result": None,
            }
    except Exception as e:
        return {
            "success": False,
            "error": f"Input validation error: {str(e)}",
            "result": None,
        }

    # 2. Load all context data and select context(s) based on context_id from environment variable
    tool_name = "xiaohongshu:list_feeds"
    tool_name_lower = tool_name.lower() if tool_name else ""
    is_query_tool = any(
        keyword in tool_name_lower
        for keyword in ["get", "list", "query", "search", "batch"]
    )

    context_file_path = Path(json.loads(os.environ.get("XIAOHONGSHU_SANDBOX_PATHS"))[0])
    all_context_data = load_context(context_file_path)

    # Read context_id from environment variable (NOT from input parameters)
    context_id = os.environ.get("XIAOHONGSHU_CONTEXT_ID")

    # Default behavior based on tool type:
    if context_id is None:
        if is_query_tool:
            context_id = "all"
        else:
            if isinstance(all_context_data, dict) and len(all_context_data) > 0:
                context_id = list(all_context_data.keys())[0]
            else:
                context_id = None

    # Get context(s) based on context_id
    if context_id == "all":
        if isinstance(all_context_data, dict):
            context_data = list(all_context_data.values())
        elif isinstance(all_context_data, list):
            context_data = all_context_data
        else:
            context_data = [all_context_data]
    elif isinstance(all_context_data, dict):
        if context_id in all_context_data:
            context_data = all_context_data[context_id]
        else:
            return {
                "success": False,
                "error": f"Context ID '{context_id}' not found",
                "result": None,
            }
    elif isinstance(all_context_data, list):
        if context_id is None:
            context_data = all_context_data[0] if len(all_context_data) > 0 else {}
        else:
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
        context_data = all_context_data

    # 3. Validate entity references exist in context (CRITICAL!)
    # For this list operation, there are no filter references to validate.

    # 4. Perform operation (list) using path-based functions
    def _collect_feeds_from_single_context(single_ctx):
        # Try the most likely paths for Xiaohongshu feeds
        feeds = []
        try:
            feeds = list_entities_by_path(single_ctx, "xiaohongshu.feeds", {}, 10000)
            if not isinstance(feeds, list):
                feeds = []
        except Exception:
            feeds = []

        if not feeds:
            try:
                feeds = list_entities_by_path(single_ctx, "feeds", {}, 10000)
                if not isinstance(feeds, list):
                    feeds = []
            except Exception:
                feeds = []

        # Fallback: attempt to list everything and heuristically find feeds under 'xiaohongshu' key
        if not feeds:
            try:
                all_entities = list_entities_by_path(single_ctx, "None", {}, 10000)
                if isinstance(single_ctx, dict):
                    xhs = single_ctx.get("xiaohongshu")
                    if isinstance(xhs, dict):
                        inner_feeds = xhs.get("feeds")
                        if isinstance(inner_feeds, list):
                            feeds = inner_feeds
            except Exception:
                pass

        # Ensure list type
        if not isinstance(feeds, list):
            feeds = []
        return feeds

    # Aggregate feeds across contexts
    aggregated_feeds = []
    if isinstance(context_data, list):
        for ctx in context_data:
            if isinstance(ctx, dict):
                aggregated_feeds.extend(_collect_feeds_from_single_context(ctx))
    elif isinstance(context_data, dict):
        aggregated_feeds.extend(_collect_feeds_from_single_context(context_data))
    else:
        # Unknown context structure, attempt safe fallback
        try:
            fallback_list = list_entities_by_path(context_data, "None", {}, 10000)
            if isinstance(fallback_list, list):
                aggregated_feeds.extend(fallback_list)
        except Exception:
            pass

    # Pagination logic
    total_count = len(aggregated_feeds)
    if page_size is None:
        page_size = 20
    start_index = 0
    if page_token is not None:
        try:
            start_index = int(page_token)
            if start_index < 0:
                start_index = 0
        except Exception:
            start_index = 0
    end_index = start_index + page_size
    sliced_feeds = aggregated_feeds[start_index:end_index]
    has_more = end_index < total_count
    next_page_token = str(end_index) if has_more else None

    # 5. Save context if modified (not applicable for list operation, so skip)
    # Example structure retained for compliance when modifications occur:
    # context_id_save = os.environ.get("XIAOHONGSHU_CONTEXT_ID")
    # if context_id_save is None:
    #     if isinstance(all_context_data, dict) and len(all_context_data) > 0:
    #         context_id_save = list(all_context_data.keys())[0]
    # if isinstance(all_context_data, dict):
    #     if context_id_save and context_id_save != "all" and context_id_save in all_context_data:
    #         all_context_data[context_id_save] = context_data
    # elif isinstance(all_context_data, list):
    #     if isinstance(context_data, dict):
    #         user_id = context_data.get("user_id")
    #         if user_id:
    #             for i, ctx in enumerate(all_context_data):
    #                 if isinstance(ctx, dict) and ctx.get("user_id") == user_id:
    #                     all_context_data[i] = context_data
    #                     break
    # else:
    #     all_context_data = context_data
    # save_context(context_file_path, all_context_data)

    # 6. Format and return response
    # Match success examples: if no feeds, return empty dict
    if total_count == 0:
        result_payload = {}
    else:
        result_payload = {
            "items": sliced_feeds,
            "has_more": has_more,
            "next_page_token": next_page_token,
        }

    response = {
        "meta": None,
        "content": [
            {"type": "text", "text": json.dumps(result_payload, ensure_ascii=False)}
        ],
        "isError": False,
    }

    return {"success": True, "error": None, "result": response}
