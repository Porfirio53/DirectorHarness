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
            return {"success": False, "error": "Invalid input: parameters_used must be a dictionary", "result": None}

        # Validate top-level keys
        allowed_top_keys = {"params", "path", "useUAT"}
        for k in parameters_used.keys():
            if k not in allowed_top_keys:
                return {"success": False, "error": f"Invalid input: unexpected field '{k}'", "result": None}

        path_obj = parameters_used.get("path")
        if not isinstance(path_obj, dict):
            return {"success": False, "error": "Invalid input: 'path' must be provided and be an object", "result": None}
        if "user_id" not in path_obj:
            return {"success": False, "error": "Invalid input: path.user_id is required", "result": None}
        user_id = path_obj.get("user_id")
        if not isinstance(user_id, str) or not user_id.strip():
            return {"success": False, "error": "Invalid input: path.user_id must be a non-empty string", "result": None}

        params_obj = parameters_used.get("params", {})
        if params_obj is None:
            params_obj = {}
        if not isinstance(params_obj, dict):
            return {"success": False, "error": "Invalid input: 'params' must be an object if provided", "result": None}

        # Validate user_id_type
        valid_user_id_types = {"open_id", "union_id", "user_id"}
        user_id_type = params_obj.get("user_id_type", "open_id")
        if user_id_type not in valid_user_id_types:
            return {"success": False, "error": f"Invalid input: params.user_id_type must be one of {sorted(list(valid_user_id_types))}", "result": None}

        # Validate department_id_type
        valid_department_id_types = {"department_id", "open_department_id"}
        department_id_type = params_obj.get("department_id_type")
        if department_id_type is not None and department_id_type not in valid_department_id_types:
            return {"success": False, "error": f"Invalid input: params.department_id_type must be one of {sorted(list(valid_department_id_types))}", "result": None}
    except Exception as e:
        return {"success": False, "error": f"Input validation error: {str(e)}", "result": None}

    # 2. Load all context data and select context(s) based on context_id from environment variable
    try:
        context_file_path = Path(json.loads(os.environ.get("LARK_MCP_SANDBOX_PATHS"))[0])
        all_context_data = load_context(context_file_path)  # Returns Dict[{context_id}: {context_dict}] or List[Dict]

        # For this tool, always use "all" contexts to search across all users
        context_id = "all"

        # Get context(s) based on context_id
        if context_id == "all":
            # Use all contexts (for query tools that need to access any context)
            if isinstance(all_context_data, dict):
                context_data = list(all_context_data.values())  # Return list of all context dicts
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
                return {"success": False, "error": f"Context ID '{context_id}' not found", "result": None}
        elif isinstance(all_context_data, list):
            # Context is list format (backward compatibility): [{context_dict}, ...]
            if context_id is None:
                context_data = all_context_data[0] if len(all_context_data) > 0 else {}
            else:
                # Try to find context by user_id field
                matched = None
                for ctx in all_context_data:
                    if isinstance(ctx, dict) and ctx.get("user_id") == context_id:
                        matched = ctx
                        break
                if matched is None:
                    return {"success": False, "error": f"Context ID '{context_id}' not found", "result": None}
                context_data = matched
        else:
            # Single context dict
            context_data = all_context_data
    except Exception as e:
        return {"success": False, "error": f"Failed to load context: {str(e)}", "result": None}

    # Helper functions for user extraction and matching
    def extract_user_dicts_from_context(ctx):
        users = []
        if isinstance(ctx, list):
            for item in ctx:
                users.extend(extract_user_dicts_from_context(item))
            return users
        if isinstance(ctx, dict):
            # If dict itself looks like a user record
            if any(k in ctx for k in ["user_id", "open_id", "union_id", "name", "email", "mobile"]):
                users.append(ctx)
            # If nested user object
            if isinstance(ctx.get("user"), dict):
                users.append(ctx.get("user"))
            # Heuristics: sometimes users could be under 'users' list
            if isinstance(ctx.get("users"), list):
                for u in ctx.get("users"):
                    if isinstance(u, dict):
                        users.append(u)
        return users

    def match_user(u, uid, uid_type):
        if not isinstance(u, dict):
            return False
        val = u.get(uid_type)
        if isinstance(val, str) and val == uid:
            return True
        # Some contexts might store IDs as nested fields or alternate keys
        # e.g., 'id' mapping to user_id; handle heuristically
        if uid_type == "user_id" and u.get("id") == uid:
            return True
        return False

    # 3. Validate entity references exist in context (CRITICAL!)
    # For user get operation, ensure the requested user exists in the selected context(s)
    try:
        candidate_users = extract_user_dicts_from_context(context_data)
        found_user = None
        for u in candidate_users:
            if match_user(u, user_id, user_id_type):
                found_user = u
                break

        if found_user is None:
            # If context_data is a single dict that might represent the user directly, check again directly
            if isinstance(context_data, dict) and match_user(context_data, user_id, user_id_type):
                found_user = context_data

        if found_user is None:
            return {"success": False, "error": f"User not found with {user_id_type}='{user_id}'", "result": None}
    except Exception as e:
        return {"success": False, "error": f"Error searching user in context: {str(e)}", "result": None}

    # 4. Perform operation (list/create/update/delete) using path-based functions
    # This is a GET operation; no modification. Prepare result with optional department_id_type transformation.
    try:
        # Prepare a shallow copy to avoid mutating context
        user_result = {}
        if isinstance(found_user, dict):
            # Only copy serializable simple structure
            user_result = json.loads(json.dumps(found_user,ensure_ascii=False))
        else:
            # If user object is not dict, return as-is string
            user_result = {"value": found_user}

        # Apply department_id_type preference if provided
        if department_id_type:
            # Attempt to standardize department IDs list
            # Case 1: departments as list of dicts with both IDs
            if isinstance(user_result.get("departments"), list):
                selected_ids = []
                normalized_departments = []
                for d in user_result.get("departments"):
                    if isinstance(d, dict):
                        if department_id_type in d:
                            selected_ids.append(d.get(department_id_type))
                        # Keep department objects but ensure requested id type is present
                        normalized_departments.append(d)
                    elif isinstance(d, str):
                        # Already an ID string; keep as-is
                        selected_ids.append(d)
                        normalized_departments.append(d)
                if selected_ids:
                    user_result["department_ids"] = selected_ids
                # Keep "departments" field unchanged to avoid unexpected shape loss
            else:
                # Case 2: separate fields exist like department_ids/open_department_ids
                if department_id_type == "department_id":
                    # Prefer department_ids if exists; else try to map from departments
                    if "open_department_ids" in user_result and "department_ids" not in user_result:
                        # Cannot safely transform open->department without mapping; skip
                        pass
                elif department_id_type == "open_department_id":
                    if "department_ids" in user_result and "open_department_ids" not in user_result:
                        # Cannot safely transform department->open without mapping; skip
                        pass

        # Include the id type used for clarity
        user_result["_id_type"] = user_id_type
        user_result["_queried_id"] = user_id
    except Exception as e:
        return {"success": False, "error": f"Error preparing user data: {str(e)}", "result": None}

    # 5. Save context if modified (not applicable for GET). Stub included for completeness and conformity.
    # No modifications performed; thus, no save needed.
    # Keeping code scaffold in case future modifications are added:
    # (No-op)

    # 6. Format and return response
    try:
        result_payload = {
            "meta": None,
            "content": [
                {
                    "type": "text",
                    "text": json.dumps(user_result, ensure_ascii=False)
                }
            ],
            "isError": False
        }
        return {"success": True, "error": None, "result": result_payload}
    except Exception as e:
        return {"success": False, "error": f"Failed to format response: {str(e)}", "result": None}