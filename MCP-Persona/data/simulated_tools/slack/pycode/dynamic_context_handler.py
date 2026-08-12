from __future__ import annotations

import json
import os
import uuid
import secrets
import string
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional, Union, Iterable, Tuple


@dataclass
class PathToken:
    name: str
    selector: Optional[str] = None


def parse_path(path: str) -> List[PathToken]:
    """
    Parse a dotted path with optional [selector] on any segment.
    Important: IDs inside brackets may contain dots; we only split by '.' outside brackets.
    Examples:
      calendars -> [PathToken(name='calendars', selector=None)]
      calendars[calendar_id].events -> [PathToken('calendars','calendar_id'), PathToken('events',None)]
      calendars[*].events -> [PathToken('calendars','*'), PathToken('events',None)]
      calendars[feishu.cn_xxx@group.calendar.feishu.cn].events -> correct single selector with dots
    """
    tokens: List[PathToken] = []
    i = 0
    n = len(path)

    while i < n:
        # Skip separator dots
        if path[i] == '.':
            i += 1
            continue

        # Find end of segment (the next '.' that is not inside brackets)
        bracket_depth = 0
        segment_end = i
        while segment_end < n:
            c = path[segment_end]
            if c == '[':
                bracket_depth += 1
            elif c == ']':
                bracket_depth = max(0, bracket_depth - 1)
            elif c == '.' and bracket_depth == 0:
                break
            segment_end += 1

        segment = path[i:segment_end]
        segment = segment.strip()
        if not segment:
            i = segment_end + 1
            continue

        name = segment
        selector: Optional[str] = None
        if '[' in segment:
            b_start = segment.find('[')
            # locate matching closing bracket
            depth = 0
            b_end: Optional[int] = None
            for idx in range(b_start, len(segment)):
                if segment[idx] == '[':
                    depth += 1
                elif segment[idx] == ']':
                    depth -= 1
                    if depth == 0:
                        b_end = idx
                        break
            if b_end is not None and b_end > b_start:
                name = segment[:b_start]
                selector = segment[b_start + 1:b_end]
                # strip spaces
                name = name.strip()
                if selector is not None:
                    selector = selector.strip()
                if selector == "":
                    selector = None
        tokens.append(PathToken(name=name, selector=selector))
        i = segment_end + 1 if segment_end < n and path[segment_end] == '.' else segment_end

    return tokens


def load_context(context_file_path: Path) -> Union[Dict[str, Dict[str, Any]], List[Dict[str, Any]]]:
    """
    Load context from JSON.
    - If file missing: return empty dict {}.
    - If JSON top-level is a dict: return dict (new format).
    - If JSON top-level is a list: return list (old format).
    """
    if not context_file_path.exists():
        return {}

    with context_file_path.open("r", encoding="utf-8") as f:
        try:
            data = json.load(f)
        except json.JSONDecodeError:
            # Corrupt file: treat as empty
            return {}

    if isinstance(data, dict):
        return data
    if isinstance(data, list):
        return data
    # Unknown structure: return empty
    return {}


def save_context(context_file_path: Path, context_data: Union[Dict[str, Dict[str, Any]], List[Dict[str, Any]]]) -> None:
    """
    Save context to JSON with UTF-8 and indentation, preserving non-ASCII characters.
    """
    context_file_path.parent.mkdir(parents=True, exist_ok=True)
    with context_file_path.open("w", encoding="utf-8") as f:
        json.dump(context_data, f, ensure_ascii=False, indent=2)


def _get_child(obj: Any, name: str) -> Any:
    if isinstance(obj, dict):
        return obj.get(name)
    return None


def _iter_container(container: Any) -> Iterable[Any]:
    if isinstance(container, dict):
        return container.values()
    if isinstance(container, list):
        return container
    return []


