def analyze_response_patterns(parameters_used):
    """
    Analyze input parameters and create a new database item (page) in Notion simulation data.

    Args:
        parameters_used (dict): Input parameters from the tool call
            - database_id (str): The ID of the database to add the item to (required)
            - properties (dict): Properties of the new database item (required)
            - format (str): Response format ('json' or 'markdown')

    Returns:
        dict: Response in the same format as the real Notion API
    """
    import json
    import uuid
    import re
    import os
    from datetime import datetime

    global status

    # Extract data from parameters
    data = parameters_used.get("data", {})
    database_id = data.get("database_id")
    properties_data = data.get("properties")
    format_type = data.get("format", "markdown")

    # Define file paths
    notion_paths = json.loads(os.environ.get("NOTION_SANDBOX_PATHS"))
    pages_file = notion_paths[1]
    users_file = notion_paths[0]

    # Error pattern: database_id and properties are not provided
    if database_id is None and properties_data is None:
        status = 0
        error_response = {
            "object": "error",
            "status": 400,
            "code": "validation_error",
            "message": "body failed validation. Fix one:\nbody.parent.page_id should be defined, instead was `undefined`.\nbody.parent.database_id should be defined, instead was `undefined`.\nbody.parent.data_source_id should be defined, instead was `undefined`.\nbody.parent.workspace should be defined, instead was `undefined`.",
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

    # Error pattern: database_id is not provided
    if database_id is None:
        status = 0
        error_response = {
            "object": "error",
            "status": 400,
            "code": "validation_error",
            "message": "body.failed validation: Fix one:\nbody.parent.page_id should be defined, instead was `undefined`.\nbody.parent.database_id should be defined, instead was `undefined`.",
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

    # Error pattern: properties is not provided
    if properties_data is None:
        status = 0
        error_response = {
            "object": "error",
            "status": 400,
            "code": "validation_error",
            "message": "body.properties should be defined, instead was `undefined`.",
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

    # Error pattern: database_id format is invalid (not a valid UUID)
    uuid_pattern = (
        r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$"
    )
    if not isinstance(database_id, str) or not re.match(uuid_pattern, database_id):
        status = 0
        error_response = {
            "object": "error",
            "status": 400,
            "code": "validation_error",
            "message": f"path failed validation: path.database_id should be a valid uuid, instead was `{database_id}`.",
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

    # Load a default user for created_by/last_edited_by fields
    # Use the first user from users.jsonl as the creator
    default_user_id = "9cc4dcba-69ba-4878-82a4-5cf434afcb98"

    # Create new page
    status = 1

    # Generate new page_id
    new_page_id = str(uuid.uuid4())

    # Get current timestamp
    current_time = datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%S.000Z")

    # Extract title from properties
    title = "Untitled"
    if "项目名称" in properties_data:
        title_array = properties_data["项目名称"].get("title", [])
        if title_array and len(title_array) > 0:
            title = title_array[0].get("text", {}).get("content", "Untitled")

    # Create new page object for external data source
    new_page = {
        "page_id": new_page_id,
        "properties": {
            "title": title,
            "user_id": default_user_id,
            "created_time": current_time,
            "modified_time": current_time,
            "database_id": database_id,
        },
        "blocks": [],
    }

    # Append to pages.jsonl
    with open(pages_file, "a", encoding="utf-8") as f:
        f.write(json.dumps(new_page, ensure_ascii=False) + "\n")

    # Validate format enum
    if format_type not in ["json", "markdown"]:
        format_type = "markdown"

    # Build response
    if format_type == "json":
        # Build JSON format response matching Notion API structure
        notion_page = {
            "object": "page",
            "id": new_page_id,
            "created_time": current_time,
            "last_edited_time": current_time,
            "created_by": {"object": "user", "id": default_user_id},
            "last_edited_by": {"object": "user", "id": default_user_id},
            "cover": None,
            "icon": None,
            "parent": {"type": "database_id", "database_id": database_id},
            "archived": False,
            "in_trash": False,
            "is_locked": False,
            "properties": {},
            "url": f"https://www.notion.so/{title.replace(' ', '-')}-{new_page_id.replace('-', '')}",
            "public_url": None,
            "request_id": str(uuid.uuid4()),
        }

        # Build properties structure from input
        # Property ID mappings (from example responses)
        property_id_map = {
            "项目名称": "title",
            "状态": "xYPp",
            "优先级": "%5CSH%7B",
            "团队": "VDI%3F",
            "负责人": "%5B%3D%3CF",
            "开始日期": "cMg%3D",
            "结束日期": "me%5E%3A",
            "附加文件": "JB%7C%40",
        }

        # Process each property from input
        for prop_name, prop_value in properties_data.items():
            if prop_name == "项目名称" and "title" in prop_value:
                title_array = prop_value["title"]
                notion_page["properties"][prop_name] = {
                    "id": "title",
                    "type": "title",
                    "title": [],
                }
                if title_array and len(title_array) > 0:
                    text_content = title_array[0].get("text", {}).get("content", "")
                    notion_page["properties"][prop_name]["title"] = [
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
                    ]

            elif prop_name == "状态" and "status" in prop_value:
                status_name = prop_value["status"].get("name", "")
                notion_page["properties"][prop_name] = {
                    "id": property_id_map.get(prop_name, ""),
                    "type": "status",
                    "status": {
                        "id": str(uuid.uuid4()),
                        "name": status_name,
                        "color": (
                            "default"
                            if status_name == "未开始"
                            else "blue" if status_name == "进行中" else "green"
                        ),
                    },
                }

            elif prop_name == "优先级" and "select" in prop_value:
                select_name = prop_value["select"].get("name", "")
                color_map = {"高": "red", "中": "yellow", "低": "green"}
                notion_page["properties"][prop_name] = {
                    "id": property_id_map.get(prop_name, ""),
                    "type": "select",
                    "select": {
                        "id": str(uuid.uuid4()),
                        "name": select_name,
                        "color": color_map.get(select_name, "default"),
                    },
                }

            elif prop_name == "团队" and "multi_select" in prop_value:
                multi_select_array = prop_value["multi_select"]
                notion_page["properties"][prop_name] = {
                    "id": property_id_map.get(prop_name, ""),
                    "type": "multi_select",
                    "multi_select": [],
                }
                color_options = [
                    "green",
                    "blue",
                    "purple",
                    "default",
                    "orange",
                    "yellow",
                ]
                color_idx = 0
                for item in multi_select_array:
                    item_name = item.get("name", "")
                    notion_page["properties"][prop_name]["multi_select"].append(
                        {
                            "id": str(uuid.uuid4()),
                            "name": item_name,
                            "color": color_options[color_idx % len(color_options)],
                        }
                    )
                    color_idx += 1

            elif prop_name == "负责人" and "people" in prop_value:
                notion_page["properties"][prop_name] = {
                    "id": property_id_map.get(prop_name, ""),
                    "type": "people",
                    "people": [],
                }

            elif prop_name in ["开始日期", "结束日期"] and "date" in prop_value:
                date_obj = prop_value["date"]
                date_start = date_obj.get("start") if date_obj else None
                notion_page["properties"][prop_name] = {
                    "id": property_id_map.get(prop_name, ""),
                    "type": "date",
                    "date": (
                        {"start": date_start, "end": None, "time_zone": None}
                        if date_start
                        else None
                    ),
                }

            elif prop_name == "附加文件":
                notion_page["properties"][prop_name] = {
                    "id": property_id_map.get(prop_name, ""),
                    "type": "files",
                    "files": [],
                }

        # Add missing empty properties for complete schema
        all_property_names = [
            "项目名称",
            "状态",
            "优先级",
            "团队",
            "负责人",
            "开始日期",
            "结束日期",
            "附加文件",
        ]
        for prop_name in all_property_names:
            if prop_name not in notion_page["properties"]:
                if prop_name == "项目名称":
                    notion_page["properties"][prop_name] = {
                        "id": "title",
                        "type": "title",
                        "title": [],
                    }
                elif prop_name == "状态":
                    notion_page["properties"][prop_name] = {
                        "id": property_id_map.get(prop_name, ""),
                        "type": "status",
                        "status": {
                            "id": str(uuid.uuid4()),
                            "name": "未开始",
                            "color": "default",
                        },
                    }
                elif prop_name == "优先级":
                    notion_page["properties"][prop_name] = {
                        "id": property_id_map.get(prop_name, ""),
                        "type": "select",
                        "select": None,
                    }
                elif prop_name == "团队":
                    notion_page["properties"][prop_name] = {
                        "id": property_id_map.get(prop_name, ""),
                        "type": "multi_select",
                        "multi_select": [],
                    }
                elif prop_name == "负责人":
                    notion_page["properties"][prop_name] = {
                        "id": property_id_map.get(prop_name, ""),
                        "type": "people",
                        "people": [],
                    }
                elif prop_name in ["开始日期", "结束日期"]:
                    notion_page["properties"][prop_name] = {
                        "id": property_id_map.get(prop_name, ""),
                        "type": "date",
                        "date": None,
                    }
                elif prop_name == "附加文件":
                    notion_page["properties"][prop_name] = {
                        "id": property_id_map.get(prop_name, ""),
                        "type": "files",
                        "files": [],
                    }

        result_text = json.dumps(notion_page, indent=2, ensure_ascii=False)

    else:  # markdown format
        # Build markdown format response for readable display
        lines = []
        lines.append(f"# Database Item Created Successfully")
        lines.append("")
        lines.append(f"**Page ID:** `{new_page_id}`")
        lines.append("")
        lines.append(f"**Title:** {title}")
        lines.append("")
        lines.append(f"**Database ID:** `{database_id}`")
        lines.append("")
        lines.append(f"**Created:** {current_time}")
        lines.append("")
        lines.append(f"**Created by:** `{default_user_id}`")
        lines.append("")

        if properties_data:
            lines.append("### Properties")
            lines.append("")
            for prop_key, prop_value in properties_data.items():
                lines.append(
                    f"- **{prop_key}:** {json.dumps(prop_value, ensure_ascii=False)}"
                )
            lines.append("")

        lines.append("---")
        lines.append("")
        lines.append("*A new item has been created in the database.*")

        result_text = "\\n".join(lines)

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
