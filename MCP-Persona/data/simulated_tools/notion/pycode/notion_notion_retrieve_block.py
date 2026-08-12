def analyze_response_patterns(parameters_used):
    """
    Analyze input parameters and return simulated block retrieval response.

    Args:
        parameters_used (dict): Input parameters from the tool call
            - block_id (str): The ID of the block to retrieve (required)
            - format (str): Response format ('json' or 'markdown', default 'markdown')

    Returns:
        dict: Response in the same format as the real Notion API
    """
    import json
    import uuid
    import re
    import os
    from datetime import datetime, timedelta

    global status

    # Extract data from parameters
    data = parameters_used.get("data", {})
    block_id = data.get("block_id")
    format_type = data.get("format", "markdown")

    # Load block data from JSONL file
    try:
        blocks_file = json.loads(os.environ.get("NOTION_SANDBOX_PATHS"))[2]
        blocks_data = []
        with open(blocks_file, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line:
                    block_data = json.loads(line)
                    blocks_data.append(block_data)
    except FileNotFoundError:
        blocks_data = []

    # Error pattern 1: Missing required block_id parameter
    if not block_id:
        status = 0
        error_response = {"error": "Missing required argument: block_id"}
        error_text = json.dumps(error_response, ensure_ascii=False)
        error_text_escaped = error_text.replace("\\", "\\\\").replace("'", "\\'")
        return {
            "parameters_used": parameters_used,
            "success": True,
            "error": None,
            "result": f"meta=None content=[TextContent(type='text', text='{error_text_escaped}', annotations=None, meta=None)] structuredContent=None isError=False",
        }

    # Error pattern 2: Invalid UUID format validation
    # Check if block_id matches UUID pattern (8-4-4-4-12 format)
    uuid_pattern = r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$"
    is_valid_uuid_format = bool(re.match(uuid_pattern, block_id.lower()))

    # Specific UUIDs that fail validation based on real cases
    invalid_uuids = [
        "ffffffff-ffff-ffff-ffff-ffffffffffff"  # All f's - validation error
    ]

    if not is_valid_uuid_format or block_id.lower() in invalid_uuids:
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
            "success": True,
            "error": None,
            "result": f"meta=None content=[TextContent(type='text', text='{error_text_escaped}', annotations=None, meta=None)] structuredContent=None isError=False",
        }

    # Error pattern 3: Special case - all zeros UUID returns 404
    if block_id == "00000000-0000-0000-0000-000000000000":
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
            "success": True,
            "error": None,
            "result": f"meta=None content=[TextContent(type='text', text='{error_text_escaped}', annotations=None, meta=None)] structuredContent=None isError=False",
        }

    # Error pattern 4: block_id not found in data source
    matched_block = None
    for block in blocks_data:
        if block.get("block_id") == block_id:
            matched_block = block
            break

    if not matched_block:
        # Block ID not found - return not found error
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
            "success": True,
            "error": None,
            "result": f"meta=None content=[TextContent(type='text', text='{error_text_escaped}', annotations=None, meta=None)] structuredContent=None isError=False",
        }

    # Validate format enum (default to json if invalid, based on real behavior)
    if format_type not in ["json", "markdown"]:
        format_type = "json"  # Notion defaults to json when invalid format is provided

    # Success case - build response based on matched block
    status = 1

    # Generate timestamps
    base_time = datetime(2025, 12, 7, 12, 0, 0)
    created_time = base_time.strftime("%Y-%m-%dT%H:%M:%S.000Z")
    last_edited_time = (base_time + timedelta(hours=6)).strftime(
        "%Y-%m-%dT%H:%M:%S.000Z"
    )

    # Generate user IDs
    created_by_id = "29cd872b-594c-8172-b723-0002706e35a6"
    last_edited_by_id = "9cc4dcba-69ba-4878-82a4-5cf434afcb98"

    # Map block type from JSONL to Notion API format
    block_type = matched_block.get("type", "text")
    block_content = matched_block.get("content", "")

    # Build Notion block object structure
    if format_type == "json":
        # Build JSON format response matching Notion API structure
        notion_block = {
            "object": "block",
            "id": block_id,
            "parent": {"type": "workspace", "workspace": True},
            "created_time": created_time,
            "last_edited_time": last_edited_time,
            "created_by": {"object": "user", "id": created_by_id},
            "last_edited_by": {"object": "user", "id": last_edited_by_id},
            "has_children": False,
            "archived": False,
            "in_trash": False,
            "type": block_type,
            "request_id": str(uuid.uuid4()),
        }

        # Add type-specific content based on block type
        if block_type == "child_page":
            notion_block[block_type] = {"title": block_content}
        elif block_type in ["heading_1", "heading_2", "heading_3"]:
            notion_block[block_type] = {
                "rich_text": [{"type": "text", "text": {"content": block_content}}]
            }
        elif block_type in ["paragraph", "bulleted_list_item", "numbered_list_item"]:
            notion_block[block_type] = {
                "rich_text": [{"type": "text", "text": {"content": block_content}}]
            }
        elif block_type == "to_do":
            notion_block[block_type] = {
                "rich_text": [{"type": "text", "text": {"content": block_content}}],
                "checked": False,
            }
        else:
            # Generic text block
            notion_block["type"] = "text"
            notion_block["text"] = {"content": block_content}

        result_text = json.dumps(notion_block, indent=2, ensure_ascii=False)

    else:  # markdown format
        # Build markdown format response for readable display
        lines = []

        if block_type == "child_page":
            lines.append(f"# 📄 {block_content}")
            lines.append("")
            lines.append(f"**Block ID:** `{block_id}`")
            lines.append("")
            lines.append(f"**Type:** Child Page")
            lines.append("")

        elif block_type == "heading_1":
            lines.append(f"# {block_content}")
            lines.append("")
            lines.append(f"*Block ID: `{block_id}`*")
            lines.append("")

        elif block_type == "heading_2":
            lines.append(f"## {block_content}")
            lines.append("")
            lines.append(f"*Block ID: `{block_id}`*")
            lines.append("")

        elif block_type == "heading_3":
            lines.append(f"### {block_content}")
            lines.append("")
            lines.append(f"*Block ID: `{block_id}`*")
            lines.append("")

        elif block_type in ["bulleted_list_item", "numbered_list_item"]:
            prefix = "-" if block_type == "bulleted_list_item" else "1."
            lines.append(f"{prefix} {block_content}")
            lines.append("")
            lines.append(f"*Block ID: `{block_id}`*")
            lines.append("")

        elif block_type == "paragraph":
            lines.append(f"{block_content}")
            lines.append("")
            lines.append(f"*Block ID: `{block_id}`*")
            lines.append("")

        elif block_type == "to_do":
            lines.append(f"- [ ] {block_content}")
            lines.append("")
            lines.append(f"*Block ID: `{block_id}`*")
            lines.append("")

        else:
            # Generic text block
            lines.append(f"```")
            lines.append(block_content)
            lines.append("```")
            lines.append("")
            lines.append(f"**Block ID:** `{block_id}`")
            lines.append("")
            lines.append(f"**Type:** {block_type}")
            lines.append("")

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