def _find_in_list_by_id(lst: List[Any], entity_id: str) -> Tuple[Optional[int], Optional[Any]]:
    """
    Find an entity in a list by id heuristics.
    Returns index and the item; if not found returns (None, None).
    Heuristics:
      - Prefer item['id'] == entity_id
      - Else any key ending with '_id' equals entity_id
      - Else item['name'] == entity_id
    """
    for idx, item in enumerate(lst):
        if not isinstance(item, dict):
            continue
        # direct id
        if item.get("id") == entity_id:
            return idx, item
        # any *_id
        for k, v in item.items():
            if k.endswith("_id") and v == entity_id:
                return idx, item
        # name
        if item.get("name") == entity_id:
            return idx, item
    return None, None


def _find_in_container(container: Any, entity_id: str) -> Any:
    """
    Find entity in dict or list container using id heuristics.
    """
    if container is None:
        return None
    if isinstance(container, dict):
        # Direct by key
        if entity_id in container:
            return container.get(entity_id)
        # If keys are not matching, try match values with 'id' field
        for v in container.values():
            if isinstance(v, dict):
                if v.get("id") == entity_id:
                    return v
                for k2, v2 in v.items():
                    if k2.endswith("_id") and v2 == entity_id:
                        return v
    elif isinstance(container, list):
        _, item = _find_in_list_by_id(container, entity_id)
        return item
    return None


def get_entity_by_path(context_data: Dict, path: str, entity_id: str) -> Optional[Dict]:
    """
    Get entity by path and ID.
    - path example: 'calendars' and entity_id=calendar_id
    - nested example: 'calendars[calendar_id].events' and entity_id=event_id
    - if last path segment already selects the entity (e.g., 'calendars[calendar_id]'), entity_id is ignored.
    """
    tokens = parse_path(path)
    if not tokens:
        return None

    current: Any = context_data
    for idx, token in enumerate(tokens):
        container = _get_child(current, token.name)
        if container is None:
            return None

        is_last = (idx == len(tokens) - 1)
        if token.selector is None:
            # No selector on this segment: if last, we need to select entity_id from this container
            if is_last:
                if entity_id is None:
                    return None
                return _find_in_container(container, entity_id)
            else:
                # Move deeper based on container - must choose next token under each item; ambiguous for single-path get
                # For get operation, we cannot resolve ambiguity without selector; return None
                return None
        elif token.selector == "*":
            # Wildcard doesn't make sense for a single entity fetch
            return None
        else:
            selected = _find_in_container(container, token.selector)
            if selected is None:
                return None
            if is_last:
                # Already selected the entity at the last token
                if isinstance(selected, dict):
                    return selected
                # If primitive, wrap? Return as-is
                return selected
            else:
                current = selected
                continue

    return None


def _collect_by_tokens(start: Any, tokens: List[PathToken]) -> List[Any]:
    """
    Traverse using tokens and collect matching entities for listing.
    Supports wildcards and deep nesting.
    """
    def recurse(curr_items: List[Any], idx: int) -> List[Any]:
        if idx >= len(tokens):
            return curr_items

        token = tokens[idx]
        results: List[Any] = []

        for curr in curr_items:
            container = _get_child(curr, token.name)
            if container is None:
                continue

            is_last = (idx == len(tokens) - 1)

            if token.selector is None:
                if is_last:
                    # Return items within this container
                    results.extend(list(_iter_container(container)))
                else:
                    # Continue traversal under each item in the container
                    next_items = list(_iter_container(container))
                    # If container itself is a dict-like or list, and next token references fields inside elements
                    results.extend(recurse(next_items, idx + 1))
            elif token.selector == "*":
                # Iterate all items and continue
                items = list(_iter_container(container))
                if is_last:
                    # Returning items directly
                    results.extend(items)
                else:
                    results.extend(recurse(items, idx + 1))
            else:
                selected = _find_in_container(container, token.selector)
                if selected is None:
                    continue
                if is_last:
                    # If selected is a container, we return it as a single item
                    if isinstance(selected, (dict, list)):
                        # If list or dict, flatten dict values if it's a container of items? We keep as single item to be consistent.
                        results.append(selected)
                    else:
                        results.append(selected)
                else:
                    results.extend(recurse([selected], idx + 1))

        return results

    return recurse([start], 0)


