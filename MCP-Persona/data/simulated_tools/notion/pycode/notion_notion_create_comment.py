def analyze_response_patterns(parameters_used):
    """
    Analyze input parameters and create a comment in Notion simulation data.

    Args:
        parameters_used (dict): Input parameters from the tool call
            - parent (dict): Parent object with page_id or block_id
            - discussion_id (str): Existing discussion ID to add comment to
            - rich_text (list): Array of rich text objects (required)
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
    parent = data.get("parent")
    discussion_id = data.get("discussion_id")
    rich_text = data.get("rich_text")
    format_type = data.get("format", "markdown")

    # Define file paths
    notion_paths = json.loads(os.environ.get("NOTION_SANDBOX_PATHS"))
    comments_file = notion_paths[3]
    users_file = notion_paths[0]

    # Error pattern: Neither parent nor discussion_id provided
    if parent is None and discussion_id is None:
        status = 0
        error_response = {
            "error": "Either parent.page_id or discussion_id must be provided"
        }
        error_text = json.dumps(error_response, ensure_ascii=False)
        error_text_escaped = error_text.replace("\\", "\\\\").replace("'", "\\'")
        return {
            "parameters_used": parameters_used,
            "success": False,
            "error": None,
            "result": f"meta=None content=[TextContent(type='text', text='{error_text_escaped}', annotations=None, meta=None)] structuredContent=None isError=False",
        }

    # Error pattern: Both parent and discussion_id provided
    if parent is not None and discussion_id is not None:
        status = 0
        error_response = {"error": "Cannot specify both parent and discussion_id"}
        error_text = json.dumps(error_response, ensure_ascii=False)
        error_text_escaped = error_text.replace("\\", "\\\\").replace("'", "\\'")
        return {
            "parameters_used": parameters_used,
            "success": False,
            "error": None,
            "result": f"meta=None content=[TextContent(type='text', text='{error_text_escaped}', annotations=None, meta=None)] structuredContent=None isError=False",
        }

    # Error pattern: rich_text is not provided
    if rich_text is None:
        status = 0
        error_response = {
            "object": "error",
            "status": 400,
            "code": "validation_error",
            "message": "body failed validation: body.rich_text should be defined, instead was `undefined`.",
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

    # Error pattern: rich_text is empty array
    if isinstance(rich_text, list) and len(rich_text) == 0:
        status = 0
        error_response = {
            "object": "error",
            "status": 400,
            "code": "validation_error",
            "message": "body failed validation: body.rich_text should be defined, instead was `undefined`.",
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

    # Validate parent.page_id format if provided
    uuid_pattern = (
        r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$"
    )
    if parent is not None:
        page_id = parent.get("page_id")
        block_id = parent.get("block_id")

        # If page_id is provided, validate it
        if page_id is not None:
            if not isinstance(page_id, str) or not re.match(uuid_pattern, page_id):
                status = 0
                error_response = {
                    "object": "error",
                    "status": 400,
                    "code": "validation_error",
                    "message": f'body failed validation. Fix one:\nbody.parent.page_id should be a valid uuid, instead was `"{page_id}"`.\nbody.parent.block_id should be defined, instead was `undefined`.',
                    "request_id": str(uuid.uuid4()),
                }
                error_text = json.dumps(error_response, ensure_ascii=False)
                error_text_escaped = error_text.replace("\\", "\\\\").replace(
                    "'", "\\'"
                )
                return {
                    "parameters_used": parameters_used,
                    "success": False,
                    "error": None,
                    "result": f"meta=None content=[TextContent(type='text', text='{error_text_escaped}', annotations=None, meta=None)] structuredContent=None isError=False",
                }

    # Validate discussion_id format if provided
    if discussion_id is not None:
        if not isinstance(discussion_id, str) or not re.match(
            uuid_pattern, discussion_id
        ):
            status = 0
            error_response = {
                "object": "error",
                "status": 400,
                "code": "validation_error",
                "message": f'path failed validation: path.discussion_id should be a valid uuid, instead was `"{discussion_id}"`.',
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

    # Load default user for created_by field
    default_user_id = "9cc4dcba-69ba-4878-82a4-5cf434afcb98"
    default_user_name = "MCP-test"

    # Create comment
    status = 1

    # Generate new comment_id
    new_comment_id = str(uuid.uuid4())

    # Generate or use existing discussion_id
    if discussion_id:
        target_discussion_id = discussion_id
        parent_info = None  # Will load from existing comments if needed
    else:
        # Creating new discussion thread
        target_discussion_id = str(uuid.uuid4())
        page_id = parent.get("page_id")
        # Generate a block_id for the comment (comments are attached to blocks)
        target_block_id = str(uuid.uuid4())
        parent_info = {"page_id": page_id, "block_id": target_block_id}

    # Get current timestamp
    current_time = datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%S.000Z")

    # Extract plain text from rich_text for storage in comments.jsonl
    plain_text_parts = []
    for rt in rich_text:
        rt_type = rt.get("type")
        if rt_type == "text":
            text_obj = rt.get("text", {})
            content = text_obj.get("content", "")
            plain_text_parts.append(content)
        elif rt_type == "mention":
            mention_obj = rt.get("mention", {})
            mention_type = mention_obj.get("type")
            if mention_type == "user":
                user_ref = mention_obj.get("user", {})
                # Try to get user name from users.jsonl
                user_id_to_find = user_ref.get("id")
                user_name = "@Unknown"
                if os.path.exists(users_file):
                    with open(users_file, "r", encoding="utf-8") as f:
                        for line in f:
                            line = line.strip()
                            if line:
                                try:
                                    user_data = json.loads(line)
                                    if user_data.get("user_id") == user_id_to_find:
                                        user_name = (
                                            f"@{user_data.get('name', 'Unknown')}"
                                        )
                                        break
                                except:
                                    pass
                plain_text_parts.append(user_name)
            elif mention_type == "page":
                page_ref = mention_obj.get("page", {})
                plain_text_parts.append(f"@Page({page_ref.get('id', '')})")
            else:
                plain_text_parts.append(f"@{mention_type}")
        elif rt_type == "equation":
            eq_obj = rt.get("equation", {})
            plain_text_parts.append(f"$${eq_obj.get('expression', '')}$$")

    plain_text = "".join(plain_text_parts)

    # Create comment object for external data source
    comment_record = {
        "discussion_id": target_discussion_id,
        "parent": parent_info if parent_info else {"page_id": "", "block_id": ""},
        "created_time": current_time,
        "created_by": default_user_name,
        "text": plain_text,
    }

    # Append to comments.jsonl
    with open(comments_file, "a", encoding="utf-8") as f:
        f.write(json.dumps(comment_record, ensure_ascii=False) + "\n")

    # Validate format enum
    if format_type not in ["json", "markdown"]:
        format_type = "markdown"

    # Build response
    if format_type == "json":
        # Build JSON format response matching Notion API structure
        notion_comment = {
            "object": "comment",
            "id": new_comment_id,
            "parent": {
                "type": "page_id",
                "page_id": parent.get("page_id") if parent else "",
            },
            "discussion_id": target_discussion_id,
            "created_time": current_time,
            "last_edited_time": current_time,
            "created_by": {"object": "user", "id": default_user_id},
            "rich_text": [],
            "display_name": {"type": "integration", "resolved_name": default_user_name},
            "request_id": str(uuid.uuid4()),
        }

        # Build rich_text array from input
        for rt in rich_text:
            rt_type = rt.get("type")
            rt_item = {"type": rt_type}

            # Default annotations
            annotations_input = rt.get("annotations", {})
            rt_item["annotations"] = {
                "bold": annotations_input.get("bold", False),
                "italic": annotations_input.get("italic", False),
                "strikethrough": annotations_input.get("strikethrough", False),
                "underline": annotations_input.get("underline", False),
                "code": annotations_input.get("code", False),
                "color": annotations_input.get("color", "default"),
            }

            if rt_type == "text":
                text_obj = rt.get("text", {})
                content = text_obj.get("content", "")
                link_obj = text_obj.get("link")

                rt_item["text"] = {
                    "content": content,
                    "link": link_obj if link_obj else None,
                }
                rt_item["plain_text"] = content
                rt_item["href"] = link_obj.get("url") if link_obj else None

            elif rt_type == "mention":
                mention_obj = rt.get("mention", {})
                mention_type = mention_obj.get("type")
                rt_item["mention"] = {"type": mention_type}

                if mention_type == "user":
                    user_ref = mention_obj.get("user", {})
                    user_id_mention = user_ref.get("id")

                    # Try to load full user info from users.jsonl
                    user_info = {"object": "user", "id": user_id_mention}
                    if os.path.exists(users_file):
                        with open(users_file, "r", encoding="utf-8") as f:
                            for line in f:
                                line = line.strip()
                                if line:
                                    try:
                                        user_data = json.loads(line)
                                        if user_data.get("user_id") == user_id_mention:
                                            user_info["name"] = user_data.get("name")
                                            user_info["avatar_url"] = (
                                                None  # Not available
                                            )
                                            user_info["type"] = "person"
                                            user_info["person"] = {
                                                "email": user_data.get("email")
                                            }
                                            break
                                    except:
                                        pass

                    rt_item["mention"]["user"] = user_info
                    rt_item["plain_text"] = f"@{user_info.get('name', user_id_mention)}"
                    rt_item["href"] = None

                elif mention_type == "page":
                    page_ref = mention_obj.get("page", {})
                    rt_item["mention"]["page"] = page_ref
                    rt_item["plain_text"] = f"@Page({page_ref.get('id')})"
                    rt_item["href"] = None

                elif mention_type == "database":
                    db_ref = mention_obj.get("database", {})
                    rt_item["mention"]["database"] = db_ref
                    rt_item["plain_text"] = f"@Database({db_ref.get('id')})"
                    rt_item["href"] = None

                elif mention_type == "date":
                    date_ref = mention_obj.get("date", {})
                    rt_item["mention"]["date"] = date_ref
                    rt_item["plain_text"] = date_ref.get("start", "")
                    rt_item["href"] = None

                elif mention_type == "link_preview":
                    lp_ref = mention_obj.get("link_preview", {})
                    rt_item["mention"]["link_preview"] = lp_ref
                    rt_item["plain_text"] = lp_ref.get("url", "")
                    rt_item["href"] = None

                elif mention_type == "template_mention":
                    tm_ref = mention_obj.get("template_mention", {})
                    rt_item["mention"]["template_mention"] = tm_ref
                    tm_type = tm_ref.get("type", "")
                    if tm_type == "template_mention_date":
                        rt_item["plain_text"] = tm_ref.get("template_mention_date", "")
                    elif tm_type == "template_mention_user":
                        rt_item["plain_text"] = tm_ref.get("template_mention_user", "")
                    rt_item["href"] = None

            elif rt_type == "equation":
                eq_obj = rt.get("equation", {})
                expression = eq_obj.get("expression", "")
                rt_item["equation"] = {"expression": expression}
                rt_item["plain_text"] = f"$${expression}$$"
                rt_item["href"] = None

            notion_comment["rich_text"].append(rt_item)

        result_text = json.dumps(notion_comment, indent=2, ensure_ascii=False)

    else:  # markdown format
        # Build markdown format response for readable display
        lines = []
        lines.append(f"# Comment Created Successfully")
        lines.append("")
        lines.append(f"**Comment ID:** `{new_comment_id}`")
        lines.append("")
        lines.append(f"**Discussion ID:** `{target_discussion_id}`")
        lines.append("")

        if parent:
            page_id = parent.get("page_id")
            if page_id:
                lines.append(f"**Parent Page:** `{page_id}`")
                lines.append("")

        lines.append(f"**Created:** {current_time}")
        lines.append("")
        lines.append(f"**Author:** {default_user_name}")
        lines.append("")

        if rich_text:
            lines.append("### Content")
            lines.append("")
            lines.append(plain_text)
            lines.append("")

        lines.append("---")
        lines.append("")
        lines.append("*A new comment has been created.*")

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
