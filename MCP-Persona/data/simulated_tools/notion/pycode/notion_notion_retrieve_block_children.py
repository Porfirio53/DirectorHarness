import os
def analyze_response_patterns(parameters_used):
    """
    Analyze input parameters and return simulated block children response.

    Args:
        parameters_used (dict): Input parameters from the tool call
            - block_id (str): The ID of the block (required)
            - start_cursor (str, optional): Pagination start cursor
            - page_size (int): Number of results per page (max 100)
            - format (str): Response format ('json' or 'markdown')

    Returns:
        dict: Response in the same format as the real Notion API
    """
    import json
    import uuid
    import re

    global status

    # Extract data from parameters
    data = parameters_used.get("data", {})
    block_id = data.get("block_id")
    start_cursor = data.get("start_cursor")
    page_size = data.get("page_size")
    format_type = data.get("format", "markdown")

    # Load blocks and pages data
    notion_paths = json.loads(os.environ.get("NOTION_SANDBOX_PATHS"))
    blocks_dict = {}
    try:
        blocks_file = notion_paths[2]
        with open(blocks_file, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line:
                    block_data = json.loads(line)
                    blocks_dict[block_data["block_id"]] = block_data
    except FileNotFoundError:
        blocks_dict = {}

    pages_dict = {}
    try:
        pages_file = notion_paths[1]
        with open(pages_file, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line:
                    page_data = json.loads(line)
                    pages_dict[page_data["page_id"]] = page_data
    except FileNotFoundError:
        pages_dict = {}

    # Error pattern: block_id is not provided
    if block_id is None:
        status = 0
        error_response = {
            "object": "error",
            "status": 400,
            "code": "validation_error",
            "message": "path failed validation: path.block_id is required",
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

    # Error pattern: block_id format is invalid (not a valid UUID)
    uuid_pattern = (
        r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$"
    )
    if not isinstance(block_id, str) or not re.match(uuid_pattern, block_id):
        status = 0
        error_response = {
            "object": "error",
            "status": 400,
            "code": "validation_error",
            "message": f'path failed validation: path.block_id should be a valid uuid, instead was `"{block_id}"`.',
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

    # Error pattern: start_cursor is not a string
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

    # Error pattern: start_cursor is not a valid UUID
    if start_cursor is not None and not re.match(uuid_pattern, start_cursor):
        status = 0
        error_response = {
            "object": "error",
            "status": 400,
            "code": "validation_error",
            "message": f'query failed validation: query.start_cursor should be a valid uuid or `undefined`, instead was `"{start_cursor}"`.',
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

    # Error pattern: block_id not found
    if block_id not in pages_dict and block_id not in blocks_dict:
        status = 0
        error_response = {
            "object": "error",
            "status": 404,
            "code": "object_not_found",
            "message": f"Could not find block with ID: {block_id}. Make sure the relevant pages and databases are shared with your integration.",
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

    # Get children blocks
    children_blocks = []

    # Check if block_id is a page
    if block_id in pages_dict:
        page_data = pages_dict[block_id]
        block_ids = page_data.get("blocks", [])

        for bid in block_ids:
            if bid in blocks_dict:
                children_blocks.append(blocks_dict[bid])
    else:
        # For regular blocks, return their sub_blocks if any
        if block_id in blocks_dict:
            block_data = blocks_dict[block_id]
            sub_block_ids = block_data.get("sub_blocks", [])
            for bid in sub_block_ids:
                if bid in blocks_dict:
                    children_blocks.append(blocks_dict[bid])

    # Handle pagination
    start_index = 0
    if start_cursor:
        # Try to parse cursor as index (simplified approach)
        # In real API, cursor is an opaque token
        try:
            # Check if cursor matches any child block id
            cursor_found = False
            for i, block in enumerate(children_blocks):
                if block["block_id"] == start_cursor:
                    start_index = i + 1
                    cursor_found = True
                    break

            if not cursor_found:
                # Invalid cursor, return empty
                start_index = len(children_blocks)
        except Exception:
            start_index = len(children_blocks)

    # Normalize page_size
    if page_size is None:
        page_size = 50  # Default
    elif not isinstance(page_size, int) or page_size <= 0:
        page_size = 50  # Use default for invalid values

    # Handle negative page_size (special case from failed records)
    if isinstance(page_size, int) and page_size < 0:
        status = 1
        # Return empty result with next_cursor pointing to first block
        next_cursor = children_blocks[0]["block_id"] if children_blocks else None
        result_data = {
            "object": "list",
            "results": [],
            "next_cursor": next_cursor,
            "has_more": len(children_blocks) > 0,
            "type": "block",
            "block": {},
            "request_id": str(uuid.uuid4()),
        }
        result_text = json.dumps(result_data, indent=2, ensure_ascii=False)
        return {
            "parameters_used": parameters_used,
            "success": True,
            "error": None,
            "result": {
                "meta": None,
                "content": [
                    {
                        "type": "text",
                        "text": result_text,
                        "annotations": None,
                        "meta": None,
                    }
                ],
                "structuredContent": None,
                "isError": False,
            },
        }

    # Cap page_size at maximum
    actual_page_size = min(page_size, 100)

    # Get paginated blocks
    end_index = start_index + actual_page_size
    paginated_blocks = children_blocks[start_index:end_index]

    # Determine pagination metadata
    has_more = end_index < len(children_blocks)
    next_cursor = (
        paginated_blocks[-1]["block_id"] if paginated_blocks and has_more else None
    )

    # Validate format enum
    if format_type not in ["json", "markdown"]:
        format_type = "markdown"

    # Build response
    status = 1

    if format_type == "json":
        # Build JSON format response matching Notion API structure
        results = []
        for block in paginated_blocks:
            block_type = block.get("type", "paragraph")
            content = block.get("content", "")
            bid = block.get("block_id", "")

            # Base block structure
            notion_block = {
                "object": "block",
                "id": bid,
                "parent": {"type": "page_id", "page_id": block_id},
                "created_time": "2025-10-30T06:04:00.000Z",
                "last_edited_time": "2025-10-30T06:04:00.000Z",
                "created_by": {
                    "object": "user",
                    "id": "29cd872b-594c-8172-b723-0002706e35a6",
                },
                "last_edited_by": {
                    "object": "user",
                    "id": "29cd872b-594c-8172-b723-0002706e35a6",
                },
                "has_children": False,
                "archived": False,
                "in_trash": False,
                "type": block_type,
            }

            # Build type-specific content
            if block_type == "paragraph":
                notion_block[block_type] = {
                    "rich_text": (
                        [
                            {
                                "type": "text",
                                "text": {"content": content, "link": None},
                                "annotations": {
                                    "bold": False,
                                    "italic": False,
                                    "strikethrough": False,
                                    "underline": False,
                                    "code": False,
                                    "color": "default",
                                },
                                "plain_text": content,
                                "href": None,
                            }
                        ]
                        if content
                        else []
                    ),
                    "color": "default",
                }
            elif block_type == "heading_1":
                notion_block[block_type] = {
                    "rich_text": [
                        {
                            "type": "text",
                            "text": {"content": content, "link": None},
                            "annotations": {
                                "bold": False,
                                "italic": False,
                                "strikethrough": False,
                                "underline": False,
                                "code": False,
                                "color": "default",
                            },
                            "plain_text": content,
                            "href": None,
                        }
                    ],
                    "is_toggleable": False,
                    "color": "default",
                }
            elif block_type == "heading_2":
                notion_block[block_type] = {
                    "rich_text": [
                        {
                            "type": "text",
                            "text": {"content": content, "link": None},
                            "annotations": {
                                "bold": False,
                                "italic": False,
                                "strikethrough": False,
                                "underline": False,
                                "code": False,
                                "color": "default",
                            },
                            "plain_text": content,
                            "href": None,
                        }
                    ],
                    "is_toggleable": False,
                    "color": "default",
                }
            elif block_type in ["bulleted_list_item", "numbered_list_item"]:
                notion_block[block_type] = {
                    "rich_text": [
                        {
                            "type": "text",
                            "text": {"content": content, "link": None},
                            "annotations": {
                                "bold": False,
                                "italic": False,
                                "strikethrough": False,
                                "underline": False,
                                "code": False,
                                "color": "default",
                            },
                            "plain_text": content,
                            "href": None,
                        }
                    ],
                    "color": "default",
                }
            else:
                # Unsupported or unknown type
                notion_block["type"] = "unsupported"
                notion_block["unsupported"] = {}

            results.append(notion_block)

        result_data = {
            "object": "list",
            "results": results,
            "next_cursor": next_cursor,
            "has_more": has_more,
            "type": "block",
            "block": {},
            "request_id": str(uuid.uuid4()),
        }
        result_text = json.dumps(result_data, indent=2, ensure_ascii=False)

    else:  # markdown format
        # Build markdown format response for readable display
        if not paginated_blocks:
            result_text = (
                "# No blocks found\n\nNo children blocks found for this block."
            )
        else:
            lines = []
            lines.append(
                f"# Block Children ({start_index + 1}-{start_index + len(paginated_blocks)})"
            )
            lines.append("")
            lines.append(f"**Total blocks shown:** {len(paginated_blocks)}")
            lines.append("")

            for idx, block in enumerate(paginated_blocks, start=start_index + 1):
                block_type = block.get("type", "unknown")
                content = block.get("content", "")

                # Format based on block type
                if block_type == "heading_1":
                    lines.append(f"## {idx}. {content}")
                elif block_type == "heading_2":
                    lines.append(f"### {idx}. {content}")
                elif block_type == "paragraph":
                    lines.append(f"**{idx}.** {content}")
                elif block_type == "bulleted_list_item":
                    lines.append(f"**{idx}.** • {content}")
                elif block_type == "numbered_list_item":
                    lines.append(f"**{idx}.** {idx}. {content}")
                else:
                    lines.append(f"**{idx}.** [{block_type}] {content}")

                lines.append(f"   - **Block ID:** `{block.get('block_id', 'N/A')}`")
                lines.append("")

            if has_more:
                lines.append(f"**Next page cursor:** `{next_cursor}`")
                lines.append("")
                lines.append(
                    "*More blocks available. Use the cursor to fetch the next page.*"
                )
            else:
                lines.append("*End of block list*")

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
