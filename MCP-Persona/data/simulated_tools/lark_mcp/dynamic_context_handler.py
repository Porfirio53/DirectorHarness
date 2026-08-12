from __future__ import annotations

import json
import os
import uuid
import random
import string
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple, Union, Iterable

# Entity and path configurations
ENTITY_PATHS: Dict[str, str] = {
    "calendar": "calendars",
    "event": "calendars[*].events",
    "attendee": "calendars[*].events[*].attendees",
    "chat": "chats",
    "member": "chats[*].members",
    "message": "chats[*].messages",
}

ID_FIELDS: Dict[str, str] = {
    "calendar": "calendar_id",
    "event": "event_id",
    "attendee": "attendee_id",
    "chat": "chat_id",
    "member": "member_id",
    "message": "message_id",
}

PARENT_RELATIONS: Dict[str, Dict[str, str]] = {
    "event": {"parent": "calendar", "parent_id_field": "calendar_id"},
    "attendee": {"parent": "event", "parent_id_field": "event_id"},
    "member": {"parent": "chat", "parent_id_field": "chat_id"},
    "message": {"parent": "chat", "parent_id_field": "chat_id"},
}

# Mapping from collection names to entity type
COLLECTION_TO_ENTITY: Dict[str, str] = {
    "calendars": "calendar",
    "events": "event",
    "attendees": "attendee",
    "chats": "chat",
    "members": "member",
    "messages": "message",
}


@dataclass
class PathToken:
    name: str
    selector: Optional[str] = None  # None, '*', or specific id string


def load_context(context_file_path: Path) -> Union[Dict[str, Dict[str, Any]], List[Dict[str, Any]]]:
    """
    Load context from JSON file. Supports both dict (new) and list (old) formats.
    - If file does not exist: returns {}.
    - If JSON is a dict: return it directly.
    - If JSON is a list: return the list as-is.
    """
    if not context_file_path.exists():
        # Return empty dict by default
        return {}

    with context_file_path.open("r", encoding="utf-8") as f:
        try:
            data = json.load(f)
        except json.JSONDecodeError:
            # If file is corrupted or invalid JSON, treat as empty dict
            return {}

    if isinstance(data, dict):
        return data  # new format: {"context_id": {context_dict}, ...}
    if isinstance(data, list):
        return data  # old format: [ {context_dict}, ... ]
    # Unexpected format, return empty
    return {}


def save_context(context_file_path: Path, context_data: Union[Dict[str, Dict[str, Any]], List[Dict[str, Any]]]) -> None:
    """
    Save context to JSON file with indent=2 and ensure_ascii=False.
    """
    context_file_path.parent.mkdir(parents=True, exist_ok=True)
    with context_file_path.open("w", encoding="utf-8") as f:
        json.dump(context_data, f, ensure_ascii=False, indent=2)


def parse_path(path: str) -> List[PathToken]:
    """
    Parse a path string like:
      - 'calendars'
      - 'calendars[calendar_id].events'
      - 'calendars[*].events'
      - 'calendars[feishu.cn_xxx@group.calendar.feishu.cn].events'
    Critical: IDs may contain dots. This parser extracts bracket selectors first,
    then splits segments by '.' outside brackets.
    """
    tokens: List[PathToken] = []
    i = 0
    n = len(path)

    while i < n:
        # skip dots
        if path[i] == '.':
            i += 1
            continue

        # Parse segment name until '[' or '.' or end
        j = i
        while j < n and path[j] not in '.[':
            j += 1
        name = path[i:j]
        selector: Optional[str] = None

        # If next char is '[', parse the selector including possible dots
        if j < n and path[j] == '[':
            # find matching closing bracket
            depth = 1
            k = j + 1
            while k < n and depth > 0:
                if path[k] == '[':
                    depth += 1
                elif path[k] == ']':
                    depth -= 1
                k += 1
            if depth == 0:
                # k is position after closing bracket
                selector = path[j + 1 : k - 1]  # Extract content between [ and ]
                i = k  # continue from after ']'
            else:
                # Unmatched bracket; treat as no selector and continue
                i = j
        else:
            # No selector; move pointer to end of this name or dot
            i = j

        if name:
            tokens.append(PathToken(name=name, selector=selector))
        else:
            # Defensive: empty name segment, skip
            pass

    return tokens


def _id_field_for_collection(collection_name: str) -> Optional[str]:
    entity_type = COLLECTION_TO_ENTITY.get(collection_name)
    if not entity_type:
        return None
    return ID_FIELDS.get(entity_type)


