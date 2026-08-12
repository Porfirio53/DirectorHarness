from pathlib import Path
import json
import os
import uuid
from datetime import datetime, timezone
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
    if not isinstance(parameters_used, dict):
        return {
            "success": False,
            "error": "Invalid parameters: expected an object/dict",
            "result": None,
        }
    content = parameters_used.get("content")
    if content is None:
        return {
            "success": False,
            "error": "Missing required field: content",
            "result": None,
        }
    if not isinstance(content, str):
        return {
            "success": False,
            "error": "Invalid type for 'content': expected string",
            "result": None,
        }
    if content.strip() == "":
        return {
            "success": False,
            "error": "Field 'content' cannot be empty",
            "result": None,
        }

    mentioned_list = parameters_used.get("mentioned_list")
    if mentioned_list is not None and not isinstance(mentioned_list, str):
        return {
            "success": False,
            "error": "Invalid type for 'mentioned_list': expected string",
            "result": None,
        }
    mentioned_mobile_list = parameters_used.get("mentioned_mobile_list")
    if mentioned_mobile_list is not None and not isinstance(mentioned_mobile_list, str):
        return {
            "success": False,
            "error": "Invalid type for 'mentioned_mobile_list': expected string",
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
    tool_name = "wecome:send_text"
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

    # If someone set WECOME_CONTEXT_ID=all for a modifying tool, reject it
    if isinstance(context_data, list):
        return {
            "success": False,
            "error": "WECOME_CONTEXT_ID 'all' is not allowed for wecome:send_text; specify a single context ID.",
            "result": None,
        }

    # 3. Validate entity references exist in context (CRITICAL!)
    # This tool does not reference parent entities; no external references to validate.

    # 4. Perform operation (list/create/update/delete) using path-based functions
    # Simulate sending a text message to WeCom group; record in context log
    try:
        # Prepare message record
        now_iso = datetime.now(timezone.utc).isoformat()
        message_record = {
            "id": str(uuid.uuid4()),
            "tool": "wecome:send_text",
            "content": content,
            "mentioned_list_raw": mentioned_list,
            "mentioned_mobile_list_raw": mentioned_mobile_list,
            "mentioned_list": (
                [s.strip() for s in mentioned_list.split(",")]
                if isinstance(mentioned_list, str) and mentioned_list.strip() != ""
                else []
            ),
            "mentioned_mobile_list": (
                [s.strip() for s in mentioned_mobile_list.split(",")]
                if isinstance(mentioned_mobile_list, str)
                and mentioned_mobile_list.strip() != ""
                else []
            ),
            "timestamp": now_iso,
            "status": "sent",
        }

        # Update context_data log
        if not isinstance(context_data, dict):
            return {
                "success": False,
                "error": "Invalid context format: expected dict for single context",
                "result": None,
            }
        wecome_section = context_data.get("wecome")
        if not isinstance(wecome_section, dict):
            wecome_section = {}
            context_data["wecome"] = wecome_section
        sent_list = wecome_section.get("sent_texts")
        if not isinstance(sent_list, list):
            sent_list = []
            wecome_section["sent_texts"] = sent_list
        sent_list.append(message_record)

    except Exception as e:
        return {
            "success": False,
            "error": f"Failed to simulate send_text: {e}",
            "result": None,
        }

    # 5. Save context if modified
    try:
        # Get context_id from environment variable (same as when loading)
        context_id = os.environ.get("WECOME_CONTEXT_ID")
        if context_id is None:
            # Use first key if dict, or first element if list
            if isinstance(all_context_data, dict) and len(all_context_data) > 0:
                context_id = list(all_context_data.keys())[0]

        # Update the modified context back to all_context_data
        if isinstance(all_context_data, dict):
            # Dict format: {context_id: context_dict, ...}
            if context_id and context_id != "all" and context_id in all_context_data:
                all_context_data[context_id] = (
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
    # The tool's expected successful output example is {}, so we return an empty object as the tool result.
    api_result = {}

    formatted = {
        "meta": None,
        "content": [
            {"type": "text", "text": json.dumps(api_result, ensure_ascii=False)}
        ],
        "isError": False,
    }
    return {"success": True, "error": None, "result": formatted}
