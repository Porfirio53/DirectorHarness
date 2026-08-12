import os
def analyze_response_patterns(parameters_used):
    """
    Retrieve a list of unresolved comments from a Notion page or block.

    This function simulates the Notion API for retrieving comments by reading
    from external JSONL data files. It supports both JSON and Markdown response formats.

    Args:
        parameters_used: Dictionary containing:
            - data.block_id: The ID of the block or page (required)
            - data.start_cursor: Pagination cursor (optional)
            - data.page_size: Number of comments to return, max 100 (optional)
            - data.format: Response format, 'json' or 'markdown' (default: 'markdown')

    Returns:
        Dictionary with response structure matching Notion API
    """
    import json
    import uuid
    import re

    global status

    data = parameters_used.get("data", {})
    block_id = data.get("block_id")
    start_cursor = data.get("start_cursor")
    page_size = data.get("page_size", 100)
    format_type = data.get("format", "markdown")

    notion_paths = json.loads(os.environ.get("NOTION_SANDBOX_PATHS"))
    comments_file = notion_paths[3]
    users_file = notion_paths[0]

    # Error: Missing required block_id
    if block_id is None or block_id == "":
        status = 0
        error_response = {
            "object": "error",
            "status": 400,
            "code": "validation_error",
            "message": "Missing required argument: block_id",
            "request_id": str(uuid.uuid4()),
        }
        return {
            "parameters_used": parameters_used,
            "success": False,
            "error": error_response,
            "result": {
                "meta": None,
                "content": [
                    {
                        "type": "text",
                        "text": json.dumps(error_response, ensure_ascii=False),
                        "annotations": None,
                        "meta": None,
                    }
                ],
                "structuredContent": None,
                "isError": True,
            },
        }

    # Normalize block_id (add hyphens if missing)
    normalized_block_id = block_id
    if "-" not in block_id and len(block_id) == 32:
        normalized_block_id = f"{block_id[0:8]}-{block_id[8:12]}-{block_id[12:16]}-{block_id[16:20]}-{block_id[20:32]}"

    # Validate UUID format
    uuid_pattern = (
        r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$"
    )
    if not re.match(uuid_pattern, normalized_block_id):
        status = 0
        error_response = {
            "object": "error",
            "status": 400,
            "code": "validation_error",
            "message": f"The block_id provided is invalid: {block_id}",
            "request_id": str(uuid.uuid4()),
        }
        return {
            "parameters_used": parameters_used,
            "success": False,
            "error": error_response,
            "result": {
                "meta": None,
                "content": [
                    {
                        "type": "text",
                        "text": json.dumps(error_response, ensure_ascii=False),
                        "annotations": None,
                        "meta": None,
                    }
                ],
                "structuredContent": None,
                "isError": True,
            },
        }

    # Error: Invalid start_cursor
    if start_cursor:
        cursor_uuid_pattern = r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$"
        if not re.match(cursor_uuid_pattern, start_cursor):
            status = 0
            error_response = {
                "object": "error",
                "status": 400,
                "code": "validation_error",
                "message": f"The start_cursor provided is invalid: {start_cursor}",
                "request_id": str(uuid.uuid4()),
            }
            return {
                "parameters_used": parameters_used,
                "success": False,
                "error": error_response,
                "result": {
                    "meta": None,
                    "content": [
                        {
                            "type": "text",
                            "text": json.dumps(error_response, ensure_ascii=False),
                            "annotations": None,
                            "meta": None,
                        }
                    ],
                    "structuredContent": None,
                    "isError": True,
                },
            }

    # Validate page_size
    if page_size < 1 or page_size > 100:
        page_size = min(max(page_size, 1), 100)

    # Load user data for mention resolution
    users_dict = {}
    try:
        with open(users_file, "r", encoding="utf-8") as f:
            for line in f:
                if line.strip():
                    user_data = json.loads(line)
                    users_dict[user_data.get("name", "")] = user_data
    except FileNotFoundError:
        pass

    # Load comments from external data source
    all_comments = []
    try:
        with open(comments_file, "r", encoding="utf-8") as f:
            for line in f:
                if line.strip():
                    comment = json.loads(line)
                    # Match if block_id equals parent.page_id or parent.block_id
                    parent = comment.get("parent", {})
                    page_id = parent.get("page_id", "")
                    block_id_in_comment = parent.get("block_id", "")

                    if (
                        page_id == normalized_block_id
                        or block_id_in_comment == normalized_block_id
                    ):
                        all_comments.append(comment)
    except FileNotFoundError:
        pass

    # Error: Block not found (no comments and block doesn't exist in data)
    if len(all_comments) == 0 and normalized_block_id not in [
        c.get("parent", {}).get("page_id", "") for c in all_comments
    ] + [c.get("parent", {}).get("block_id", "") for c in all_comments]:
        # Check if this is a non-existent block ID (all zeros or similar)
        if normalized_block_id == "00000000-0000-0000-0000-000000000000":
            status = 0
            error_response = {
                "object": "error",
                "status": 404,
                "code": "object_not_found",
                "message": f"Could not find block with ID: {block_id}. Make sure the relevant pages and databases are shared with your integration.",
                "request_id": str(uuid.uuid4()),
            }
            return {
                "parameters_used": parameters_used,
                "success": False,
                "error": error_response,
                "result": {
                    "meta": None,
                    "content": [
                        {
                            "type": "text",
                            "text": json.dumps(error_response, ensure_ascii=False),
                            "annotations": None,
                            "meta": None,
                        }
                    ],
                    "structuredContent": None,
                    "isError": True,
                },
            }

    status = 1

    # Handle pagination with start_cursor
    if start_cursor:
        # Find the position of the cursor in the results
        cursor_index = -1
        for i, comment in enumerate(all_comments):
            comment_id = comment.get("discussion_id", "")
            if comment_id == start_cursor:
                cursor_index = i
                break

        # If cursor not found, return validation error
        if cursor_index == -1:
            status = 0
            error_response = {
                "object": "error",
                "status": 400,
                "code": "validation_error",
                "message": f"The start_cursor provided is invalid: {start_cursor}",
                "request_id": str(uuid.uuid4()),
            }
            return {
                "parameters_used": parameters_used,
                "success": False,
                "error": error_response,
                "result": {
                    "meta": None,
                    "content": [
                        {
                            "type": "text",
                            "text": json.dumps(error_response, ensure_ascii=False),
                            "annotations": None,
                            "meta": None,
                        }
                    ],
                    "structuredContent": None,
                    "isError": True,
                },
            }

        # Slice results after the cursor
        all_comments = all_comments[cursor_index + 1 :]

    # Apply page_size limit
    comments_page = all_comments[:page_size]
    has_more = len(all_comments) > page_size

    # Generate next_cursor if there are more results
    next_cursor = None
    if has_more and len(comments_page) > 0:
        last_comment = comments_page[-1]
        next_cursor = last_comment.get("discussion_id", "")

    # Build Notion API comment objects
    results = []
    default_user_id = "9cc4dcba-69ba-4878-82a4-5cf434afcb98"

    for comment in comments_page:
        discussion_id = comment.get("discussion_id", str(uuid.uuid4()))
        parent_info = comment.get("parent", {})
        page_id = parent_info.get("page_id", normalized_block_id)
        created_time = comment.get("created_time", "")
        created_by_name = comment.get("created_by", "MCP-test")
        text_content = comment.get("text", "")

        # Find user info from users.jsonl
        user_info = users_dict.get(created_by_name, {})
        user_id = user_info.get("user_id", default_user_id)
        user_email = user_info.get("email", "")
        user_persona = user_info.get("persona", "")

        # Generate comment_id (simulate as UUID based on discussion_id + index)
        comment_id = str(uuid.uuid4())

        # Build rich_text array from plain text
        rich_text_array = []
        if text_content:
            # Simple text type
            rich_text_array.append(
                {
                    "type": "text",
                    "text": {"content": text_content, "link": None},
                    "annotations": {
                        "bold": False,
                        "italic": False,
                        "strikethrough": False,
                        "underline": False,
                        "code": False,
                        "color": "default",
                    },
                    "plain_text": text_content,
                    "href": None,
                }
            )

        # Build Notion comment object
        notion_comment = {
            "object": "comment",
            "id": comment_id,
            "parent": {"type": "page_id", "page_id": page_id},
            "discussion_id": discussion_id,
            "created_time": created_time,
            "last_edited_time": created_time,
            "created_by": {"object": "user", "id": user_id},
            "rich_text": rich_text_array,
            "display_name": {"type": "integration", "resolved_name": "MCP-test"},
        }

        results.append(notion_comment)

    # Build response based on format
    if format_type == "json":
        response_data = {
            "object": "list",
            "results": results,
            "next_cursor": next_cursor,
            "has_more": has_more,
            "type": "comment",
            "comment": {},
            "request_id": str(uuid.uuid4()),
        }
        result_text = json.dumps(response_data, ensure_ascii=False, indent=2)
    else:
        # Markdown format
        lines = ["{", '  "object": "list",', '  "results": [']

        for i, comment in enumerate(results):
            if i > 0:
                lines.append("    },")
            lines.append("    {")
            lines.append(f'      "object": "comment",')
            lines.append(f'      "id": "{comment["id"]}",')
            lines.append(f'      "parent": {{')
            lines.append(f'        "type": "page_id",')
            lines.append(f'        "page_id": "{comment["parent"]["page_id"]}"')
            lines.append("      },")
            lines.append(f'      "discussion_id": "{comment["discussion_id"]}",')
            lines.append(f'      "created_time": "{comment["created_time"]}",')
            lines.append(f'      "last_edited_time": "{comment["last_edited_time"]}",')
            lines.append(f'      "created_by": {{')
            lines.append(f'        "object": "user",')
            lines.append(f'        "id": "{comment["created_by"]["id"]}"')
            lines.append("      },")
            lines.append('      "rich_text": [')

            # Add rich_text items
            for j, rt in enumerate(comment["rich_text"]):
                if j > 0:
                    lines.append("        },")
                lines.append("        {")
                lines.append(f'          "type": "{rt["type"]}",')
                lines.append('          "text": {')
                lines.append(f'            "content": "{rt["text"]["content"]}",')
                lines.append('            "link": null')
                lines.append("          },")
                lines.append('          "annotations": {')
                lines.append('            "bold": false,')
                lines.append('            "italic": false,')
                lines.append('            "strikethrough": false,')
                lines.append('            "underline": false,')
                lines.append('            "code": false,')
                lines.append('            "color": "default"')
                lines.append("          },")
                lines.append(f'          "plain_text": "{rt["plain_text"]}",')
                lines.append('          "href": null')

                if j == len(comment["rich_text"]) - 1:
                    lines.append("        }")
                else:
                    lines.append("        },")

            lines.append("      ],")
            lines.append('      "display_name": {')
            lines.append('        "type": "integration",')
            lines.append('        "resolved_name": "MCP-test"')
            lines.append("      }")

            if i == len(results) - 1:
                lines.append("    }")
            else:
                lines.append("    },")

        lines.extend(
            [
                "  ],",
                f'  "next_cursor": {json.dumps(next_cursor)},',
                f'  "has_more": {str(has_more).lower()},',
                '  "type": "comment",',
                '  "comment": {},',
                f'  "request_id": "{str(uuid.uuid4())}"',
                "}",
            ]
        )

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
