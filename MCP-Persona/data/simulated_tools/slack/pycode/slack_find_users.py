from pathlib import Path
import json
import os
from slack.pycode.dynamic_context_handler import (
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
    tool_name = "slack:find_users"
    description = "Find users in a Slack workspace by any criteria - email, name, display name, or other text. Includes optimized email lookup for exact email matches."
    if not isinstance(parameters_used, dict):
        return {"success": False, "error": "Input must be a dictionary", "result": None}
    # Defaults
    exact_match = parameters_used.get("exact_match", False)
    include_bots = parameters_used.get("include_bots", False)
    include_deleted = parameters_used.get("include_deleted", False)
    include_restricted = parameters_used.get("include_restricted", True)
    limit = parameters_used.get("limit", 50)
    search_query = parameters_used.get("search_query")

    # Type checks
    if not isinstance(exact_match, bool):
        return {
            "success": False,
            "error": "exact_match must be a boolean",
            "result": None,
        }
    if not isinstance(include_bots, bool):
        return {
            "success": False,
            "error": "include_bots must be a boolean",
            "result": None,
        }
    if not isinstance(include_deleted, bool):
        return {
            "success": False,
            "error": "include_deleted must be a boolean",
            "result": None,
        }
    if not isinstance(include_restricted, bool):
        return {
            "success": False,
            "error": "include_restricted must be a boolean",
            "result": None,
        }
    if not isinstance(limit, int):
        return {"success": False, "error": "limit must be an integer", "result": None}
    if limit < 1 or limit > 200:
        return {
            "success": False,
            "error": "limit must be between 1 and 200",
            "result": None,
        }
    if (
        search_query is None
        or not isinstance(search_query, str)
        or not search_query.strip()
    ):
        return {
            "success": False,
            "error": "search_query is required and must be a non-empty string",
            "result": None,
        }
    search_query = search_query.strip()

    # 2. Load all context data and select context(s) based on context_id from environment variable
    context_file_path = Path(json.loads(os.environ.get("SLACK_SANDBOX_PATHS"))[0])
    try:
        all_context_data = load_context(context_file_path)
    except Exception as e:
        return {
            "success": False,
            "error": f"Failed to load context: {e}",
            "result": None,
        }

    # Read context_id from environment variable (NOT from input parameters)
    context_id = "all"

    # Determine tool type from tool_name or description to set appropriate default
    tool_name_lower = tool_name.lower() if tool_name else ""
    is_query_tool = any(
        keyword in tool_name_lower
        for keyword in ["get", "list", "query", "search", "batch", "find"]
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
            found = None
            for ctx in all_context_data:
                if isinstance(ctx, dict) and ctx.get("user_id") == context_id:
                    found = ctx
                    break
            if found is None:
                return {
                    "success": False,
                    "error": f"Context ID '{context_id}' not found",
                    "result": None,
                }
            context_data = found
    else:
        # Single context dict
        context_data = all_context_data

    # Helper: extract users from a single context dict
    def extract_users_from_single_context(ctx):
        users_collections = []

        # SPECIAL HANDLING: If ctx is a list (multiple user contexts), extract users directly
        if isinstance(ctx, list) and len(ctx) > 0:
            # This is the multi-user context structure: [{user1_context}, {user2_context}, ...]
            users = []
            for user_ctx in ctx:
                if isinstance(user_ctx, dict):
                    user_info = {
                        "id": user_ctx.get("my_user_id"),
                        "name": user_ctx.get("my_user_name"),
                        "email": user_ctx.get("my_user_email"),
                        "real_name": user_ctx.get("my_user_real_name"),
                        "team_id": user_ctx.get("team_id"),
                    }
                    if user_info.get("id"):  # Only add if we have an ID
                        users.append(user_info)
            if users:
                return users

        # SPECIAL HANDLING: If ctx is a dict (single user context), extract user directly
        if isinstance(ctx, dict):
            user_id = ctx.get("my_user_id")
            if user_id:
                return [
                    {
                        "id": user_id,
                        "name": ctx.get("my_user_name"),
                        "email": ctx.get("my_user_email"),
                        "real_name": ctx.get("my_user_real_name"),
                        "team_id": ctx.get("team_id"),
                    }
                ]

        # Try common paths via dynamic handler
        candidate_paths = [
            "users",
            "members",
            "slack.users",
            "workspace.users",
            "workspaces[*].users",
            "team.members",
            "teams[*].members",
            "profiles",
            "people",
        ]
        for p in candidate_paths:
            try:
                listed = list_entities_by_path(ctx, p, {}, 10000)
                if isinstance(listed, list) and len(listed) > 0:
                    users_collections.append(listed)
            except Exception:
                # Ignore path errors
                pass

        # Fallback deep traversal to locate arrays that look like user objects
        def is_user_like(obj):
            if not isinstance(obj, dict):
                return False
            # Heuristics: presence of id or profile with email or name fields
            has_id = "id" in obj or "user_id" in obj
            profile = obj.get("profile")
            email_like = None
            name_like = None
            if isinstance(profile, dict):
                email_like = profile.get("email")
                name_like = (
                    profile.get("real_name")
                    or profile.get("display_name")
                    or profile.get("first_name")
                    or profile.get("last_name")
                )
            direct_email = obj.get("email")
            direct_name = (
                obj.get("name") or obj.get("real_name") or obj.get("display_name")
            )
            return has_id or email_like or name_like or direct_email or direct_name

        def traverse_collect(node):
            collected = []
            if isinstance(node, list):
                if len(node) > 0 and all(isinstance(x, dict) for x in node):
                    # Check if list of dicts resembles users
                    sample = node[0]
                    if is_user_like(sample):
                        collected.append(node)
                for item in node:
                    collected.extend(traverse_collect(item))
            elif isinstance(node, dict):
                for k, v in node.items():
                    collected.extend(traverse_collect(v))
            return collected

        try:
            users_collections.extend(traverse_collect(ctx))
        except Exception:
            pass

        # Flatten unique by id/email
        flat = []
        seen_keys = set()
        for collection in users_collections:
            if not isinstance(collection, list):
                continue
            for u in collection:
                if not isinstance(u, dict):
                    continue
                key = (
                    u.get("id")
                    or u.get("user_id")
                    or (
                        u.get("profile", {}).get("email")
                        if isinstance(u.get("profile"), dict)
                        else None
                    )
                    or u.get("email")
                )
                if key is None:
                    # use repr hash fallback
                    key = json.dumps(u, sort_keys=True)
                if key in seen_keys:
                    continue
                seen_keys.add(key)
                flat.append(u)
        return flat

    # 3. Validate entity references exist in context (CRITICAL!)
    # For slack:find_users, there are no external references to validate; but ensure we can locate users
    all_users = []

    # Handle both list (multiple users) and dict (single user) context_data
    contexts_to_process = []
    if isinstance(context_data, list):
        contexts_to_process = context_data
    elif isinstance(context_data, dict):
        # Single user context - extract current user
        if context_data.get("my_user_id"):
            contexts_to_process = [context_data]
        else:
            # Fallback: try to extract users from this single context
            contexts_to_process = [context_data]

    for ctx in contexts_to_process:
        if isinstance(ctx, dict):
            users = extract_users_from_single_context(ctx)
            if users:
                all_users.extend(users)

    if not all_users:
        return {"success": False, "error": "No users found in context", "result": None}

    # 4. Perform operation (list/search)
    def normalize_text(val):
        if val is None:
            return ""
        if isinstance(val, (str, int, float)):
            return str(val).strip().lower()
        return ""

    query = search_query.strip()
    query_lower = query.lower()
    tokens = [t for t in query_lower.split() if t]

    def user_matches(u):
        prof = u.get("profile") if isinstance(u.get("profile"), dict) else {}
        # Flags
        if not include_bots:
            if u.get("is_bot") or u.get("bot") or prof.get("is_bot"):
                return False
        if not include_deleted:
            if u.get("deleted") or u.get("is_deleted") or prof.get("deleted"):
                return False
        if not include_restricted:
            if (
                u.get("is_restricted")
                or u.get("is_ultra_restricted")
                or prof.get("is_restricted")
                or prof.get("is_ultra_restricted")
            ):
                return False

        email = normalize_text(prof.get("email") or u.get("email"))
        name = normalize_text(u.get("name"))
        real_name = normalize_text(u.get("real_name") or prof.get("real_name"))
        display_name = normalize_text(u.get("display_name") or prof.get("display_name"))
        first_name = normalize_text(u.get("first_name") or prof.get("first_name"))
        last_name = normalize_text(u.get("last_name") or prof.get("last_name"))
        status_text = normalize_text(u.get("status_text") or prof.get("status_text"))
        fields = [
            email,
            name,
            real_name,
            display_name,
            first_name,
            last_name,
            status_text,
        ]

        if exact_match:
            # Optimized email equality when query looks like an email
            if "@" in query:
                return email == query_lower
            # Otherwise, exact equality on any field
            for f in fields:
                if f and f == query_lower:
                    return True
            return False
        else:
            # Partial, case-insensitive matching across combined fields
            haystack = " ".join([f for f in fields if f])
            if not haystack:
                return False
            # Special handling for domain search like "@company.com"
            if query_lower.startswith("@") and "@" in haystack:
                # Check email domain suffix
                if email.endswith(query_lower) or query_lower in email:
                    return True
            # All tokens must appear
            for t in tokens:
                if t not in haystack:
                    return False
            return True

    # Deduplicate by id or email
    filtered = []
    seen_keys = set()
    for u in all_users:
        try:
            if user_matches(u):
                key = (
                    u.get("id")
                    or u.get("user_id")
                    or (
                        u.get("profile", {}).get("email")
                        if isinstance(u.get("profile"), dict)
                        else None
                    )
                    or u.get("email")
                )
                if key is None:
                    key = json.dumps(u, sort_keys=True)
                if key in seen_keys:
                    continue
                seen_keys.add(key)
                filtered.append(u)
        except Exception:
            # Skip malformed user dicts
            continue

    total_results = len(filtered)
    limited_results = filtered[:limit]

    result_payload = {
        "tool": tool_name,
        "query": search_query,
        "exact_match": exact_match,
        "include_bots": include_bots,
        "include_deleted": include_deleted,
        "include_restricted": include_restricted,
        "limit": limit,
        "total_results": total_results,
        "users": limited_results,
        "has_more": total_results > len(limited_results),
    }

    # 5. Save context if modified (not applicable for query tool; no modifications performed)

    # 6. Format and return response
    return {
        "success": True,
        "error": None,
        "result": {
            "meta": None,
            "content": [
                {
                    "type": "text",
                    "text": json.dumps(result_payload, ensure_ascii=False),
                }
            ],
            "isError": False,
        },
    }
