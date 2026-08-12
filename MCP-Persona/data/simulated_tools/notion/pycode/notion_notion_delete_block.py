def analyze_response_patterns(parameters_used):
    """
    Analyze input parameters and delete a block from Notion simulation data.

    Args:
        parameters_used (dict): Input parameters from the tool call
            - block_id (str): The ID of the block to delete (required)
            - format (str): Response format ('json' or 'markdown')

    Returns:
        dict: Response in the same format as the real Notion API
    """
    import json
    import uuid
    import re
    import os

    global status

    # Extract data from parameters
    data = parameters_used.get("data", {})
    block_id = data.get("block_id")
    format_type = data.get("format", "markdown")

    # Define file paths
    notion_paths = json.loads(os.environ.get("NOTION_SANDBOX_PATHS"))
    blocks_file = notion_paths[2]
    pages_file = notion_paths[1]

    # Error pattern: block_id is not provided
    if block_id is None:
        status = 0
        error_response = {"error": "Missing required argument: block_id"}
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

    # Load all blocks and find the target block
    blocks_dict = {}
    blocks_list = []
    target_block = None
    parent_page_id = None

    if os.path.exists(blocks_file):
        with open(blocks_file, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line:
                    block_data = json.loads(line)
                    blocks_dict[block_data["block_id"]] = block_data
                    blocks_list.append(block_data)
                    if block_data["block_id"] == block_id:
                        target_block = block_data

    # Find which page contains this block
    pages_dict = {}
    if os.path.exists(pages_file):
        with open(pages_file, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line:
                    page_data = json.loads(line)
                    pages_dict[page_data["page_id"]] = page_data
                    if "blocks" in page_data and block_id in page_data["blocks"]:
                        parent_page_id = page_data["page_id"]

    # Error pattern: block_id not found
    if target_block is None:
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

    # Perform deletion: remove from blocks.jsonl and remove from page's blocks list
    status = 1

    # Rewrite blocks.jsonl without the deleted block
    if os.path.exists(blocks_file):
        with open(blocks_file, "w", encoding="utf-8") as f:
            for block in blocks_list:
                if block["block_id"] != block_id:
                    f.write(json.dumps(block, ensure_ascii=False) + "\n")

    # Remove block_id from parent page's blocks list
    if parent_page_id and parent_page_id in pages_dict:
        page_data = pages_dict[parent_page_id]
        if "blocks" in page_data and block_id in page_data["blocks"]:
            page_data["blocks"].remove(block_id)

            # Rewrite pages.jsonl with updated page
            all_pages = []
            if os.path.exists(pages_file):
                with open(pages_file, "r", encoding="utf-8") as f:
                    for line in f:
                        line = line.strip()
                        if line:
                            page = json.loads(line)
                            if page["page_id"] == parent_page_id:
                                page = page_data
                            all_pages.append(page)

            with open(pages_file, "w", encoding="utf-8") as f:
                for page in all_pages:
                    f.write(json.dumps(page, ensure_ascii=False) + "\n")

    # Validate format enum
    if format_type not in ["json", "markdown"]:
        format_type = "markdown"

    # Get current timestamp
    from datetime import datetime

    current_time = datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%S.000Z")

    # Build response
    if format_type == "json":
        # Build JSON format response matching Notion API structure
        block_type = target_block.get("type", "paragraph")
        content = target_block.get("content", "")

        # Base block structure
        notion_block = {
            "object": "block",
            "id": block_id,
            "parent": {
                "type": "page_id",
                "page_id": parent_page_id if parent_page_id else "",
            },
            "created_time": "2025-10-30T06:04:00.000Z",
            "last_edited_time": current_time,
            "created_by": {
                "object": "user",
                "id": "29cd872b-594c-8172-b723-0002706e35a6",
            },
            "last_edited_by": {
                "object": "user",
                "id": "9cc4dcba-69ba-4878-82a4-5cf434afcb98",
            },
            "has_children": False,
            "archived": True,
            "in_trash": True,
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
        elif block_type == "child_page":
            notion_block[block_type] = {"title": ""}
        else:
            # Unsupported or unknown type
            notion_block["type"] = "unsupported"
            notion_block["unsupported"] = {}

        notion_block["request_id"] = str(uuid.uuid4())
        result_text = json.dumps(notion_block, indent=2, ensure_ascii=False)

    else:  # markdown format
        # Build markdown format response for readable display
        block_type = target_block.get("type", "unknown")
        content = target_block.get("content", "")

        lines = []
        lines.append(f"# Block Deleted Successfully")
        lines.append("")
        lines.append(f"**Block ID:** `{block_id}`")
        lines.append("")
        lines.append(f"**Type:** {block_type}")
        lines.append("")
        lines.append(f"**Status:** Archived & Moved to Trash")
        lines.append("")

        if content:
            lines.append(f"**Content:** {content}")
            lines.append("")

        if parent_page_id:
            lines.append(f"**Parent Page:** `{parent_page_id}`")
            lines.append("")

        lines.append("*This block has been deleted and is no longer accessible.*")

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
