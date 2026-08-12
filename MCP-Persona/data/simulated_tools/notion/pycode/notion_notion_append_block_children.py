def analyze_response_patterns(parameters_used):
    """
    Analyze input parameters and return simulated block children append response.

    Args:
        parameters_used (dict): Input parameters from the tool call
            - block_id (str): The ID of the parent block (page or block)
            - children (list): Array of block objects to append
            - after (str, optional): Block ID to append after

    Returns:
        dict: Response in the same format as the real Notion API
    """
    import json
    import uuid
    from datetime import datetime, timezone
    import os

    global status

    # Extract data from parameters
    data = parameters_used.get("data", {})
    block_id = data.get("block_id")
    children = data.get("children", [])
    after = data.get("after")

    # Helper function to validate UUID format
    def is_valid_uuid(uuid_str):
        if not uuid_str or not isinstance(uuid_str, str):
            return False
        try:
            uuid_obj = uuid.UUID(uuid_str)
            return str(uuid_obj) == uuid_str
        except ValueError:
            return False

    # Error pattern 1: Missing required parameters
    if not block_id and not children:
        status = 0
        error_response = {"error": "Missing required arguments: block_id and children"}
        error_text = json.dumps(error_response, ensure_ascii=False)
        error_text_escaped = error_text.replace("\\", "\\\\").replace("'", "\\'")
        return {
            "parameters_used": parameters_used,
            "success": False,
            "error": None,
            "result": f"meta=None content=[TextContent(type='text', text='{error_text_escaped}', annotations=None, meta=None)] structuredContent=None isError=False",
        }

    # Error pattern 2: block_id is not a valid UUID
    if block_id and not is_valid_uuid(block_id):
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

    # Error pattern 3: children is not a list or is empty
    if not isinstance(children, list):
        status = 0
        error_response = {
            "object": "error",
            "status": 400,
            "code": "validation_error",
            "message": "query failed validation: query.children should be an array",
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

    # Error pattern 4: children array is empty (return success but no blocks created)
    if len(children) == 0:
        status = 1
        result_data = {
            "object": "list",
            "results": [],
            "next_cursor": None,
            "has_more": False,
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

    # Validate each child block structure
    for child in children:
        if not isinstance(child, dict):
            status = 0
            error_response = {
                "object": "error",
                "status": 400,
                "code": "validation_error",
                "message": "query failed validation: each child should be an object",
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

        # Check for required 'object' field
        if child.get("object") != "block":
            status = 0
            error_response = {
                "object": "error",
                "status": 400,
                "code": "validation_error",
                "message": "query failed validation: each child block must have 'object' field set to 'block'",
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

        # Check for required 'type' field
        if "type" not in child:
            status = 0
            error_response = {
                "object": "error",
                "status": 400,
                "code": "validation_error",
                "message": "query failed validation: each child block must have a 'type' field",
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

    # Success branch: create blocks
    status = 1

    # Generate metadata for created blocks
    current_time = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.000Z")
    user_id = "9cc4dcba-69ba-4878-82a4-5cf434afcb98"  # Simulated user ID

    # Build results array
    results = []
    blocks_to_append = []

    for child in children:
        # Generate new block ID
        new_block_id = str(uuid.uuid4())

        # Build block object following Notion API structure
        block_obj = {
            "object": "block",
            "id": new_block_id,
            "parent": {"type": "page_id", "page_id": block_id},
            "created_time": current_time,
            "last_edited_time": current_time,
            "created_by": {"object": "user", "id": user_id},
            "last_edited_by": {"object": "user", "id": user_id},
            "has_children": False,
            "archived": False,
            "in_trash": False,
            "type": child.get("type"),
        }

        # Add type-specific content
        block_type = child.get("type")

        if block_type == "paragraph":
            paragraph_data = child.get("paragraph", {})
            rich_text = paragraph_data.get("rich_text", [])

            # Build rich_text array with proper structure
            formatted_rich_text = []
            for rt in rich_text:
                formatted_rt = {
                    "type": rt.get("type", "text"),
                    "text": {
                        "content": rt.get("text", {}).get("content", ""),
                        "link": rt.get("text", {}).get("link"),
                    },
                    "annotations": rt.get(
                        "annotations",
                        {
                            "bold": False,
                            "italic": False,
                            "strikethrough": False,
                            "underline": False,
                            "code": False,
                            "color": "default",
                        },
                    ),
                    "plain_text": rt.get("text", {}).get("content", ""),
                    "href": None,
                }
                formatted_rich_text.append(formatted_rt)

            block_obj["paragraph"] = {
                "rich_text": formatted_rich_text,
                "color": "default",
            }

        elif block_type in ["heading_1", "heading_2", "heading_3"]:
            heading_key = block_type
            heading_data = child.get(heading_key, {})
            rich_text = heading_data.get("rich_text", [])

            # Build rich_text array
            formatted_rich_text = []
            for rt in rich_text:
                formatted_rt = {
                    "type": rt.get("type", "text"),
                    "text": {
                        "content": rt.get("text", {}).get("content", ""),
                        "link": rt.get("text", {}).get("link"),
                    },
                    "annotations": rt.get(
                        "annotations",
                        {
                            "bold": False,
                            "italic": False,
                            "strikethrough": False,
                            "underline": False,
                            "code": False,
                            "color": "default",
                        },
                    ),
                    "plain_text": rt.get("text", {}).get("content", ""),
                    "href": None,
                }
                formatted_rich_text.append(formatted_rt)

            block_obj[heading_key] = {
                "rich_text": formatted_rich_text,
                "is_toggleable": False,
                "color": "default",
            }

        else:
            # For unsupported block types, create a minimal structure
            # This won't be written to the data source but will be returned
            block_obj[block_type] = {}

        results.append(block_obj)

        # Prepare block for appending to data source
        # Extract plain text content from rich_text
        plain_content = ""
        if block_type == "paragraph":
            rich_text = child.get("paragraph", {}).get("rich_text", [])
            plain_content = " ".join(
                [rt.get("text", {}).get("content", "") for rt in rich_text]
            )
        elif block_type in ["heading_1", "heading_2", "heading_3"]:
            rich_text = child.get(block_type, {}).get("rich_text", [])
            plain_content = " ".join(
                [rt.get("text", {}).get("content", "") for rt in rich_text]
            )

        block_to_append = {
            "block_id": new_block_id,
            "type": block_type,
            "content": plain_content,
            "sub_blocks": [],
        }
        blocks_to_append.append(block_to_append)

    # Append new blocks to the data source file
    blocks_file = json.loads(os.environ.get("NOTION_SANDBOX_PATHS"))[2]

    try:
        # Check if file exists
        if os.path.exists(blocks_file):
            # Read existing blocks to check for duplicates
            existing_block_ids = set()
            try:
                with open(blocks_file, "r", encoding="utf-8") as f:
                    for line in f:
                        if line.strip():
                            try:
                                block_data = json.loads(line)
                                existing_block_ids.add(block_data.get("block_id"))
                            except:
                                pass
            except:
                pass

            # Append only new blocks
            with open(blocks_file, "a", encoding="utf-8") as f:
                for block_to_append in blocks_to_append:
                    block_id_to_append = block_to_append["block_id"]
                    if block_id_to_append not in existing_block_ids:
                        f.write(json.dumps(block_to_append, ensure_ascii=False) + "\n")
        else:
            # Create new file
            with open(blocks_file, "w", encoding="utf-8") as f:
                for block_to_append in blocks_to_append:
                    f.write(json.dumps(block_to_append, ensure_ascii=False) + "\n")
    except Exception as e:
        # If writing fails, still return success but log the error
        pass

    # Build final response
    result_data = {
        "object": "list",
        "results": results,
        "next_cursor": None,
        "has_more": False,
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
                {"type": "text", "text": result_text, "annotations": None, "meta": None}
            ],
            "structuredContent": None,
            "isError": False,
        },
    }
