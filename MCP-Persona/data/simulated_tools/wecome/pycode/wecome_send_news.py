from pathlib import Path
import json
import os
import uuid
from datetime import datetime
from  wecome.pycode.dynamic_context_handler  import (
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
    if parameters_used is None or not isinstance(parameters_used, dict):
        return {
            "success": False,
            "error": "Invalid input: parameters_used must be a JSON object",
            "result": None,
        }
    # Validate required fields
    title = parameters_used.get("title")
    url = parameters_used.get("url")
    description = parameters_used.get("description")
    picurl = parameters_used.get("picurl")

    if title is None:
        return {
            "success": False,
            "error": "Missing required field: title",
            "result": None,
        }
    if url is None:
        return {
            "success": False,
            "error": "Missing required field: url",
            "result": None,
        }

    if not isinstance(title, str):
        return {
            "success": False,
            "error": "Invalid type for field 'title': expected string",
            "result": None,
        }
    if not isinstance(url, str):
        return {
            "success": False,
            "error": "Invalid type for field 'url': expected string",
            "result": None,
        }
    if description is not None and not isinstance(description, str):
        return {
            "success": False,
            "error": "Invalid type for field 'description': expected string",
            "result": None,
        }
    if picurl is not None and not isinstance(picurl, str):
        return {
            "success": False,
            "error": "Invalid type for field 'picurl': expected string",
            "result": None,
        }

    # Optional: minimal URL sanity checks (non-fatal if not strictly required)
    if url.strip() == "":
        return {
            "success": False,
            "error": "Invalid value for 'url': cannot be empty",
            "result": None,
        }

    # 2. Load all context data and select context(s) based on context_id from environment variable
    context_file_path = Path(json.loads(os.environ.get("WECOM_SANDBOX_PATHS"))[0])
    try:
        all_context_data = load_context(context_file_path)
    except Exception as e:
        return {
            "success": False,
            "error": f"Failed to load context: {e}",
            "result": None,
        }

    # Read context_id from environment variable (NOT from input parameters)
    context_id = os.environ.get("WECOME_CONTEXT_ID")

    # Determine tool type from tool_name or description to set appropriate default
    tool_name = "wecome:send_news"
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

    # Prevent modifications when context_id == "all" (we only allow modifying a single context)
    if not is_query_tool and isinstance(context_data, list):
        return {
            "success": False,
            "error": "WECOME_CONTEXT_ID cannot be 'all' for send operation. Set WECOME_CONTEXT_ID to a specific context id.",
            "result": None,
        }

    # 3. Validate entity references exist in context (none required for this tool)
    # No external entity references (like calendar_id) are required for wecome:send_news.

    # 4. Perform operation (simulate sending a WeCom news message)
    try:
        # Construct the message entity
        message_id = f"msg_{uuid.uuid4().hex}"
        timestamp = datetime.utcnow().isoformat() + "Z"

        news_entity = {
            "message_id": message_id,
            "type": "news",
            "title": title,
            "url": url,
            "description": description if description is not None else "",
            "picurl": picurl if picurl is not None else "",
            "status": "sent",
            "created_at": timestamp,
        }

        # Use a dedicated path within the context to store sent WeCom news messages
        # This will create or append to the collection at: wecome.sent_news
        path = "wecome.sent_news"
        created = create_entity_by_path(context_data, path, news_entity, message_id)

        # If create_entity_by_path doesn't return the created entity, try fetching it
        if not created:
            created = get_entity_by_path(context_data, path, message_id)

        if not created:
            return {
                "success": False,
                "error": "Failed to record sent news message in context",
                "result": None,
            }
    except Exception as e:
        return {"success": False, "error": f"Failed to send news: {e}", "result": None}

    # 5. Save context if modified
    try:
        # Get context_id from environment variable (same as when loading)
        context_id_save = os.environ.get("WECOME_CONTEXT_ID")
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
    except Exception as e:
        return {
            "success": False,
            "error": f"Failed to save context: {e}",
            "result": None,
        }

    # 6. Format and return response
    try:
        result_payload = {
            "meta": None,
            "content": [
                {
                    "type": "text",
                    "text": json.dumps(
                        {
                            "message_id": created.get("message_id", message_id),
                            "title": created.get("title", title),
                            "url": created.get("url", url),
                            "description": created.get(
                                "description",
                                description if description is not None else "",
                            ),
                            "picurl": created.get(
                                "picurl", picurl if picurl is not None else ""
                            ),
                            "status": created.get("status", "sent"),
                            "created_at": created.get("created_at", timestamp),
                        },
                        ensure_ascii=False,
                    ),
                }
            ],
            "isError": False,
        }
        return {"success": True, "error": None, "result": result_payload}
    except Exception as e:
        return {
            "success": False,
            "error": f"Failed to format result: {e}",
            "result": None,
        }
