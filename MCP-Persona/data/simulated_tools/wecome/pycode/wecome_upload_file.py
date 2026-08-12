from pathlib import Path
import json
import os
import hashlib
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
    if not isinstance(parameters_used, dict):
        return {
            "success": False,
            "error": "Invalid parameters format: expected an object",
            "result": None,
        }
    if "file_path" not in parameters_used:
        return {
            "success": False,
            "error": "Missing required field: file_path",
            "result": None,
        }
    file_path = parameters_used.get("file_path")
    if not isinstance(file_path, str):
        return {
            "success": False,
            "error": "Invalid type for file_path: expected string",
            "result": None,
        }
    file_path_str = file_path.strip()
    if not file_path_str:
        return {"success": False, "error": "file_path cannot be empty", "result": None}
    try:
        file_path_obj = Path(file_path_str).expanduser().resolve()
    except Exception as e:
        return {"success": False, "error": f"Invalid file path: {e}", "result": None}
    if not file_path_obj.exists():
        return {
            "success": False,
            "error": f"File does not exist: {file_path_obj}",
            "result": None,
        }
    if not file_path_obj.is_file():
        return {
            "success": False,
            "error": f"Path is not a file: {file_path_obj}",
            "result": None,
        }

    # 2. Load all context data and select context(s) based on context_id from environment variable
    context_file_path = Path(json.loads(os.environ.get("WECOM_SANDBOX_PATHS"))[0])
    all_context_data = load_context(
        context_file_path
    )  # Returns Dict[{context_id}: {context_dict}] or List[Dict]

    # Read context_id from environment variable (NOT from input parameters)
    context_id = os.environ.get("WECOME_CONTEXT_ID")

    # Determine tool type from tool_name or description to set appropriate default
    tool_name = "wecome:upload_file"
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

    # If WECOME_CONTEXT_ID was "all", we cannot modify multiple contexts in a single upload operation
    if isinstance(context_data, list):
        return {
            "success": False,
            "error": "WECOME_CONTEXT_ID is set to 'all', which is not supported for this modify operation",
            "result": None,
        }

    # 3. Validate entity references exist in context (CRITICAL!)
    # For this tool (file upload), there are no entity references to validate.

    # 4. Perform operation (simulate file upload)
    # Compute a fake media_id and record metadata
    try:
        hasher = hashlib.sha256()
        with open(file_path_obj, "rb") as f:
            for chunk in iter(lambda: f.read(8192), b""):
                hasher.update(chunk)
        digest = hasher.hexdigest()
        media_id = f"media_{digest[:16]}"
        upload_record = {
            "media_id": media_id,
            "file_name": file_path_obj.name,
            "file_path": str(file_path_obj),
            "size": file_path_obj.stat().st_size,
            "uploaded_at": datetime.utcnow().isoformat() + "Z",
            "tool": "wecome:upload_file",
        }
        # Store in context under "wecome_uploads"
        if not isinstance(context_data, dict):
            context_data = {}
        uploads = context_data.get("wecome_uploads")
        if not isinstance(uploads, list):
            uploads = []
        # Avoid duplicates by media_id
        if not any(u.get("media_id") == media_id for u in uploads):
            uploads.append(upload_record)
        context_data["wecome_uploads"] = uploads
    except Exception as e:
        return {
            "success": False,
            "error": f"Failed to process file: {e}",
            "result": None,
        }

    # 5. Save context if modified
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
            all_context_data[context_id] = context_data  # Update the specific context
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

    try:
        save_context(context_file_path, all_context_data)
    except Exception as e:
        return {
            "success": False,
            "error": f"Failed to save context: {e}",
            "result": None,
        }

    # 6. Format and return response
    # According to success examples, output is an empty object {}
    tool_output = {}
    wrapped_result = {
        "meta": None,
        "content": [
            {"type": "text", "text": json.dumps(tool_output, ensure_ascii=False)}
        ],
        "isError": False,
    }
    return {"success": True, "error": None, "result": wrapped_result}
