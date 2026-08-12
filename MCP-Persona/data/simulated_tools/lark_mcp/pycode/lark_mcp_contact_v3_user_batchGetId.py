from pathlib import Path
import json
import os
from lark_mcp.dynamic_context_handler import load_context, save_context, get_entity_by_path, list_entities_by_path, create_entity_by_path, update_entity_by_path, delete_entity_by_path

def analyze_response_patterns(parameters_used):
    # 1. Basic input format validation (types, required fields, etc.)
    if parameters_used is None or not isinstance(parameters_used, dict):
        return {"success": False, "error": "Invalid input: parameters_used must be a dict", "result": None}

    data = parameters_used.get("data", {})
    if data is None:
        data = {}
    if not isinstance(data, dict):
        return {"success": False, "error": "Invalid input: 'data' must be an object", "result": None}

    emails = data.get("emails", [])
    mobiles = data.get("mobiles", [])
    include_resigned = data.get("include_resigned", False)

    # Validate types
    if emails is not None and not isinstance(emails, list):
        return {"success": False, "error": "Invalid input: 'emails' must be an array of strings", "result": None}
    if mobiles is not None and not isinstance(mobiles, list):
        return {"success": False, "error": "Invalid input: 'mobiles' must be an array of strings", "result": None}
    if include_resigned is not None and not isinstance(include_resigned, bool):
        return {"success": False, "error": "Invalid input: 'include_resigned' must be a boolean", "result": None}

    # Validate individual items are strings
    if any(not isinstance(e, str) for e in emails):
        return {"success": False, "error": "Invalid input: all 'emails' entries must be strings", "result": None}
    if any(not isinstance(m, str) for m in mobiles):
        return {"success": False, "error": "Invalid input: all 'mobiles' entries must be strings", "result": None}

    # Validate size limits
    if len(emails) > 50:
        return {"success": False, "error": "Invalid input: 'emails' can contain at most 50 entries", "result": None}
    if len(mobiles) > 50:
        return {"success": False, "error": "Invalid input: 'mobiles' can contain at most 50 entries", "result": None}

    params = parameters_used.get("params", {})
    if params is None:
        params = {}
    if not isinstance(params, dict):
        return {"success": False, "error": "Invalid input: 'params' must be an object", "result": None}

    user_id_type = params.get("user_id_type")
    if user_id_type is not None and user_id_type not in ["open_id", "union_id", "user_id"]:
        return {"success": False, "error": "Invalid input: 'user_id_type' must be one of ['open_id', 'union_id', 'user_id']", "result": None}

    # At least one email or mobile should be provided
    if (not emails or len(emails) == 0) and (not mobiles or len(mobiles) == 0):
        return {"success": False, "error": "Invalid input: provide at least one email or mobile", "result": None}

    # 2. Load all context data and select context(s) based on context_id from environment variable
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

    # 3. Validate entity references exist in context (CRITICAL!)
    # For this tool (batch get user id by email/mobile), there are no specific entity references like calendar/event.
    # We will search across the provided context(s) for user-like records.

    # Helper functions
    def normalize_email(e):
        try:
            return e.strip().lower()
        except Exception:
            return str(e).strip().lower()

    def normalize_mobile(m):
        if not isinstance(m, str):
            m = str(m)
        s = m.strip()
        # Keep leading '+', remove spaces, dashes, parentheses
        sign = '+' if s.startswith('+') else ''
        digits = ''.join(ch for ch in s if ch.isdigit())
        return sign + digits

    def derive_status(record):
        # Default status
        status_val = record.get("status")
        is_resigned = record.get("is_resigned")
        # Interpret common patterns
        status_str = "active"
        if isinstance(status_val, str):
            if status_val.lower() in ["resigned", "inactive", "left", "terminated"]:
                status_str = "resigned"
        elif isinstance(status_val, int):
            # assume 0 active, 1 resigned
            status_str = "resigned" if status_val != 0 else "active"
        elif isinstance(status_val, bool):
            # True could mean active, but ambiguous; ignore
            pass
        if isinstance(is_resigned, bool):
            if is_resigned:
                status_str = "resigned"
        return status_str

    def collect_candidates(obj, candidates):
        # Recursively traverse to find user-like records
        if isinstance(obj, dict):
            # Identify candidate if dictionary has at least one contact field and at least one id field
            contact_fields_present = any(k in obj for k in ["email", "emails", "work_email", "mobile", "mobiles", "phone", "phone_number", "phones"])
            id_fields_present = any(k in obj for k in ["user_id", "open_id", "union_id"])
            if contact_fields_present and (id_fields_present or any(k in obj for k in ["status", "is_resigned"])):
                candidates.append(obj)
            for v in obj.values():
                collect_candidates(v, candidates)
        elif isinstance(obj, list):
            for item in obj:
                collect_candidates(item, candidates)

    # Build indexes for fast lookup
    def build_indexes(contexts):
        email_index = {}
        mobile_index = {}
        candidates = []
        for ctx in contexts:
            collect_candidates(ctx, candidates)
        # Index emails
        for rec in candidates:
            emails_in_rec = []
            if "email" in rec and isinstance(rec.get("email"), str):
                emails_in_rec.append(rec["email"])
            if "work_email" in rec and isinstance(rec.get("work_email"), str):
                emails_in_rec.append(rec["work_email"])
            if "emails" in rec and isinstance(rec.get("emails"), list):
                emails_in_rec.extend([e for e in rec["emails"] if isinstance(e, str)])
            # Build email index
            for e in emails_in_rec:
                ne = normalize_email(e)
                if ne not in email_index:
                    email_index[ne] = rec

            # Index mobiles/phones
            mobiles_in_rec = []
            for key in ["mobile", "phone", "phone_number"]:
                if key in rec and isinstance(rec.get(key), str):
                    mobiles_in_rec.append(rec[key])
            if "mobiles" in rec and isinstance(rec.get("mobiles"), list):
                mobiles_in_rec.extend([m for m in rec["mobiles"] if isinstance(m, str)])
            if "phones" in rec and isinstance(rec.get("phones"), list):
                mobiles_in_rec.extend([m for m in rec["phones"] if isinstance(m, str)])
            for m in mobiles_in_rec:
                nm = normalize_mobile(m)
                if nm not in mobile_index:
                    mobile_index[nm] = rec

        return email_index, mobile_index

    # Prepare contexts list
    if isinstance(context_data, list):
        contexts_list = [ctx for ctx in context_data if isinstance(ctx, dict)]
    elif isinstance(context_data, dict):
        contexts_list = [context_data]
    else:
        contexts_list = []

    email_index, mobile_index = build_indexes(contexts_list)

    # 4. Perform operation (list/create/update/delete) using path-based functions
    # This tool is a query operation: simulate fetching user ids by emails/mobiles

    def build_result_item(input_value, kind, record):
        # kind: "email" or "mobile"
        if record is None:
            item = {
                kind: input_value,
                "id": None if user_id_type else None,
                "user_id": None,
                "open_id": None,
                "union_id": None,
                "status": "not_found"
            }
            return item

        status_str = derive_status(record)
        # Filter resigned if requested
        if status_str == "resigned" and not include_resigned:
            item = {
                kind: input_value,
                "id": None if user_id_type else None,
                "user_id": None,
                "open_id": None,
                "union_id": None,
                "status": "not_found"
            }
            return item

        item = {
            kind: input_value,
            "user_id": record.get("user_id"),
            "open_id": record.get("open_id"),
            "union_id": record.get("union_id"),
            "status": status_str
        }
        # Provide requested id as 'id' if specified
        if user_id_type:
            item["id"] = record.get(user_id_type)
        else:
            item["id"] = None
        return item

    results = []

    # Process emails
    for e in emails:
        ne = normalize_email(e)
        rec = email_index.get(ne)
        results.append(build_result_item(e, "email", rec))

    # Process mobiles
    for m in mobiles:
        nm = normalize_mobile(m)
        rec = mobile_index.get(nm)
        results.append(build_result_item(m, "mobile", rec))

    api_result = {
        "requested_id_type": user_id_type if user_id_type else None,
        "include_resigned": include_resigned,
        "users": results
    }

    # 5. Save context if modified - not applicable for query operation; no changes made.

    # 6. Format and return response
    return {
        "success": True,
        "error": None,
        "result": {
            "meta": None,
            "content": [
                {
                    "type": "text",
                    "text": json.dumps(api_result,ensure_ascii=False)
                }
            ],
            "isError": False
        }
    }