def _apply_filters(entities: List[Dict[str, Any]], filters: Dict[str, Any]) -> List[Dict[str, Any]]:
    if not filters:
        return entities
    filtered: List[Dict[str, Any]] = []
    for e in entities:
        if not isinstance(e, dict):
            continue
        ok = True
        for k, v in filters.items():
            if e.get(k) != v:
                ok = False
                break
        if ok:
            filtered.append(e)
    return filtered


def list_entities_by_path(context_data: Dict, path: str, filters: Dict, limit: int) -> List[Dict]:
    """
    List entities by path with optional filters and limit.
    - Supports wildcards like 'calendars[*].events'
    - Filters are applied after traversal.
    """
    tokens = parse_path(path)
    if not tokens:
        return []

    collected = _collect_by_tokens(context_data, tokens)

    # If collected includes containers (dicts) that are not entity dicts, we keep them.
    entities = [e for e in collected if isinstance(e, dict)]

    # Apply filters
    entities = _apply_filters(entities, filters or {})

    # Apply limit
    if isinstance(limit, int) and limit > 0:
        return entities[:limit]
    elif isinstance(limit, int) and limit == 0:
        return []
    return entities


def _resolve_parent_and_container(context_data: Dict[str, Any], path: str) -> Tuple[Optional[Dict[str, Any]], Optional[PathToken], Optional[Any]]:
    """
    Resolve the parent entity and the last container for create/update/delete operations.
    Returns (parent_entity_dict, last_token, container_at_last_token_name)
    - parent_entity_dict is the dict where last_token.name resides as a key.
    """
    tokens = parse_path(path)
    if not tokens:
        return None, None, None

    parent: Any = context_data
    for token in tokens[:-1]:
        container = _get_child(parent, token.name)
        if container is None:
            return None, None, None
        if token.selector is None:
            # Ambiguous path for mutation
            return None, None, None
        if token.selector == "*":
            # Mutation path cannot use wildcard
            return None, None, None
        selected = _find_in_container(container, token.selector)
        if selected is None:
            return None, None, None
        parent = selected

    last = tokens[-1]
    if not isinstance(parent, dict):
        return None, None, None
    container = parent.get(last.name)
    return parent, last, container


def create_entity_by_path(context_data: Dict, path: str, entity_data: Dict, entity_id: str) -> Dict:
    """
    Create an entity at the specified path. Path must include parent IDs for nested resources.
    Example: create_entity_by_path(ctx, f"calendars[{calendar_id}].events", event_data, event_id)
    """
    parent, last, container = _resolve_parent_and_container(context_data, path)
    if parent is None or last is None:
        raise ValueError("Invalid path or parent not found for creation.")

    if last.selector not in (None, ""):
        raise ValueError("Creation path must refer to a container (no selector) at the last segment.")

    # Ensure container exists
    if container is None:
        # Prefer dict-based keyed container
        parent[last.name] = {}
        container = parent[last.name]

    if isinstance(container, dict):
        if entity_id in container:
            raise ValueError(f"Entity with id '{entity_id}' already exists in container '{last.name}'.")
        # Ensure entity has an id field if sensible
        if isinstance(entity_data, dict):
            entity_data = dict(entity_data)  # copy
            if "id" not in entity_data:
                entity_data["id"] = entity_id
        container[entity_id] = entity_data
        return container[entity_id]
    elif isinstance(container, list):
        # For list container, append
        # Avoid duplicates by heuristics
        idx, _ = _find_in_list_by_id(container, entity_id)
        if idx is not None:
            raise ValueError(f"Entity with id '{entity_id}' already exists in list container '{last.name}'.")
        if isinstance(entity_data, dict) and "id" not in entity_data:
            entity_data = dict(entity_data)
            entity_data["id"] = entity_id
        container.append(entity_data)
        return entity_data
    else:
        raise ValueError(f"Container '{last.name}' is not a valid collection for creation.")


