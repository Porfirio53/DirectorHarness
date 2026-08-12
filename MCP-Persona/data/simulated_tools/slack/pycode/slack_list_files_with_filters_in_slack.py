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
    allowed_keys = {
        "channel": str,
        "count": str,
        "page": str,
        "show_files_hidden_by_limit": bool,
        "ts_from": int,
        "ts_to": int,
        "types": str,
        "user": str,
    }
    if parameters_used is None:
        parameters_used = {}
    if not isinstance(parameters_used, dict):
        return {
            "success": False,
            "error": "parameters_used must be a dict",
            "result": None,
        }
    for key in parameters_used.keys():
        if key not in allowed_keys:
            return {
                "success": False,
                "error": f"Unknown parameter: {key}",
                "result": None,
            }
    # Type checks
    for key, expected_type in allowed_keys.items():
        if key in parameters_used and parameters_used[key] is not None:
            if expected_type is bool:
                if not isinstance(parameters_used[key], bool):
                    return {
                        "success": False,
                        "error": f"Parameter '{key}' must be a boolean",
                        "result": None,
                    }
            elif expected_type is int:
                if not isinstance(parameters_used[key], int):
                    return {
                        "success": False,
                        "error": f"Parameter '{key}' must be an integer",
                        "result": None,
                    }
            elif expected_type is str:
                if not isinstance(parameters_used[key], str):
                    return {
                        "success": False,
                        "error": f"Parameter '{key}' must be a string",
                        "result": None,
                    }

    # Parse pagination parameters with defaults
    count_str = parameters_used.get("count", "100")
    page_str = parameters_used.get("page", "1")
    try:
        page_size = int(count_str)
    except Exception:
        return {
            "success": False,
            "error": "Parameter 'count' must be a numeric string",
            "result": None,
        }
    try:
        page_num = int(page_str)
    except Exception:
        return {
            "success": False,
            "error": "Parameter 'page' must be a numeric string",
            "result": None,
        }
    if page_size <= 0:
        return {
            "success": False,
            "error": "Parameter 'count' must be > 0",
            "result": None,
        }
    if page_size > 1000:
        page_size = 1000
    if page_num <= 0:
        return {
            "success": False,
            "error": "Parameter 'page' must be >= 1",
            "result": None,
        }

    # Extract other filters
    channel_filter = parameters_used.get("channel")
    user_filter = parameters_used.get("user")
    ts_from = parameters_used.get("ts_from")
    ts_to = parameters_used.get("ts_to")
    types_filter_raw = parameters_used.get("types", "all")
    show_hidden = parameters_used.get("show_files_hidden_by_limit", False)

    # Normalize types filter
    if isinstance(types_filter_raw, str):
        types_filter = [
            t.strip().lower() for t in types_filter_raw.split(",") if t.strip()
        ]
        if not types_filter:
            types_filter = ["all"]
    else:
        types_filter = ["all"]

    # 2. Load all context data and select context(s) based on context_id from environment variable
    context_file_path = Path(json.loads(os.environ.get("SLACK_SANDBOX_PATHS"))[0])
    all_context_data = load_context(
        context_file_path
    )  # Returns Dict[{context_id}: {context_dict}] or List[Dict]

    # Determine tool type from tool_name or description to set appropriate default
    tool_name = "slack:list_files_with_filters_in_slack"
    tool_name_lower = tool_name.lower() if tool_name else ""
    is_query_tool = any(
        keyword in tool_name_lower
        for keyword in ["get", "list", "query", "search", "batch"]
    )

    # Read context_id from environment variable (NOT from input parameters)
    context_id = os.environ.get("SLACK_CONTEXT_ID")

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

    # Helper to normalize to a list of context dicts
    def as_context_list(ctx):
        if isinstance(ctx, list):
            return [c for c in ctx if isinstance(c, dict)]
        elif isinstance(ctx, dict):
            return [ctx]
        else:
            return []

    contexts_list = as_context_list(context_data)

    # 3. Validate entity references exist in context (CRITICAL!)
    # Validate channel reference if provided: check if exists in any context
    def entity_exists_in_any_context(entity_id, candidate_paths):
        for ctx in contexts_list:
            for p in candidate_paths:
                try:
                    ent = get_entity_by_path(ctx, p, entity_id)
                except Exception:
                    ent = None
                if ent:
                    return True
        return False

    if channel_filter:
        # Candidate paths where channels might be stored
        channel_paths = ["group_channels", "dm_channels", "channels"]
        if not entity_exists_in_any_context(channel_filter, channel_paths):
            return {
                "success": False,
                "error": f"Invalid channel ID: {channel_filter}",
                "result": None,
            }

    if user_filter:
        # For user validation, check if user exists in any context
        user_found = False
        for ctx in contexts_list:
            if isinstance(ctx, dict) and ctx.get("my_user_id") == user_filter:
                user_found = True
                break
        if not user_found:
            return {
                "success": False,
                "error": f"Invalid user ID: {user_filter}",
                "result": None,
            }

    # 4. Perform operation (list using path-based functions)
    # Collect files from all contexts
    def list_files_from_context(ctx_dict):
        files = []

        # SPECIAL HANDLING: Files are stored inside channels, not in a global files path
        # Try to extract files from group_channels and dm_channels
        for channel_type in ["group_channels", "dm_channels"]:
            try:
                channels = list_entities_by_path(ctx_dict, channel_type, {}, 10000)
                if isinstance(channels, list) and channels:
                    for channel in channels:
                        if isinstance(channel, dict):
                            channel_files = channel.get("files", {})
                            if isinstance(channel_files, dict):
                                # files is a dict: {file_id: file_obj}
                                files.extend(list(channel_files.values()))
            except Exception:
                # Ignore and try next
                pass

        # Fallback: Try standard paths (for backward compatibility)
        for path in ["files"]:
            try:
                listed = list_entities_by_path(ctx_dict, path, {}, 10000)
                if isinstance(listed, list) and listed:
                    files.extend(listed)
            except Exception:
                # Ignore and try next
                pass

        # Deduplicate by id if possible
        seen_ids = set()
        unique_files = []
        for f in files:
            fid = None
            if isinstance(f, dict):
                fid = f.get("id") or f.get("file_id")
            if fid:
                if fid in seen_ids:
                    continue
                seen_ids.add(fid)
            unique_files.append(f)
        return unique_files

    # Filter helpers
    def file_in_channel(file_obj, chan_id):
        if not chan_id or not isinstance(file_obj, dict):
            return True
        # Direct lists
        for key in ["channels", "groups", "ims"]:
            vals = file_obj.get(key)
            if isinstance(vals, list) and chan_id in vals:
                return True
        # Shares structure: shares.public[channel_id], shares.private[group_id]
        shares = file_obj.get("shares")
        if isinstance(shares, dict):
            for scope in ["public", "private"]:
                scope_dict = shares.get(scope)
                if isinstance(scope_dict, dict) and chan_id in scope_dict:
                    return True
        return False

    def file_by_user(file_obj, user_id):
        if not user_id or not isinstance(file_obj, dict):
            return True
        u = (
            file_obj.get("user")
            or file_obj.get("created_by")
            or file_obj.get("uploader")
        )
        return u == user_id

    def file_within_ts(file_obj, ts_from_val, ts_to_val):
        if not isinstance(file_obj, dict):
            return False
        # Candidate timestamp fields
        t = file_obj.get("timestamp")
        if t is None:
            t = file_obj.get("ts")
        if t is None:
            t = file_obj.get("created")
        if t is None:
            t = file_obj.get("created_ts")
        # If still None, exclude if filters provided
        if t is None:
            if ts_from_val is not None or ts_to_val is not None:
                return False
            return True
        try:
            t_int = int(t)
        except Exception:
            return False
        if ts_from_val is not None and t_int < int(ts_from_val):
            return False
        if ts_to_val is not None and t_int > int(ts_to_val):
            return False
        return True

    def file_matches_types(file_obj, categories):
        if not isinstance(file_obj, dict):
            return False
        if not categories or "all" in categories:
            return True
        ftype = (file_obj.get("type") or "").lower()
        filetype = (file_obj.get("filetype") or "").lower()
        mimetype = (file_obj.get("mimetype") or "").lower()
        mode = (file_obj.get("mode") or "").lower()

        def is_image():
            if mimetype.startswith("image/"):
                return True
            if ftype == "image":
                return True
            if filetype in {
                "jpg",
                "jpeg",
                "png",
                "gif",
                "bmp",
                "tiff",
                "svg",
                "webp",
                "heic",
                "heif",
            }:
                return True
            return False

        def is_pdf():
            return mimetype == "application/pdf" or filetype == "pdf"

        def is_snippet():
            if ftype == "snippet" or mode == "snippet":
                return True
            if filetype in {
                "txt",
                "log",
                "py",
                "js",
                "ts",
                "java",
                "go",
                "rb",
                "php",
                "c",
                "cpp",
                "cs",
                "sh",
                "json",
                "yaml",
                "yml",
                "md",
            }:
                # Heuristic: treat common text/code as snippet if explicitly marked
                return ftype == "snippet" or mode == "snippet"
            return False

        def is_spaces():
            # Slack "posts" historically
            return ftype == "post" or filetype == "post"

        def is_gdocs():
            if "application/vnd.google" in mimetype:
                return True
            if ftype in {"gdoc", "gdrive"}:
                return True
            if filetype in {"gdoc", "gsheet", "gslide", "gdraw"}:
                return True
            return False

        def is_zips():
            return filetype in {
                "zip",
                "tar",
                "gz",
                "bz2",
                "xz",
                "7z",
                "rar",
            } or mimetype in {
                "application/zip",
                "application/x-tar",
                "application/gzip",
                "application/x-bzip2",
                "application/x-xz",
                "application/x-7z-compressed",
                "application/vnd.rar",
            }

        type_checks = {
            "images": is_image,
            "pdfs": is_pdf,
            "snippets": is_snippet,
            "spaces": is_spaces,
            "gdocs": is_gdocs,
            "zips": is_zips,
        }
        for cat in categories:
            if cat in type_checks and type_checks[cat]():
                return True
        return False

    def file_hidden_by_limit(file_obj):
        if not isinstance(file_obj, dict):
            return False
        # Heuristic flags
        return bool(
            file_obj.get("is_hidden_by_limit")
            or file_obj.get("hidden_by_limit")
            or file_obj.get("is_hidden")
        )

    # Aggregate and filter
    all_files = []
    for ctx in contexts_list:
        files = list_files_from_context(ctx)
        all_files.extend(files)

    # Apply filters
    filtered = []
    for f in all_files:
        if channel_filter and not file_in_channel(f, channel_filter):
            continue
        if user_filter and not file_by_user(f, user_filter):
            continue
        if not file_within_ts(f, ts_from, ts_to):
            continue
        if not file_matches_types(f, types_filter):
            continue
        if not show_hidden and file_hidden_by_limit(f):
            continue
        # If showing hidden, we can mark as truncated to simulate "truncated file info"
        if show_hidden and file_hidden_by_limit(f) and isinstance(f, dict):
            # Do not mutate original; create a shallow copy with a hint
            f_copy = dict(f)
            f_copy.setdefault("truncated_due_to_limit", True)
            filtered.append(f_copy)
        else:
            filtered.append(f)

    # Pagination
    total = len(filtered)
    pages = (total + page_size - 1) // page_size if page_size > 0 else 0
    start_idx = (page_num - 1) * page_size
    end_idx = start_idx + page_size
    if start_idx >= total:
        page_items = []
    else:
        page_items = filtered[start_idx:end_idx]
    has_more = end_idx < total

    # Build result in a Slack-like format
    result_payload = {
        "ok": True,
        "files": page_items,
        "paging": {
            "count": page_size,
            "page": page_num,
            "pages": pages,
            "total": total,
        },
        "has_more": has_more,
        "filters_applied": {
            "channel": channel_filter,
            "user": user_filter,
            "ts_from": ts_from,
            "ts_to": ts_to,
            "types": types_filter,
            "show_files_hidden_by_limit": show_hidden,
        },
    }

    # 6. Format and return response
    try:
        content_text = json.dumps(result_payload, ensure_ascii=False)
    except Exception:
        # Fallback: ensure serialization
        content_text = json.dumps(
            {
                "ok": True,
                "error": "Serialization issue with files; counts only",
                "total": total,
                "has_more": has_more,
            }
        )

    return {
        "success": True,
        "error": None,
        "result": {
            "meta": None,
            "content": [{"type": "text", "text": content_text}],
            "isError": False,
        },
    }
