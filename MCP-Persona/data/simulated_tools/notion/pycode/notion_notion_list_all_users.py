import os
def analyze_response_patterns(parameters_used):
    """
    Analyze input parameters and return simulated user list response.

    Args:
        parameters_used (dict): Input parameters from the tool call
            - page_size (int): Number of users to retrieve (max 100)
            - start_cursor (str, optional): Pagination start cursor
            - format (str): Response format ('json' or 'markdown')

    Returns:
        dict: Response in the same format as the real Notion API
    """
    import json
    import uuid

    global status

    # Extract data from parameters
    data = parameters_used.get("data", {})
    page_size = data.get("page_size")
    start_cursor = data.get("start_cursor")
    format_type = data.get("format", "markdown")

    # Load user data from JSONL file
    try:
        users_file = json.loads(os.environ.get("NOTION_SANDBOX_PATHS"))[0]
        users_data = []
        with open(users_file, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line:
                    user_data = json.loads(line)
                    users_data.append(user_data)
    except FileNotFoundError:
        users_data = []

    # Error pattern: start_cursor is not a string (type validation error)
    if start_cursor is not None and not isinstance(start_cursor, str):
        status = 0
        error_response = {
            "object": "error",
            "status": 400,
            "code": "validation_error",
            "message": f"query failed validation: query.start_cursor should be a string or `undefined`, instead was `{start_cursor}`.",
            "request_id": str(uuid.uuid4()),
        }
        error_text = json.dumps(error_response, ensure_ascii=False)
        error_text_escaped = error_text.replace("\\", "\\\\").replace("'", "\\'")
        return {
            "parameters_used": parameters_used,
            "success": False,
            "error": None,
            "result": f"meta=None content=[TextContent(type='text', text='{error_text_escaped}', annotations=None, meta=None)] structuredContent=None isError=False",
        }

    # Error pattern: page_size is negative
    if isinstance(page_size, int) and page_size < 0:
        status = 0
        error_response = {
            "object": "error",
            "status": 503,
            "code": "service_unavailable",
            "message": "Public API service is temporarily unavailable, please try again later.",
            "request_id": str(uuid.uuid4()),
        }
        error_text = json.dumps(error_response, ensure_ascii=False)
        error_text_escaped = error_text.replace("\\", "\\\\").replace("'", "\\'")
        return {
            "parameters_used": parameters_used,
            "success": False,
            "error": None,
            "result": f"meta=None content=[TextContent(type='text', text='{error_text_escaped}', annotations=None, meta=None)] structuredContent=None isError=False",
        }

    # Validate format enum (default to markdown if invalid)
    if format_type not in ["json", "markdown"]:
        format_type = "markdown"

    # Parse start_cursor to determine starting index
    start_index = 0
    if start_cursor:
        try:
            start_index = int(start_cursor)
        except ValueError:
            # Invalid cursor format, return empty result
            start_index = len(users_data)

    # Normalize page_size
    if page_size is None:
        page_size = 50  # Default page size
    elif not isinstance(page_size, int) or page_size <= 0:
        page_size = 50  # Use default for invalid values

    # Cap page_size at maximum (even if API allows more, we limit to 100)
    actual_page_size = min(page_size, 100)

    # Get paginated users
    end_index = start_index + actual_page_size
    paginated_users = users_data[start_index:end_index]

    # Determine pagination metadata
    has_more = end_index < len(users_data)
    next_cursor = str(end_index) if has_more else None

    # Build response based on format
    status = 1

    if format_type == "json":
        # Build JSON format response matching Notion API structure
        results = []
        for user in paginated_users:
            notion_user = {
                "object": "user",
                "id": user.get("user_id", ""),
                "name": user.get("name", ""),
                "avatar_url": None,
                "type": "person",
                "person": {"email": user.get("email", "")},
            }
            results.append(notion_user)

        result_data = {
            "object": "list",
            "results": results,
            "next_cursor": next_cursor,
            "has_more": has_more,
            "type": "user",
            "user": {},
            "request_id": str(uuid.uuid4()),
        }
        result_text = json.dumps(result_data, indent=2, ensure_ascii=False)

    else:  # markdown format
        # Build markdown format response for readable display
        if not paginated_users:
            result_text = "# No users found\n\nNo users match your query."
        else:
            lines = []
            lines.append(
                f"# Workspace Users ({start_index + 1}-{start_index + len(paginated_users)})"
            )
            lines.append("")
            lines.append(f"**Total users shown:** {len(paginated_users)}")
            lines.append("")

            for idx, user in enumerate(paginated_users, start=start_index + 1):
                lines.append(f"## {idx}. {user.get('name', 'Unknown')}")
                lines.append("")
                lines.append(f"- **Email:** {user.get('email', 'N/A')}")
                lines.append(f"- **User ID:** {user.get('user_id', 'N/A')}")
                lines.append(f"- **Type:** Person")
                lines.append("")

            if has_more:
                lines.append(f"**Next page cursor:** `{next_cursor}`")
                lines.append("")
                lines.append(
                    "*More users available. Use the cursor to fetch the next page.*"
                )
            else:
                lines.append("*End of user list*")

            result_text = "\n".join(lines)

    return {
        "parameters_used": parameters_used,
        "success": True,
        "error": None,
        "result": {
            "meta": None,
            "content": [
                {"type": "text", "text": result_text, "annotations": None, "meta": None}
            ],
            "structuredContent": None,
            "isError": False,
        },
    }