def _entity_type_for_collection(collection_name: str) -> Optional[str]:
    return COLLECTION_TO_ENTITY.get(collection_name)


def _iter_collection_items(collection: Any) -> Iterable[Dict[str, Any]]:
    if isinstance(collection, list):
        for item in collection:
            if isinstance(item, dict):
                yield item
    elif isinstance(collection, dict):
        for _, value in collection.items():
            if isinstance(value, dict):
                yield value


def _get_collection_item_by_id(collection: Any, item_id: str, id_field: Optional[str]) -> Optional[Dict[str, Any]]:
    if collection is None:
        return None
    if isinstance(collection, dict):
        # Direct key match
        if item_id in collection and isinstance(collection[item_id], dict):
            return collection[item_id]
        # Search by id_field among values
        if id_field:
            for value in collection.values():
                if isinstance(value, dict) and value.get(id_field) == item_id:
                    return value
    elif isinstance(collection, list):
        if id_field:
            for item in collection:
                if isinstance(item, dict) and item.get(id_field) == item_id:
                    return item
    return None


def _delete_from_collection_by_id(collection: Any, item_id: str, id_field: Optional[str]) -> bool:
    if collection is None:
        return False
    if isinstance(collection, dict):
        if item_id in collection:
            del collection[item_id]
            return True
        if id_field:
            # find key by id_field
            for k, v in list(collection.items()):
                if isinstance(v, dict) and v.get(id_field) == item_id:
                    del collection[k]
                    return True
    elif isinstance(collection, list):
        if id_field:
            for idx, item in enumerate(collection):
                if isinstance(item, dict) and item.get(id_field) == item_id:
                    collection.pop(idx)
                    return True
    return False


def _ensure_collection(parent_node: Dict[str, Any], collection_name: str, prefer: str = "list") -> Any:
    existing = parent_node.get(collection_name)
    if existing is None:
        if prefer == "dict":
            parent_node[collection_name] = {}
        else:
            parent_node[collection_name] = []
        return parent_node[collection_name]
    return existing


def _random_base36(n: int) -> str:
    chars = string.ascii_lowercase + string.digits
    return "".join(random.choice(chars) for _ in range(n))


def generate_entity_id(entity_type: str) -> str:
    """
    Generate IDs based on entity type.
    - calendar: feishu.cn_<8hex>@group.calendar.feishu.cn
    - event: <uuid4>_0
    - chat: oc_<24base36>
    - member: mem_<24hex>
    - message: msg_<32hex>
    - attendee: att_<24hex>
    """
    if entity_type == "calendar":
        return f"feishu.cn_{uuid.uuid4().hex[:8]}@group.calendar.feishu.cn"
    if entity_type == "event":
        return f"{uuid.uuid4()}_0"
    if entity_type == "chat":
        return f"oc_{_random_base36(24)}"
    if entity_type == "member":
        return f"mem_{uuid.uuid4().hex[:24]}"
    if entity_type == "message":
        return f"msg_{uuid.uuid4().hex}"
    if entity_type == "attendee":
        return f"att_{uuid.uuid4().hex[:24]}"
    # Fallback generic id
    return uuid.uuid4().hex


def _find_primary_calendar_id(root_context: Dict[str, Any]) -> Optional[str]:
    calendars = root_context.get("calendars")
    id_field = _id_field_for_collection("calendars") or "calendar_id"
    for cal in _iter_collection_items(calendars):
        if cal.get("is_primary") or cal.get("primary") or cal.get("default") or cal.get("type") == "primary":
            cid = cal.get(id_field)
            if isinstance(cid, str):
                return cid
    return None


def _resolve_special_selector(name: str, selector: Optional[str], root_context: Dict[str, Any]) -> Optional[str]:
    if selector is None or selector == "*" or not isinstance(selector, str):
        return selector
    # Handle 'primary' for calendars
    if name == "calendars" and selector == "primary":
        resolved = _find_primary_calendar_id(root_context)
        return resolved or selector
    return selector


