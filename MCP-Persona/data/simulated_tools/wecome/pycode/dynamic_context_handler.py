from __future__ import annotations

import json
import os
import random
import string
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple, Union
from uuid import uuid4


@dataclass
class PathToken:
    name: str
    selector: Optional[str] = None  # None, '*', or ID string


def parse_path(path: str) -> List[PathToken]:
    """
    Parse a path like 'calendars[feishu.cn_xxx@group.calendar.feishu.cn].events'
    into tokens while handling brackets first, then splitting by '.' outside brackets.
    """
    tokens: List[PathToken] = []
    i = 0
    n = len(path)

    while i < n:
        # Skip leading dots (in case of malformed input or consecutive dots)
        if path[i] == '.':
            i += 1
            continue

        segment_start = i
        segment_end = i
        bracket_start = -1
        bracket_end = -1

        # Find the end of the segment (either next '.' outside brackets or end)
        while segment_end < n:
            ch = path[segment_end]
            if ch == '[':
                # Locate matching closing bracket
                bracket_start = segment_end
                depth = 1
                j = segment_end + 1
                while j < n and depth > 0:
                    if path[j] == '[':
                        depth += 1
                    elif path[j] == ']':
                        depth -= 1
                    j += 1
                if depth == 0:
                    bracket_end = j - 1
                    segment_end = j  # position after closing bracket
                    # The segment may continue until a dot after the bracket
                    if segment_end < n and path[segment_end] == '.':
                        # End of this segment
                        break
                    # Keep scanning for further '.' (unlikely in well-formed paths)
                else:
                    # Unmatched bracket; treat rest as part of segment
                    bracket_end = n - 1
                    segment_end = n
                    break
            elif ch == '.':
                # End of segment outside brackets
                break
            segment_end += 1

        segment = path[segment_start:segment_end]

        if bracket_start != -1 and bracket_end != -1:
            name = segment[: bracket_start - segment_start]
            selector = segment[bracket_start - segment_start + 1 : bracket_end - segment_start]
            tokens.append(PathToken(name=name, selector=selector))
        else:
            tokens.append(PathToken(name=segment, selector=None))

        i = segment_end + (1 if segment_end < n and path[segment_end] == '.' else 0)

    return tokens


def load_context(context_file_path: Path) -> Union[Dict[str, Dict[str, Any]], List[Dict[str, Any]]]:
    """
    Load context from a JSON file.
    - If JSON is a dict: return dict as-is (new format: {"context_id": {context_dict}, ...})
    - If JSON is a list: return list as-is (old format: [{context_dict}, ...])
    - If file doesn't exist: return empty dict {}
    """
    if not context_file_path.exists():
        return {}
    try:
        with context_file_path.open("r", encoding="utf-8") as f:
            data = json.load(f)
        if isinstance(data, dict):
            return data
        if isinstance(data, list):
            return data
        # Fallback if the file contains unexpected types
        return {}
    except Exception:
        return {}


def save_context(
    context_file_path: Path,
    context_data: Union[Dict[str, Dict[str, Any]], List[Dict[str, Any]]],
) -> None:
    """
    Save context to a JSON file with proper formatting.
    """
    context_file_path.parent.mkdir(parents=True, exist_ok=True)
    with context_file_path.open("w", encoding="utf-8") as f:
        json.dump(context_data, f, ensure_ascii=False, indent=2)


def _singularize(name: str) -> str:
    # Simple singularization heuristic
    if name.endswith("ies"):
        return name[:-3] + "y"
    if name.endswith("ses"):
        return name[:-2]
    if name.endswith("s") and len(name) > 1:
        return name[:-1]
    return name


def _parent_id_key(name: str) -> str:
    return f"{_singularize(name)}_id"


def _generate_entity_id(target_collection_name: str) -> str:
    """
    Generate IDs based on common entity naming conventions.
    """
    if target_collection_name.lower() in {"calendar", "calendars"}:
        suffix = "".join(random.choices(string.ascii_lowercase + string.digits, k=8))
        return f"feishu.cn_{suffix}@group.calendar.feishu.cn"
    if target_collection_name.lower() in {"event", "events"}:
        return f"{uuid4()}-{uuid4()}-{uuid4()}-{uuid4()}-{uuid4()}_0"
    if target_collection_name.lower() in {"chat", "chats"}:
        suffix = "".join(random.choices(string.ascii_lowercase + string.digits, k=10))
        return f"oc_{suffix}"
    # Default generic ID
    return "".join(random.choices(string.ascii_lowercase + string.digits, k=16))


def _get_node(container: Any, name: str) -> Any:
    if not isinstance(container, dict):
        return None
    return container.get(name)


def _get_child(container: Any, selector: str) -> Any:
    if not isinstance(container, dict):
        return None
    return container.get(selector)