def update_entity_by_path(context_data: Dict, path: str, entity_id: str, updates: Dict) -> Optional[Dict]:
    """
    Update an entity at the specified path with provided updates (shallow update).
    Supports both dict and list containers.
    If last path segment includes a selector, entity_id is ignored.
    """
    parent, last, container = _resolve_parent_and_container(context_data, path)
    if parent is None or last is None or container is None:
        return None

    target_id = last.selector if (last.selector not in (None, "", "*")) else entity_id

    if target_id in (None, "", "*"):
        return None

    if isinstance(container, dict):
        # Prefer direct key lookup
        if target_id in container:
            entity = container[target_id]
            if isinstance(entity, dict):
                entity.update(updates or {})
            return entity
        # Fallback: find by 'id' or *_id
        entity = _find_in_container(container, target_id)
        if entity is None:
            return None
        if isinstance(entity, dict):
            entity.update(updates or {})
        return entity
    elif isinstance(container, list):
        idx, entity = _find_in_list_by_id(container, target_id)
        if idx is None or entity is None:
            return None
        if isinstance(entity, dict):
            entity.update(updates or {})
        else:
            container[idx] = updates  # overwrite primitive
        return container[idx]
    else:
        return None


def delete_entity_by_path(context_data: Dict, path: str, entity_id: str) -> bool:
    """
    Delete an entity at the specified path.
    If last path segment includes a selector, entity_id is ignored.
    """
    parent, last, container = _resolve_parent_and_container(context_data, path)
    if parent is None or last is None or container is None:
        return False

    target_id = last.selector if (last.selector not in (None, "", "*")) else entity_id
    if target_id in (None, "", "*"):
        return False

    if isinstance(container, dict):
        if target_id in container:
            del container[target_id]
            return True
        # Fallback: find by 'id' or *_id
        # Need to find the key to delete
        key_to_delete: Optional[str] = None
        for k, v in container.items():
            if isinstance(v, dict):
                if v.get("id") == target_id:
                    key_to_delete = k
                    break
                for k2, v2 in v.items():
                    if k2.endswith("_id") and v2 == target_id:
                        key_to_delete = k
                        break
            if key_to_delete is not None:
                break
        if key_to_delete is not None:
            del container[key_to_delete]
            return True
        return False
    elif isinstance(container, list):
        idx, _ = _find_in_list_by_id(container, target_id)
        if idx is None:
            return False
        container.pop(idx)
        return True
    else:
        return False


def generate_id_for_path(path: str) -> str:
    """
    Generate an appropriate ID based on the last segment name of the path.
    Rules derived from typical patterns:
      - calendars: feishu.cn_<random>@group.calendar.feishu.cn
      - events: <uuid>_0
      - chats / group_channels: oc_<random>
      - reminders: Rm<random>
      - default: <uuid>
    """
    tokens = parse_path(path)
    last_name = tokens[-1].name if tokens else ""
    lname = last_name.lower()

    rnd = ''.join(secrets.choice(string.ascii_letters + string.digits) for _ in range(12))

    if "calendar" in lname:
        # feishu.cn_xxx@group.calendar.feishu.cn
        left = "feishu.cn_" + ''.join(secrets.choice(string.ascii_lowercase + string.digits) for _ in range(8))
        return f"{left}@group.calendar.feishu.cn"
    if "event" in lname:
        return f"{uuid.uuid4()}_0"
    if "chat" in lname or "channel" in lname:
        return "oc_" + ''.join(secrets.choice(string.ascii_lowercase + string.digits) for _ in range(20))
    if "reminder" in lname:
        return "Rm" + ''.join(secrets.choice(string.ascii_uppercase + string.digits) for _ in range(10))
    return str(uuid.uuid4())