def _traverse_to_nodes(root_context: Dict[str, Any], tokens: List[PathToken], upto_index_exclusive: int) -> List[Dict[str, Any]]:
    """
    Traverse tokens up to but not including index upto_index_exclusive.
    Returns list of nodes (dicts) at that depth.
    """
    nodes: List[Dict[str, Any]] = [root_context]
    for idx in range(upto_index_exclusive):
        token = tokens[idx]
        next_nodes: List[Dict[str, Any]] = []
        for node in nodes:
            if not isinstance(node, dict):
                continue
            resolved_selector = _resolve_special_selector(token.name, token.selector, root_context)
            if resolved_selector is None:
                # Access attribute and expand items if list/dict; if object dict not collection, pass through
                value = node.get(token.name)
                if isinstance(value, list):
                    for item in value:
                        if isinstance(item, dict):
                            next_nodes.append(item)
                elif isinstance(value, dict):
                    # Expand dict values (collection mapping) or treat as a single nested object without id
                    # Both cases we collect dict values and also pass the object itself if next token expects nested keys.
                    # We choose to expand values; if this dict is an object (non-collection), expanding values might not be ideal.
                    # However, next token with attribute name will likely fail gracefully if not present.
                    # Prefer expanding values for collections; also include the dict itself to allow nested attribute access.
                    for v in value.values():
                        if isinstance(v, dict):
                            next_nodes.append(v)
                    # Include the mapping object itself for nested attribute paths
                    next_nodes.append(value)
                elif isinstance(value, dict):
                    next_nodes.append(value)
                else:
                    # Non-dict/list, cannot proceed
                    continue
            elif resolved_selector == "*":
                value = node.get(token.name)
                if isinstance(value, list):
                    for item in value:
                        if isinstance(item, dict):
                            next_nodes.append(item)
                elif isinstance(value, dict):
                    for v in value.values():
                        if isinstance(v, dict):
                            next_nodes.append(v)
                else:
                    continue
            else:
                # Specific ID
                value = node.get(token.name)
                id_field = _id_field_for_collection(token.name)
                item = _get_collection_item_by_id(value, resolved_selector, id_field)
                if item is not None and isinstance(item, dict):
                    next_nodes.append(item)
        nodes = next_nodes
        if not nodes:
            break
    return nodes


def get_entity_by_path(context_data: Dict[str, Any], path: str, entity_id: str) -> Optional[Dict[str, Any]]:
    """
    Get entity by path and ID.
    Path points to a collection, e.g., "calendars" or "calendars[calendar_id].events".
    entity_id is the ID of the entity within the final collection.
    """
    if not isinstance(context_data, dict):
        raise ValueError("get_entity_by_path expects a dict context (single user context).")

    tokens = parse_path(path)
    if not tokens:
        return None

    # Traverse to parent nodes (before the last token)
    parent_nodes = _traverse_to_nodes(context_data, tokens, len(tokens) - 1)
    if not parent_nodes:
        return None

    last = tokens[-1]
    collection_name = last.name
    id_field = _id_field_for_collection(collection_name)
    # We ignore any selector on the last token, using entity_id as the selector
    for parent in parent_nodes:
        if not isinstance(parent, dict):
            continue
        collection = parent.get(collection_name)
        entity = _get_collection_item_by_id(collection, entity_id, id_field)
        if entity is not None:
            return entity
    return None


def list_entities_by_path(context_data: Dict[str, Any], path: str, filters: Dict[str, Any], limit: int) -> List[Dict[str, Any]]:
    """
    List entities by path. Supports wildcard traversal ('[*]').
    Filters is a dict of key -> expected value (top-level keys of entity).
    Returns at most 'limit' entities.
    """
    if not isinstance(context_data, dict):
        raise ValueError("list_entities_by_path expects a dict context (single user context).")

    if limit is not None and limit <= 0:
        return []

    tokens = parse_path(path)
    if not tokens:
        return []

    # Traverse tokens up to but not including last token
    parent_nodes = _traverse_to_nodes(context_data, tokens, len(tokens) - 1)
    if not parent_nodes:
        return []

    last = tokens[-1]

    # Collect entities at the last token
    entities: List[Dict[str, Any]] = []
    for parent in parent_nodes:
        if not isinstance(parent, dict):
            continue

        resolved_selector = _resolve_special_selector(last.name, last.selector, context_data)

        if resolved_selector is None or resolved_selector == "*":
            collection = parent.get(last.name)
            for item in _iter_collection_items(collection):
                entities.append(item)
        else:
            id_field = _id_field_for_collection(last.name)
            collection = parent.get(last.name)
            item = _get_collection_item_by_id(collection, resolved_selector, id_field)
            if item is not None:
                entities.append(item)

    # Apply filters
    if filters:
        def match_filters(entity: Dict[str, Any]) -> bool:
            for k, v in filters.items():
                if entity.get(k) != v:
                    return False
            return True

        entities = [e for e in entities if match_filters(e)]

    # Apply limit
    if limit is not None and limit > 0 and len(entities) > limit:
        entities = entities[:limit]

    return entities