def _ensure_collection(container: Dict[str, Any], name: str) -> Dict[str, Any]:
    """
    Ensure a sub-collection is a dict and exists at container[name].
    """
    if name not in container or not isinstance(container[name], dict):
        container[name] = {}
    return container[name]


def _matches_filters(entity: Any, filters: Dict[str, Any]) -> bool:
    if not filters:
        return True
    if not isinstance(entity, dict):
        return False
    for k, v in filters.items():
        if entity.get(k) != v:
            return False
    return True


def get_entity_by_path(context_data: Dict[str, Any], path: str, entity_id: str) -> Optional[Dict]:
    """
    Get an entity by path and ID.
    - Path can be 'calendars' (entity_id is calendar_id) or 'calendars[calendar_id].events' (entity_id is event_id)
    - Handles nested paths like 'calendars[calendar_id].events[event_id].attendees'
    - Returns None if not found.
    """
    tokens = parse_path(path)
    # Wildcards are not supported in get
    if any(t.selector == "*" for t in tokens):
        return None

    cur: Any = context_data
    # Traverse all but the last token
    for idx, token in enumerate(tokens[:-1]):
        node = _get_node(cur, token.name)
        if node is None:
            return None
        if token.selector is None:
            cur = node
        else:
            child = _get_child(node, token.selector)
            if child is None:
                return None
            cur = child

    # Handle last token
    if not tokens:
        return None
    last = tokens[-1]
    last_node = _get_node(cur, last.name)
    if last_node is None:
        return None

    # If the last token includes a selector, return that specific entity
    if last.selector is not None:
        ent = _get_child(last_node, last.selector)
        return ent if isinstance(ent, dict) else None

    # Otherwise, last_node is the collection we should search by entity_id
    if isinstance(last_node, dict):
        ent = last_node.get(entity_id)
        return ent if isinstance(ent, dict) else None
    elif isinstance(last_node, list):
        # If it's a list, search by 'id' field
        for item in last_node:
            if isinstance(item, dict) and item.get("id") == entity_id:
                return item
    return None


def list_entities_by_path(
    context_data: Dict[str, Any],
    path: str,
    filters: Dict[str, Any],
    limit: int,
) -> List[Dict]:
    """
    List entities by path with support for wildcard traversal and filtering.
    - Path examples: 'calendars', 'calendars[*].events', 'calendars[calendar_id].events'
    - Filters are applied after collection for wildcard paths.
    - Returns at most 'limit' entities.
    """
    tokens = parse_path(path)
    results: List[Dict] = []

    def traverse(cur: Any, idx: int, context_info: Dict[str, Any]) -> None:
        if idx >= len(tokens):
            return

        token = tokens[idx]
        node = _get_node(cur, token.name)
        if node is None:
            return

        # Wildcard
        if token.selector == "*":
            if not isinstance(node, dict):
                return
            for child_id, child_obj in node.items():
                next_context = dict(context_info)
                next_context[_parent_id_key(token.name)] = child_id
                traverse(child_obj, idx + 1, next_context)
            return

        # Specific selector
        if token.selector is not None:
            if not isinstance(node, dict):
                return
            child = node.get(token.selector)
            if child is None:
                return
            next_context = dict(context_info)
            next_context[_parent_id_key(token.name)] = token.selector
            if idx == len(tokens) - 1:
                # Final entity
                if isinstance(child, dict):
                    ent_copy = dict(child)
                    # Include context info for filtering and downstream use
                    for k, v in next_context.items():
                        if k not in ent_copy:
                            ent_copy[k] = v
                    results.append(ent_copy)
                elif isinstance(child, list):
                    for item in child:
                        if isinstance(item, dict):
                            ent_copy = dict(item)
                            for k, v in next_context.items():
                                if k not in ent_copy:
                                    ent_copy[k] = v
                            results.append(ent_copy)
                return
            else:
                traverse(child, idx + 1, next_context)
            return

        # No selector: node is either a collection or a container to go deeper
        if idx == len(tokens) - 1:
            # Final collection: collect entities from node[name]
            if isinstance(node, dict):
                for eid, ent in node.items():
                    if isinstance(ent, dict):
                        ent_copy = dict(ent)
                        if "id" not in ent_copy:
                            ent_copy["id"] = eid
                        for k, v in context_info.items():
                            if k not in ent_copy:
                                ent_copy[k] = v
                        results.append(ent_copy)
            elif isinstance(node, list):
                for item in node:
                    if isinstance(item, dict):
                        ent_copy = dict(item)
                        for k, v in context_info.items():
                            if k not in ent_copy:
                                ent_copy[k] = v
                        results.append(ent_copy)
            return
        else:
            # Continue traversal
            traverse(node, idx + 1, context_info)

    traverse(context_data, 0, {})

    # Apply filters
    filtered = [ent for ent in results if _matches_filters(ent, filters)]

    # Limit results
    if limit is not None and limit > 0:
        return filtered[:limit]
    return filtered


