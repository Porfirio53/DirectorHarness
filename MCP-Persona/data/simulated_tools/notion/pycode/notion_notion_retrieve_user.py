def analyze_response_patterns(parameters_used):
    """
    Analyze input parameters and retrieve a user from Notion simulation data.

    Args:
        parameters_used (dict): Input parameters from the tool call
            - user_id (str): The ID of the user to retrieve (required)
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
    user_id = data.get("user_id")
    format_type = data.get("format", "markdown")

    # Define file paths
    notion_paths = json.loads(os.environ.get("NOTION_SANDBOX_PATHS"))
    users_file = notion_paths[0]

    # Error pattern: user_id is not provided
    if user_id is None:
        status = 0
        error_response = {"error": "Missing required argument: user_id"}
        error_text = json.dumps(error_response, ensure_ascii=False)
        error_text_escaped = error_text.replace("\\", "\\\\").replace("'", "\\'")
        return {
            "parameters_used": parameters_used,
            "success": False,
            "error": None,
            "result": f"meta=None content=[TextContent(type='text', text='{error_text_escaped}', annotations=None, meta=None)] structuredContent=None isError=False",
        }

    # Error pattern: user_id format is invalid (not a valid UUID)
    uuid_pattern = (
        r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$"
    )
    if not isinstance(user_id, str) or not re.match(uuid_pattern, user_id):
        status = 0
        error_response = {
            "object": "error",
            "status": 400,
            "code": "validation_error",
            "message": f'path failed validation: path.user_id should be a valid uuid, instead was `"{user_id}"`.',
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

    # Load users data and find the target user
    target_user = None
    users_dict = {}

    if os.path.exists(users_file):
        with open(users_file, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line:
                    user_data = json.loads(line)
                    users_dict[user_data["user_id"]] = user_data
                    if user_data["user_id"] == user_id:
                        target_user = user_data

    # Error pattern: user_id not found
    if target_user is None:
        status = 0
        error_response = {
            "object": "error",
            "status": 404,
            "code": "object_not_found",
            "message": f"Could not find user with ID: {user_id}. Make sure the relevant users are shared with your integration.",
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

    # Validate format enum
    if format_type not in ["json", "markdown"]:
        format_type = "markdown"

    # Build response
    status = 1

    # Extract user properties from external data source
    name = target_user.get("name", "")
    email = target_user.get("email", "")
    phone = target_user.get("phone", "")
    persona = target_user.get("persona", "")
    country_code = target_user.get("country_code", "")

    if format_type == "json":
        # Build JSON format response matching Notion API structure
        # Determine user type based on available data
        # Users with email are treated as "person", otherwise could be "bot"
        if email:
            user_type = "person"
            notion_user = {
                "object": "user",
                "id": user_id,
                "type": user_type,
                "name": name,
                "avatar_url": None,  # Not available in external data source
                "person": {"email": email},
            }
            # Add phone if available
            if phone:
                notion_user["person"]["phone"] = phone
        else:
            # If no email, treat as bot
            user_type = "bot"
            notion_user = {
                "object": "user",
                "id": user_id,
                "type": user_type,
                "name": name,
                "avatar_url": None,
                "bot": {
                    "owner": {"type": "workspace", "workspace": True},
                    "workspace_name": "Default Workspace",
                },
            }

        # Add optional fields if available
        if persona:
            notion_user["persona"] = persona
        if country_code:
            notion_user["country_code"] = country_code

        notion_user["request_id"] = str(uuid.uuid4())
        result_text = json.dumps(notion_user, indent=2)

    else:  # markdown format
        # Build markdown format response for readable display
        lines = []
        lines.append(f"# User Profile")
        lines.append("")
        lines.append(f"**User ID:** `{user_id}`")
        lines.append("")
        lines.append(f"**Name:** {name}")
        lines.append("")

        if email:
            lines.append(f"**Email:** {email}")
            lines.append("")

        if phone:
            lines.append(f"**Phone:** {phone}")
            lines.append("")

        if persona:
            lines.append(f"**Persona:** {persona}")
            lines.append("")

        if country_code:
            lines.append(f"**Country:** {country_code}")
            lines.append("")

        lines.append("---")
        lines.append("")
        lines.append("*User information retrieved from Notion workspace.*")

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