def create_entity_by_path(context_data: Dict[str, Any], path: str, entity_data: Dict[str, Any], entity_id: Optional[str]) -> Dict[str, Any]:
    """
    Create entity at the given path. Path must include parent ID(s) where applicable, e.g.,
    - "calendars" to create a calendar
    - f"calendars[{calendar_id}].events" to create an event
    - f"calendars[{calendar_id}].events[{event_id}].attendees" to create an attendee
    Returns the created entity dict.
    """
    if not isinstance(context_data, dict):
        raise ValueError("create_entity_by_path expects a dict context (single user context).")

    tokens = parse_path(path)
    if not tokens:
        raise ValueError("Invalid path.")

    # Traverse to parent nodes (before last token)
    parent_nodes = _traverse_to_nodes(context_data, tokens, len(tokens) - 1)

    if not parent_nodes:
        raise ValueError("Parent entity not found for creation path.")

    # Path must not be wildcard at parent levels for creation; ensure single parent
    # If multiple parents (wildcards), ambiguous creation
    if len(parent_nodes) != 1:
        raise ValueError("Ambiguous creation path: multiple parent nodes resolved. Provide specific parent IDs.")

    parent_node = parent_nodes[0]
    if not isinstance(parent_node, dict):
        raise ValueError("Parent node is not a dict.")

    last = tokens[-1]
    collection_name = last.name
    entity_type = _entity_type_for_collection(collection_name)
    if not entity_type:
        raise ValueError(f"Unknown collection '{collection_name}' for entity creation.")

    id_field = ID_FIELDS.get(entity_type)
    if not id_field:
        raise ValueError(f"Unknown ID field for entity type '{entity_type}'.")

    # Determine or generate entity ID
    new_id = entity_id if entity_id else entity_data.get(id_field)
    if not new_id:
        new_id = generate_entity_id(entity_type)

    # Prepare entity data
    new_entity = dict(entity_data)
    new_entity[id_field] = new_id

    # Ensure parent exists as required by relations when path uses special selectors like 'primary'
    # This is implicitly validated by traversal. We'll also check if collection exists and initialize if needed.
    # Initialize collection if missing; default to list
    collection = parent_node.get(collection_name)
    prefer_type = "list"
    if isinstance(collection, dict):
        prefer_type = "dict"
    collection = _ensure_collection(parent_node, collection_name, prefer=prefer_type)

    # Prevent duplicate
    existing = _get_collection_item_by_id(collection, new_id, id_field)
    if existing is not None:
        raise ValueError(f"Entity with ID '{new_id}' already exists in '{collection_name}'.")

    # Insert entity
    if isinstance(collection, list):
        collection.append(new_entity)
    elif isinstance(collection, dict):
        collection[new_id] = new_entity
    else:
        raise ValueError("Invalid collection type for insertion.")

    return new_entity


def update_entity_by_path(context_data: Dict[str, Any], path: str, entity_id: str, updates: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """
    Update an entity found by path and entity_id with provided updates (shallow update).
    Returns the updated entity or None if not found.
    """
    if not isinstance(context_data, dict):
        raise ValueError("update_entity_by_path expects a dict context (single user context).")

    entity = get_entity_by_path(context_data, path, entity_id)
    if entity is None:
        return None

    if not isinstance(entity, dict):
        return None

    entity.update(updates or {})
    return entity


def delete_entity_by_path(context_data: Dict[str, Any], path: str, entity_id: str) -> bool:
    """
    Delete an entity identified by path and entity_id.
    Returns True if deletion occurred, False otherwise.
    """
    if not isinstance(context_data, dict):
        raise ValueError("delete_entity_by_path expects a dict context (single user context).")

    tokens = parse_path(path)
    if not tokens:
        return False

    parent_nodes = _traverse_to_nodes(context_data, tokens, len(tokens) - 1)
    if not parent_nodes:
        return False

    last = tokens[-1]
    id_field = _id_field_for_collection(last.name)

    deleted = False
    for parent in parent_nodes:
        if not isinstance(parent, dict):
            continue
        collection = parent.get(last.name)
        if _delete_from_collection_by_id(collection, entity_id, id_field):
            deleted = True
            # Do not break; attempt deletion across all matching parents for wildcard paths
    return deleted


__all__ = [
    "load_context",
    "save_context",
    "get_entity_by_path",
    "list_entities_by_path",
    "create_entity_by_path",
    "update_entity_by_path",
    "delete_entity_by_path",
    "parse_path",
    "generate_entity_id",
    "ENTITY_PATHS",
    "ID_FIELDS",
    "PARENT_RELATIONS",
]