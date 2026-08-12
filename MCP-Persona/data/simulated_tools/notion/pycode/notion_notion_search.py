import os
def analyze_response_patterns(parameters_used):
    """
    Search pages or databases by title in Notion.

    This function simulates the Notion API for searching by reading
    from external JSONL data files. It supports filtering by object type,
    sorting, and pagination.

    Args:
        parameters_used: Dictionary containing:
            - data.query: Text to search for in titles (optional, empty string searches all)
            - data.filter: Filter by object type {property: "object", value: "page"|"database"} (optional)
            - data.sort: Sort order {direction: "ascending"|"descending", timestamp: "last_edited_time"} (optional)
            - data.start_cursor: Pagination cursor (optional)
            - data.page_size: Number of results to return, max 100 (optional)
            - data.format: Response format, 'json' or 'markdown' (default: 'markdown')

    Returns:
        Dictionary with response structure matching Notion API
    """
    import json
    import uuid
    import re
    from datetime import datetime

    global status

    data = parameters_used.get("data", {})
    query = data.get("query", "").lower()
    filter_obj = data.get("filter")
    sort_obj = data.get("sort")
    start_cursor = data.get("start_cursor")
    page_size = data.get("page_size", 100)
    format_type = data.get("format", "markdown")

    pages_file = json.loads(os.environ.get("NOTION_SANDBOX_PATHS"))[1]

    # Validate start_cursor format (must be UUID if provided)
    if start_cursor:
        uuid_pattern = r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$"
        if not re.match(uuid_pattern, start_cursor):
            status = 0
            error_response = {
                "object": "error",
                "status": 400,
                "code": "validation_error",
                "message": f'body failed validation: body.start_cursor should be a valid uuid or `undefined`, instead was "{start_cursor}".',
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

    # Validate filter
    if filter_obj:
        filter_value = filter_obj.get("value", "")
        if filter_value not in ["page", "database"]:
            status = 0
            error_response = {
                "object": "error",
                "status": 400,
                "code": "validation_error",
                "message": f'body failed validation: body.filter.value should be `"page"` or `"data_source"`, instead was `"{filter_value}"`.',
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

    # Validate sort
    if sort_obj:
        sort_direction = sort_obj.get("direction", "")
        sort_timestamp = sort_obj.get("timestamp", "")

        if sort_direction and sort_direction not in ["ascending", "descending"]:
            status = 0
            error_response = {
                "object": "error",
                "status": 400,
                "code": "validation_error",
                "message": f'body failed validation: body.sort.direction should be `"ascending"` or `"descending"`, instead was `"{sort_direction}"`.',
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

        if sort_timestamp and sort_timestamp != "last_edited_time":
            status = 0
            error_response = {
                "object": "error",
                "status": 400,
                "code": "validation_error",
                "message": f'body failed validation: body.sort.timestamp should be `"last_edited_time"`, instead was `"{sort_timestamp}"`.',
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

    # Validate page_size (Notion API doesn't error on page_size=0, just returns empty)
    if page_size < 1 or page_size > 100:
        page_size = min(max(page_size, 1), 100)

    # Load pages from external data source
    all_results = []

    # Since we don't have databases.jsonl, we only search pages
    # In a real scenario, you would also load databases from a separate file
    try:
        with open(pages_file, "r", encoding="utf-8") as f:
            for line in f:
                if line.strip():
                    page_data = json.loads(line)
                    page_id = page_data.get("page_id", "")
                    properties = page_data.get("properties", {})
                    title = properties.get("title", "")

                    # Match query against title (case-insensitive)
                    if not query or query in title.lower():
                        # Build Notion page object
                        notion_page = {
                            "object": "page",
                            "id": page_id,
                            "created_time": properties.get("created_time", ""),
                            "last_edited_time": properties.get("modified_time", ""),
                            "created_by": {
                                "object": "user",
                                "id": properties.get(
                                    "user_id", "29cd872b-594c-8172-b723-0002706e35a6"
                                ),
                            },
                            "last_edited_by": {
                                "object": "user",
                                "id": properties.get(
                                    "user_id", "29cd872b-594c-8172-b723-0002706e35a6"
                                ),
                            },
                            "cover": None,
                            "icon": None,
                            "parent": {"type": "workspace", "workspace": True},
                            "archived": False,
                            "in_trash": False,
                            "is_locked": False,
                            "properties": {
                                "title": {
                                    "id": "title",
                                    "type": "title",
                                    "title": (
                                        [
                                            {
                                                "type": "text",
                                                "text": {
                                                    "content": title,
                                                    "link": None,
                                                },
                                                "annotations": {
                                                    "bold": False,
                                                    "italic": False,
                                                    "strikethrough": False,
                                                    "underline": False,
                                                    "code": False,
                                                    "color": "default",
                                                },
                                                "plain_text": title,
                                                "href": None,
                                            }
                                        ]
                                        if title
                                        else []
                                    ),
                                }
                            },
                            "url": f"https://www.notion.so/{page_id.replace('-', '')}",
                            "public_url": None,
                        }
                        all_results.append(notion_page)
    except FileNotFoundError:
        pass

    # Apply filter by object type
    if filter_obj:
        filter_value = filter_obj.get("value", "")
        # Only keep pages if filter is 'page'
        # Note: We don't have database data, so filtering by 'database' will return empty
        if filter_value == "database":
            all_results = []

    # Apply sorting
    if sort_obj and sort_obj.get("timestamp") == "last_edited_time":
        direction = sort_obj.get("direction", "descending")
        reverse = direction == "descending"
        all_results.sort(key=lambda x: x.get("last_edited_time", ""), reverse=reverse)

    # Handle pagination with start_cursor
    if start_cursor:
        # Find the position of the cursor in the results
        cursor_index = -1
        for i, result in enumerate(all_results):
            result_id = result.get("id", "")
            if result_id == start_cursor:
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
        all_results = all_results[cursor_index + 1 :]

    # Apply page_size limit
    results_page = all_results[:page_size]
    has_more = len(all_results) > page_size

    # Generate next_cursor if there are more results
    next_cursor = None
    if has_more and len(results_page) > 0:
        last_result = results_page[-1]
        next_cursor = last_result.get("id", "")

    status = 1

    # Build response based on format
    if format_type == "json":
        response_data = {
            "object": "list",
            "results": results_page,
            "next_cursor": next_cursor,
            "has_more": has_more,
            "type": "page_or_database",
            "page_or_database": {},
            "request_id": str(uuid.uuid4()),
        }
        result_text = json.dumps(response_data, ensure_ascii=False, indent=2)
    else:
        # Markdown format - build readable structure
        lines = ["{", '  "object": "list",', '  "results": [']

        for i, result in enumerate(results_page):
            if i > 0:
                lines.append("    },")
            lines.append("    {")
            lines.append(f'      "object": "{result["object"]}",')
            lines.append(f'      "id": "{result["id"]}",')
            lines.append(f'      "created_time": "{result["created_time"]}",')
            lines.append(f'      "last_edited_time": "{result["last_edited_time"]}",')
            lines.append('      "created_by": {')
            lines.append(f'        "object": "user",')
            lines.append(f'        "id": "{result["created_by"]["id"]}"')
            lines.append("      },")
            lines.append('      "last_edited_by": {')
            lines.append(f'        "object": "user",')
            lines.append(f'        "id": "{result["last_edited_by"]["id"]}"')
            lines.append("      },")
            lines.append('      "cover": null,')
            lines.append('      "icon": null,')
            lines.append('      "parent": {')
            lines.append(f'        "type": "{result["parent"]["type"]}",')
            if result["parent"]["type"] == "workspace":
                lines.append('        "workspace": true')
            else:
                lines.append(
                    f'        "page_id": "{result["parent"].get("page_id", "")}"'
                )
            lines.append("      },")
            lines.append('      "archived": false,')
            lines.append('      "in_trash": false,')
            lines.append('      "is_locked": false,')
            lines.append('      "properties": {')
            lines.append('        "title": {')
            lines.append('          "id": "title",')
            lines.append('          "type": "title",')
            lines.append('          "title": [')

            # Add title text if exists
            title_obj = result["properties"]["title"]
            if title_obj["title"]:
                lines.append("            {")
                lines.append('              "type": "text",')
                lines.append('              "text": {')
                lines.append(
                    f'                "content": "{title_obj["title"][0]["text"]["content"]}",'
                )
                lines.append('                "link": null')
                lines.append("              },")
                lines.append('              "annotations": {')
                lines.append('                "bold": false,')
                lines.append('                "italic": false,')
                lines.append('                "strikethrough": false,')
                lines.append('                "underline": false,')
                lines.append('                "code": false,')
                lines.append('                "color": "default"')
                lines.append("              },")
                lines.append(
                    f'              "plain_text": "{title_obj["title"][0]["plain_text"]}",'
                )
                lines.append('              "href": null')
                lines.append("            }")

            lines.append("          ]")
            lines.append("        }")
            lines.append("      },")
            lines.append(f'      "url": "{result["url"]}",')
            lines.append('      "public_url": null')

            if i == len(results_page) - 1:
                lines.append("    }")
            else:
                lines.append("    },")

        lines.extend(
            [
                "  ],",
                f'  "next_cursor": {json.dumps(next_cursor)},',
                f'  "has_more": {str(has_more).lower()},',
                '  "type": "page_or_database",',
                '  "page_or_database": {},',
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
