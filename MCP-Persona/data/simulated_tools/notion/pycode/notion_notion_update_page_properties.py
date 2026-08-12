def analyze_response_patterns(parameters_used):
    """
    Analyze input parameters and update page properties in Notion simulation data.

    Args:
        parameters_used (dict): Input parameters from the tool call
            - page_id (str): The ID of the page to update (required)
            - properties (dict): Properties to update (required)
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
    page_id = data.get("page_id")
    properties_data = data.get("properties")
    format_type = data.get("format", "markdown")

    # Define file paths
    notion_paths = json.loads(os.environ.get("NOTION_SANDBOX_PATHS"))
    pages_file = notion_paths[1]
    users_file = notion_paths[0]

    # Error pattern: page_id and properties are not provided
    if page_id is None and properties_data is None:
        status = 0
        error_response = {"error": "Missing required arguments: page_id and properties"}
        error_text = json.dumps(error_response, ensure_ascii=False)
        error_text_escaped = error_text.replace("\\", "\\\\").replace("'", "\\'")
        return {
            "parameters_used": parameters_used,
            "success": False,
            "error": None,
            "result": f"meta=None content=[TextContent(type='text', text='{error_text_escaped}', annotations=None, meta=None)] structuredContent=None isError=False",
        }

    # Error pattern: page_id is not provided
    if page_id is None:
        status = 0
        error_response = {"error": "Missing required argument: page_id"}
        error_text = json.dumps(error_response, ensure_ascii=False)
        error_text_escaped = error_text.replace("\\", "\\\\").replace("'", "\\'")
        return {
            "parameters_used": parameters_used,
            "success": False,
            "error": None,
            "result": f"meta=None content=[TextContent(type='text', text='{error_text_escaped}', annotations=None, meta=None)] structuredContent=None isError=False",
        }

    # Error pattern: properties is not provided
    if properties_data is None:
        status = 0
        error_response = {"error": "Missing required argument: properties"}
        error_text = json.dumps(error_response, ensure_ascii=False)
        error_text_escaped = error_text.replace("\\", "\\\\").replace("'", "\\'")
        return {
            "parameters_used": parameters_used,
            "success": False,
            "error": None,
            "result": f"meta=None content=[TextContent(type='text', text='{error_text_escaped}', annotations=None, meta=None)] structuredContent=None isError=False",
        }

    # Error pattern: page_id format is invalid (not a valid UUID)
    uuid_pattern = (
        r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$"
    )
    if not isinstance(page_id, str) or not re.match(uuid_pattern, page_id):
        status = 0
        error_response = {
            "object": "error",
            "status": 400,
            "code": "validation_error",
            "message": f'path failed validation: path.page_id should be a valid uuid, instead was `"{page_id}`.',
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

    # Load all pages and find the target page
    pages_dict = {}
    pages_list = []
    target_page = None

    if os.path.exists(pages_file):
        with open(pages_file, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line:
                    page_data = json.loads(line)
                    pages_dict[page_data["page_id"]] = page_data
                    pages_list.append(page_data)
                    if page_data["page_id"] == page_id:
                        target_page = page_data

    # Error pattern: page_id not found
    if target_page is None:
        status = 0
        error_response = {
            "object": "error",
            "status": 404,
            "code": "object_not_found",
            "message": f"Could not find page with ID: {page_id}. Make sure the relevant pages and databases are shared with your integration.",
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

    # Load users data for created_by/last_edited_by fields
    users_dict = {}
    if os.path.exists(users_file):
        with open(users_file, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line:
                    user_data = json.loads(line)
                    users_dict[user_data["user_id"]] = user_data

    # Get user info for created_by/last_edited_by
    user_id = target_page.get("properties", {}).get("user_id", "")
    user_info = users_dict.get(user_id, {})

    # Perform update: modify the page properties
    status = 1

    # Update properties in target_page
    if "properties" not in target_page:
        target_page["properties"] = {}

    # Parse and apply properties update
    for prop_key, prop_value in properties_data.items():
        if prop_key == "title" and "title" in prop_value:
            # Extract title content from nested structure
            title_array = prop_value["title"]
            if title_array and len(title_array) > 0:
                title_text = title_array[0].get("text", {}).get("content", "")
                target_page["properties"]["title"] = title_text
        elif prop_key in target_page["properties"]:
            # For other property types, update directly
            target_page["properties"][prop_key] = prop_value
        else:
            # Add new property
            target_page["properties"][prop_key] = prop_value

    # Update modified_time
    from datetime import datetime

    current_time = datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%S.000Z")
    target_page["properties"]["modified_time"] = current_time

    # Rewrite pages.jsonl with updated page
    if os.path.exists(pages_file):
        with open(pages_file, "w", encoding="utf-8") as f:
            for page in pages_list:
                if page["page_id"] == page_id:
                    page = target_page
                f.write(json.dumps(page, ensure_ascii=False) + "\n")

    # Validate format enum
    if format_type not in ["json", "markdown"]:
        format_type = "markdown"

    # Build response
    title = target_page.get("properties", {}).get("title", "Untitled")
    created_time = target_page.get("properties", {}).get("created_time", current_time)
    modified_time = target_page.get("properties", {}).get("modified_time", current_time)

    if format_type == "json":
        # Build JSON format response matching Notion API structure
        notion_page = {
            "object": "page",
            "id": page_id,
            "created_time": created_time,
            "last_edited_time": modified_time,
            "created_by": {"object": "user", "id": user_id},
            "last_edited_by": {"object": "user", "id": user_id},
            "cover": None,
            "icon": None,
            "parent": {"type": "workspace", "workspace": True},
            "archived": False,
            "in_trash": False,
            "is_locked": False,
            "properties": {},
            "url": f"https://www.notion.so/{title.replace(' ', '-')}-{page_id.replace('-', '')}",
            "public_url": None,
            "request_id": str(uuid.uuid4()),
        }

        # Build properties structure
        if "title" in target_page["properties"]:
            title_value = target_page["properties"]["title"]
            notion_page["properties"]["title"] = {
                "id": "title",
                "type": "title",
                "title": [
                    {
                        "type": "text",
                        "text": {"content": title_value, "link": None},
                        "annotations": {
                            "bold": False,
                            "italic": False,
                            "strikethrough": False,
                            "underline": False,
                            "code": False,
                            "color": "default",
                        },
                        "plain_text": title_value,
                        "href": None,
                    }
                ],
            }

        result_text = json.dumps(notion_page, indent=2, ensure_ascii=False)

    else:  # markdown format
        # Build markdown format response for readable display
        lines = []
        lines.append(f"# Page Properties Updated")
        lines.append("")
        lines.append(f"**Page ID:** `{page_id}`")
        lines.append("")
        lines.append(f"**Title:** {title}")
        lines.append("")
        lines.append(f"**Last Modified:** {modified_time}")
        lines.append("")

        if user_info:
            lines.append(
                f"**Modified by:** {user_info.get('name', 'Unknown')} ({user_info.get('email', 'N/A')})"
            )
            lines.append("")

        lines.append("### Updated Properties")
        lines.append("")
        for prop_key, prop_value in properties_data.items():
            lines.append(
                f"- **{prop_key}:** {json.dumps(prop_value, ensure_ascii=False)}"
            )
        lines.append("")

        lines.append("---")
        lines.append("")
        lines.append("*The page properties have been successfully updated.*")

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
