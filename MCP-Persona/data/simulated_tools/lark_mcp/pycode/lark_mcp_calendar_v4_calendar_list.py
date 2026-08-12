from pathlib import Path
import json
import os
from lark_mcp.dynamic_context_handler import load_context, save_context, get_entity_by_path, list_entities_by_path, create_entity_by_path, update_entity_by_path, delete_entity_by_path

def analyze_response_patterns(parameters_used):
    # 1. Basic input format validation (types, required fields, etc.)
    try:
        if parameters_used is None:
            parameters_used = {}
        if not isinstance(parameters_used, dict):
            return {"success": False, "error": "Invalid input: parameters_used must be an object", "result": None}

        # Extract and validate params
        params = parameters_used.get("params", {})
        if params is None:
            params = {}
        if not isinstance(params, dict):
            return {"success": False, "error": "Invalid input: 'params' must be an object", "result": None}

        allowed_param_keys = {"page_size", "page_token", "sync_token"}
        for k in params.keys():
            if k not in allowed_param_keys:
                return {"success": False, "error": f"Invalid parameter: 'params.{k}' is not allowed", "result": None}

        page_size = params.get("page_size")
        if page_size is not None and not isinstance(page_size, (int, float)):
            return {"success": False, "error": "Invalid input: 'params.page_size' must be a number", "result": None}
        if page_size is None:
            page_size_int = 100
        else:
            try:
                page_size_int = int(page_size)
            except Exception:
                return {"success": False, "error": "Invalid input: 'params.page_size' must be convertible to integer", "result": None}
        if page_size_int <= 0:
            return {"success": False, "error": "Invalid input: 'params.page_size' must be greater than 0", "result": None}

        page_token = params.get("page_token")
        if page_token is not None and not isinstance(page_token, str):
            return {"success": False, "error": "Invalid input: 'params.page_token' must be a string", "result": None}

        sync_token = params.get("sync_token")
        if sync_token is not None and not isinstance(sync_token, str):
            return {"success": False, "error": "Invalid input: 'params.sync_token' must be a string", "result": None}

        useUAT = parameters_used.get("useUAT")
        if useUAT is not None and not isinstance(useUAT, bool):
            return {"success": False, "error": "Invalid input: 'useUAT' must be a boolean", "result": None}

    except Exception as e:
        return {"success": False, "error": f"Input validation error: {str(e)}", "result": None}

    tool_name = "lark-mcp:calendar_v4_calendar_list"

    # 2. Load all context data and select context(s) based on context_id from environment variable
    try:
        context_file_path = Path(json.loads(os.environ.get("LARK_MCP_SANDBOX_PATHS"))[0])
        all_context_data = load_context(context_file_path)

        # Read context_id from environment variable (NOT from input parameters)
        context_id = os.environ.get("LARK_MCP_CONTEXT_ID")

        # Determine tool type from tool_name or description to set appropriate default
        tool_name_lower = tool_name.lower() if tool_name else ""
        is_query_tool = any(keyword in tool_name_lower for keyword in ["get", "list", "query", "search", "batch"])

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
                context_data = list(all_context_data.values())  # Return list of all context dicts
            elif isinstance(all_context_data, list):
                context_data = all_context_data
            else:
                context_data = [all_context_data] if all_context_data is not None else []
        elif isinstance(all_context_data, dict):
            # Context is dict format: {context_id: context_dict, ...}
            if context_id in all_context_data:
                context_data = all_context_data[context_id]  # Get single context dict
            else:
                # Context ID not found, return error
                return {"success": False, "error": f"Context ID '{context_id}' not found", "result": None}
        elif isinstance(all_context_data, list):
            # Context is list format (backward compatibility): [{context_dict}, ...]
            if context_id is None:
                context_data = all_context_data[0] if len(all_context_data) > 0 else {}
            else:
                # Try to find context by user_id field
                found_ctx = None
                for ctx in all_context_data:
                    if isinstance(ctx, dict) and ctx.get("user_id") == context_id:
                        found_ctx = ctx
                        break
                if found_ctx is None:
                    return {"success": False, "error": f"Context ID '{context_id}' not found", "result": None}
                context_data = found_ctx
        else:
            # Single context dict
            context_data = all_context_data
    except Exception as e:
        return {"success": False, "error": f"Failed to load context: {str(e)}", "result": None}

    # 3. Validate entity references exist in context (CRITICAL!) - For calendar listing, no parent references to validate.
    # However, ensure context_data is in expected form.
    try:
        # Build a flat list of calendars across selected contexts
        calendars_flat = []

        def list_calendars_from_ctx(ctx):
            try:
                return list_entities_by_path(ctx, "calendars", {}, 10000) or []
            except Exception:
                return []

        if isinstance(context_data, list):
            for single_ctx in context_data:
                if isinstance(single_ctx, dict):
                    calendars_flat.extend(list_calendars_from_ctx(single_ctx))
        elif isinstance(context_data, dict):
            calendars_flat.extend(list_calendars_from_ctx(context_data))
        else:
            # Unknown format; proceed with empty
            calendars_flat = []

    except Exception as e:
        return {"success": False, "error": f"Error while retrieving calendars: {str(e)}", "result": None}

    # 4. Perform operation (list using pagination logic)
    try:
        # Handle page_token as numeric offset; accept plain integer string or "offset:<n>"
        def parse_offset_from_token(token_str):
            if token_str is None or token_str == "":
                return 0
            token_str = token_str.strip()
            # Try direct int
            try:
                return int(token_str)
            except Exception:
                pass
            # Try offset:<n>
            if token_str.lower().startswith("offset:"):
                try:
                    return int(token_str.split(":", 1)[1])
                except Exception:
                    return None
            # Unrecognized token
            return None

        offset = parse_offset_from_token(page_token)
        if offset is None or offset < 0:
            return {"success": False, "error": "Invalid input: 'params.page_token' is invalid or malformed", "result": None}

        total_count = len(calendars_flat)
        start = min(offset, total_count)
        end = min(start + page_size_int, total_count)
        page_items = calendars_flat[start:end]
        has_more = end < total_count
        next_page_token = str(end) if has_more else ""
        # Generate a simple sync token when no more pages; could be used for incremental sync
        out_sync_token = f"sync_{total_count}" if not has_more else ""

        # Simulated API response payload
        result_payload = {
            "calendar_list": page_items,
            "has_more": has_more,
            "page_token": next_page_token,
            "sync_token": out_sync_token
        }

    except Exception as e:
        return {"success": False, "error": f"Failed to list calendars: {str(e)}", "result": None}

    # 5. Save context if modified - Not applicable for list/query operations (no modifications)
    # 6. Format and return response
    try:
        wrapped_result = {
            "meta": None,
            "content": [
                {
                    "type": "text",
                    "text": json.dumps(result_payload, ensure_ascii=False)
                }
            ],
            "isError": False
        }
        return {"success": True, "error": None, "result": wrapped_result}
    except Exception as e:
        return {"success": False, "error": f"Failed to format response: {str(e)}", "result": None}