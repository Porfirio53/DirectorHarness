from pathlib import Path
import os
import json
import re
import uuid
import secrets
import base64
from datetime import datetime, timezone
from xiaohongshu.pycode.dynamic_context_handler import (
    load_context,
    save_context,
    get_entity_by_path,
    list_entities_by_path,
    create_entity_by_path,
    update_entity_by_path,
    delete_entity_by_path,
)


def analyze_response_patterns(parameters_used):
    try:
        # 1. Basic input format validation (types, required fields, etc.)
        if not isinstance(parameters_used, dict):
            return {
                "success": False,
                "error": "Input must be a JSON object",
                "result": None,
            }

        allowed_keys = {"title", "content", "images", "tags"}
        extra_keys = set(parameters_used.keys()) - allowed_keys
        if extra_keys:
            return {
                "success": False,
                "error": f"Unexpected fields: {', '.join(sorted(extra_keys))}",
                "result": None,
            }

        required_fields = ["title", "content", "images"]
        for field in required_fields:
            if field not in parameters_used:
                return {
                    "success": False,
                    "error": f"Missing required field: {field}",
                    "result": None,
                }

        title = parameters_used.get("title")
        content = parameters_used.get("content")
        images = parameters_used.get("images")
        tags = parameters_used.get("tags", [])

        if not isinstance(title, str) or not title.strip():
            return {
                "success": False,
                "error": "title must be a non-empty string",
                "result": None,
            }
        if not isinstance(content, str):
            return {
                "success": False,
                "error": "content must be a string",
                "result": None,
            }
        if not isinstance(images, list) or len(images) == 0:
            return {
                "success": False,
                "error": "images must be a non-empty array of strings",
                "result": None,
            }
        if not all(isinstance(img, str) and img.strip() for img in images):
            return {
                "success": False,
                "error": "Every image path in images must be a non-empty string",
                "result": None,
            }
        if tags is not None and (
            not isinstance(tags, list) or not all(isinstance(t, str) for t in tags)
        ):
            return {
                "success": False,
                "error": "tags must be an array of strings if provided",
                "result": None,
            }

        # Enforce title constraints: max 20 Chinese characters or English words
        title_stripped = title.strip()
        # If there are spaces and mainly ASCII words, enforce word count
        english_word_matches = re.findall(
            r"[A-Za-z0-9]+(?:['-][A-Za-z0-9]+)?", title_stripped
        )
        if len(english_word_matches) > 1 or (
            " " in title_stripped and len(english_word_matches) >= 1
        ):
            if len(english_word_matches) > 20:
                return {
                    "success": False,
                    "error": "title exceeds the limit of 20 English words",
                    "result": None,
                }
        else:
            # Treat as Chinese or continuous text, limit by non-space characters
            non_space_chars = re.sub(r"\s+", "", title_stripped)
            if len(non_space_chars) > 20:
                return {
                    "success": False,
                    "error": "title exceeds the limit of 20 characters",
                    "result": None,
                }

        # Move hashtags from content to tags (content should not include #tags)
        hashtags_in_content = re.findall(r"#([^\s#]+)", content)
        cleaned_content = re.sub(r"#[^\s#]+", "", content).strip()

        # Normalize tags: strip whitespace and leading '#', remove empties and de-duplicate preserving order
        normalized_tags = []
        seen = set()
        combined_tags = (tags or []) + hashtags_in_content
        for t in combined_tags:
            if not isinstance(t, str):
                continue
            t_norm = t.strip()
            if t_norm.startswith("#"):
                t_norm = t_norm.lstrip("#").strip()
            if t_norm and t_norm not in seen:
                normalized_tags.append(t_norm)
                seen.add(t_norm)

        # 2. Load all context data and select context(s) based on context_id from environment variable
        tool_name = "xiaohongshu:publish_content"
        tool_name_lower = tool_name.lower() if tool_name else ""
        is_query_tool = any(
            keyword in tool_name_lower
            for keyword in ["get", "list", "query", "search", "batch"]
        )

        context_file_path = Path(
            json.loads(os.environ.get("XIAOHONGSHU_SANDBOX_PATHS"))[0]
        )
        all_context_data = load_context(
            context_file_path
        )  # Returns Dict[{context_id}: {context_dict}] or List[Dict]

        # Read context_id from environment variable (NOT from input parameters)
        context_id = os.environ.get("XIAOHONGSHU_CONTEXT_ID")

        # Determine tool type from tool_name or description to set appropriate default
        # Get context(s) based on context_id
        if context_id is None:
            # Default behavior based on tool type:
            if is_query_tool:
                context_id = "all"  # Query tools (get/list) can access any context
            else:
                # Modify tools: use first context (first key in dict, or first element in list)
                if isinstance(all_context_data, dict) and len(all_context_data) > 0:
                    context_id = list(all_context_data.keys())[0]
                else:
                    context_id = None

        # Get context(s) based on context_id
        if context_id == "all":
            # Use all contexts (for query tools that need to access any context)
            if isinstance(all_context_data, dict):
                context_data = list(
                    all_context_data.values()
                )  # Return list of all context dicts
            elif isinstance(all_context_data, list):
                context_data = all_context_data
            else:
                context_data = [all_context_data]
        elif isinstance(all_context_data, dict):
            # Context is dict format: {context_id: context_dict, ...}
            if context_id in all_context_data:
                context_data = all_context_data[context_id]  # Get single context dict
            else:
                # Context ID not found, return error
                return {
                    "success": False,
                    "error": f"Context ID '{context_id}' not found",
                    "result": None,
                }
        elif isinstance(all_context_data, list):
            # Context is list format (backward compatibility): [{context_dict}, ...]
            if context_id is None:
                context_data = all_context_data[0] if len(all_context_data) > 0 else {}
            else:
                # Try to find context by user_id field
                for ctx in all_context_data:
                    if isinstance(ctx, dict) and ctx.get("user_id") == context_id:
                        context_data = ctx
                        break
                else:
                    return {
                        "success": False,
                        "error": f"Context ID '{context_id}' not found",
                        "result": None,
                    }
        else:
            # Single context dict
            context_data = all_context_data

        # Prevent modify operations on multiple contexts when XIAOHONGSHU_CONTEXT_ID == "all"
        if not is_query_tool and isinstance(context_data, list):
            return {
                "success": False,
                "error": "XIAOHONGSHU_CONTEXT_ID 'all' is not supported for modify operations. Set XIAOHONGSHU_CONTEXT_ID to a specific xiaohongshu context.",
                "result": None,
            }

        if not isinstance(context_data, dict):
            return {
                "success": False,
                "error": "Invalid context format for modify operation",
                "result": None,
            }

        # 3. Validate entity references exist in context (not applicable for this tool)

        # 4. Perform operation (create)
        # Generate 24-character hex ID (matching context.json format)
        note_id = uuid.uuid4().hex[:24]

        # Generate xsecToken (43 chars base64 + "=" suffix, matching context.json format)
        token_bytes = secrets.token_bytes(32)
        xsec_token = base64.urlsafe_b64encode(token_bytes).decode("utf-8")[:43] + "="

        # Generate millisecond timestamp
        timestamp_ms = int(datetime.now(timezone.utc).timestamp() * 1000)

        # Get existing feeds count for index calculation
        existing_feeds = context_data.get("feeds", {})
        next_index = len(existing_feeds) + 1

        # Construct imageList
        image_list = {}
        for i, img_url in enumerate(images):
            image_list[f"img_{i}"] = {
                "width": 1080,
                "height": 1920,
                "urlDefault": img_url,
                "urlPre": img_url,
            }

        # Get cover image (first image)
        cover_url = images[0] if images else ""

        # Construct feed entity matching context.json format
        feed_entity = {
            # Basic metadata
            "id": note_id,
            "modelType": "",
            "time": timestamp_ms,
            "xsecToken": xsec_token,
            # Content fields
            "noteCard.type": "normal",
            "noteCard.displayTitle": title_stripped,
            "noteCard.desc": cleaned_content,
            # Interaction info (initialize as empty)
            "noteCard.interactInfo.liked": False,
            "noteCard.interactInfo.likedCount": "0",
            "noteCard.interactInfo.sharedCount": "0",
            "noteCard.interactInfo.commentCount": "0",
            "noteCard.interactInfo.collectedCount": "0",
            "noteCard.interactInfo.collected": False,
            # Cover image
            "noteCard.cover.width": 1080,
            "noteCard.cover.height": 1920,
            "noteCard.cover.url": "",
            "noteCard.cover.fileId": "",
            "noteCard.cover.urlPre": cover_url,
            "noteCard.cover.urlDefault": cover_url,
            # Image list
            "noteCard.imageList": image_list,
            # Comments (initialize as empty)
            "comments": {"cursor": "", "hasMore": False},
            # Extended fields
            "index": next_index,
            "liked": False,
        }

        # Create under path "feeds" (at context root level)
        create_entity_by_path(context_data, "feeds", feed_entity, note_id)

        # 5. Save context if modified
        # Get context_id from environment variable (same as when loading)
        context_id = os.environ.get("XIAOHONGSHU_CONTEXT_ID")
        if context_id is None:
            # Use first key if dict, or first element if list
            if isinstance(all_context_data, dict) and len(all_context_data) > 0:
                context_id = list(all_context_data.keys())[0]

        # Update the modified context back to all_context_data
        if isinstance(all_context_data, dict):
            # Dict format: {context_id: context_dict, ...}
            if context_id and context_id != "all" and context_id in all_context_data:
                all_context_data[context_id] = (
                    context_data  # Update the specific context
                )
        elif isinstance(all_context_data, list):
            # List format (backward compatibility): [{context_dict}, ...]
            if isinstance(context_data, dict):
                user_id = context_data.get("user_id")
                if user_id:
                    for i, ctx in enumerate(all_context_data):
                        if isinstance(ctx, dict) and ctx.get("user_id") == user_id:
                            all_context_data[i] = context_data
                            break
        else:
            # Single context dict
            all_context_data = context_data

        save_context(context_file_path, all_context_data)

        # 6. Format and return response
        response_payload = {
            "id": note_id,
            "modelType": "",
            "time": timestamp_ms,
            "xsecToken": xsec_token,
            "noteCard.type": "normal",
            "noteCard.displayTitle": title_stripped,
            "noteCard.desc": cleaned_content,
            "noteCard.interactInfo.liked": False,
            "noteCard.interactInfo.likedCount": "0",
            "noteCard.interactInfo.sharedCount": "0",
            "noteCard.interactInfo.commentCount": "0",
            "noteCard.interactInfo.collectedCount": "0",
            "noteCard.interactInfo.collected": False,
            "noteCard.cover.width": 1080,
            "noteCard.cover.height": 1920,
            "noteCard.cover.url": "",
            "noteCard.cover.fileId": "",
            "noteCard.cover.urlPre": cover_url,
            "noteCard.cover.urlDefault": cover_url,
            "noteCard.imageList": image_list,
            "comments": {"cursor": "", "hasMore": False},
            "index": next_index,
            "liked": False,
        }

        result_wrapper = {
            "meta": None,
            "content": [
                {
                    "type": "text",
                    "text": json.dumps(response_payload, ensure_ascii=False),
                }
            ],
            "isError": False,
        }
        return {"success": True, "error": None, "result": result_wrapper}
    except Exception as e:
        return {
            "success": False,
            "error": f"Unexpected error: {str(e)}",
            "result": None,
        }