def create_entity_by_path(
    context_data: Dict[str, Any],
    path: str,
    entity_data: Dict[str, Any],
    entity_id: Optional[str],
) -> Dict:
    """
    Create an entity at the specified path.
    - Path must include parent ID(s) for nested entities, e.g. 'calendars[calendar_id].events'
    - Validates parent existence before creating child entities.
    - Generates an ID if entity_id is None or empty.
    - Returns the created entity dict, or {} if parent doesn't exist or path invalid.
    """
    tokens = parse_path(path)
    if not tokens:
        return {}

    if any(t.selector == "*" for t in tokens):
        # Cannot create using wildcard paths
        return {}

    cur: Any = context_data

    # Traverse parents (all but the last token)
    for token in tokens[:-1]:
        node = _get_node(cur, token.name)
        if node is None or not isinstance(node, dict):
            # Parent collection missing
            return {}
        if token.selector is None:
            # Need a selector to reach a specific parent entity
            return {}
        parent_ent = node.get(token.selector)
        if parent_ent is None or not isinstance(parent_ent, dict):
            # Parent entity doesn't exist
            return {}
        cur = parent_ent

    # Now handle the last token
    last = tokens[-1]
    # The last token should represent a collection (no selector)
    if last.selector is not None:
        # Creating directly at a specific ID via path selector isn't supported
        return {}

    # Ensure the target collection exists
    if not isinstance(cur, dict):
        return {}
    target_collection = _ensure_collection(cur, last.name)

    # Generate ID if not provided
    final_entity_id = entity_id or _generate_entity_id(last.name)

    # If the entity already exists, return it (do not overwrite)
    if final_entity_id in target_collection and isinstance(target_collection[final_entity_id], dict):
        return target_collection[final_entity_id]

    # Create and store entity
    new_entity = dict(entity_data) if isinstance(entity_data, dict) else {}
    if "id" not in new_entity:
        new_entity["id"] = final_entity_id
    target_collection[final_entity_id] = new_entity
    return new_entity


def update_entity_by_path(
    context_data: Dict[str, Any],
    path: str,
    entity_id: str,
    updates: Dict[str, Any],
) -> Optional[Dict]:
    """
    Update an entity by path and ID.
    - Returns the updated entity dict, or None if not found.
    """
    tokens = parse_path(path)
    if not tokens:
        return None
    if any(t.selector == "*" for t in tokens):
        return None

    cur: Any = context_data

    # Traverse parents (all but the last)
    for token in tokens[:-1]:
        node = _get_node(cur, token.name)
        if node is None or not isinstance(node, dict):
            return None
        if token.selector is None:
            cur = node
        else:
            child = node.get(token.selector)
            if child is None or not isinstance(child, dict):
                return None
            cur = child

    last = tokens[-1]
    last_node = _get_node(cur, last.name)
    if last_node is None:
        return None

    # If last token includes a selector, update that specific entity
    if last.selector is not None:
        ent = (
            last_node.get(last.selector)
            if isinstance(last_node, dict)
            else None
        )
        if not isinstance(ent, dict):
            return None
        ent.update(updates or {})
        return ent

    # Otherwise update the entity by entity_id in the final collection
    if isinstance(last_node, dict):
        ent = last_node.get(entity_id)
        if not isinstance(ent, dict):
            return None
        ent.update(updates or {})
        return ent
    elif isinstance(last_node, list):
        for item in last_node:
            if isinstance(item, dict) and item.get("id") == entity_id:
                item.update(updates or {})
                return item
    return None


def delete_entity_by_path(
    context_data: Dict[str, Any],
    path: str,
    entity_id: str,
) -> bool:
    """
    Delete an entity by path and ID.
    - Returns True if deleted, False if not found.
    """
    tokens = parse_path(path)
    if not tokens:
        return False
    if any(t.selector == "*" for t in tokens):
        return False

    cur: Any = context_data

    # Traverse parents (all but the last)
    for token in tokens[:-1]:
        node = _get_node(cur, token.name)
        if node is None or not isinstance(node, dict):
            return False
        if token.selector is None:
            cur = node
        else:
            child = node.get(token.selector)
            if child is None or not isinstance(child, dict):
                return False
            cur = child

    last = tokens[-1]
    last_node = _get_node(cur, last.name)
    if last_node is None:
        return False

    # If last token includes a selector, delete that specific entity
    if last.selector is not None:
        if isinstance(last_node, dict) and last.selector in last_node:
            del last_node[last.selector]
            return True
        return False

    # Otherwise delete by entity_id in the final collection
    if isinstance(last_node, dict):
        if entity_id in last_node:
            del last_node[entity_id]
            return True
    elif isinstance(last_node, list):
        for idx, item in enumerate(last_node):
            if isinstance(item, dict) and item.get("id") == entity_id:
                del last_node[idx]
                return True
    return False