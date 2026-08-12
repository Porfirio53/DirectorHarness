from __future__ import annotations

import json
import os
import uuid
import random
import string
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple, Union


@dataclass
class PathToken:
    name: str
    selector: Optional[str]  # None, '*', or specific id


def parse_path(path: str) -> List[PathToken]:
    tokens: List[PathToken] = []
    i = 0
    n = len(path)
    while i < n:
        if path[i] == '.':
            i += 1
            continue
        segment_end = i
        bracket_start = -1
        bracket_end = -1

        while segment_end < n:
            ch = path[segment_end]
            if ch == '[':
                bracket_start = segment_end
                # Find matching closing bracket, handling nested brackets (if any)
                bracket_end = bracket_start + 1
                depth = 1
                while bracket_end < n and depth > 0:
                    if path[bracket_end] == '[':
                        depth += 1
                    elif path[bracket_end] == ']':
                        depth -= 1
                    bracket_end += 1
                if depth == 0:
                    bracket_end -= 1
                    segment_end = bracket_end + 1
                # Continue until '.' or end for the segment
                while segment_end < n and path[segment_end] != '.':
                    segment_end += 1
                break
            elif ch == '.':
                break
            segment_end += 1

        segment = path[i:segment_end]
        if not segment:
            i = segment_end + 1
            continue

        if '[' in segment and ']' in segment:
            # Find bracket positions relative to segment
            local_bs = segment.index('[')
            local_be = segment.rindex(']')
            name = segment[:local_bs]
            selector = segment[local_bs + 1: local_be]
            tokens.append(PathToken(name=name, selector=selector))
        else:
            tokens.append(PathToken(name=segment, selector=None))
        i = segment_end + (1 if segment_end < n and path[segment_end] == '.' else 0)
    return tokens


def load_context(context_file_path: Path) -> Union[Dict[str, Dict[str, Any]], List[Dict[str, Any]]]:
    if not context_file_path.exists():
        # Default to dict format when file doesn't exist
        return {}
    try:
        with context_file_path.open('r', encoding='utf-8') as f:
            data = json.load(f)
            if isinstance(data, dict):
                return data
            elif isinstance(data, list):
                return data
            else:
                # Unknown format, default to empty dict
                return {}
    except Exception:
        # On error, return empty dict for safety
        return {}


def save_context(context_file_path: Path, context_data: Union[Dict[str, Dict[str, Any]], List[Dict[str, Any]]]) -> None:
    context_file_path.parent.mkdir(parents=True, exist_ok=True)
    with context_file_path.open('w', encoding='utf-8') as f:
        json.dump(context_data, f, ensure_ascii=False, indent=2)


def _find_in_list_by_id(lst: List[Dict[str, Any]], entity_id: str) -> Optional[Dict[str, Any]]:
    id_keys = ['id', 'event_id', 'calendar_id', 'chat_id', 'user_id']
    for item in lst:
        if not isinstance(item, dict):
            continue
        for k in id_keys:
            if item.get(k) == entity_id:
                return item
    return None


def _get_by_id(container: Any, entity_id: str) -> Optional[Dict[str, Any]]:
    if isinstance(container, dict):
        # direct key lookup
        return container.get(entity_id)
    elif isinstance(container, list):
        # list of objects with id fields
        return _find_in_list_by_id(container, entity_id)
    else:
        return None


def _items_from_container(container: Any, meta: Dict[str, Any]) -> List[Tuple[Dict[str, Any], Dict[str, Any]]]:
    items: List[Tuple[Dict[str, Any], Dict[str, Any]]] = []
    if isinstance(container, dict):
        for v in container.values():
            if isinstance(v, dict):
                items.append((v, meta))
    elif isinstance(container, list):
        for v in container:
            if isinstance(v, dict):
                items.append((v, meta))
    return items


def _apply_filters(items_with_meta: List[Tuple[Dict[str, Any], Dict[str, Any]]], filters: Dict[str, Any], limit: int) -> List[Dict[str, Any]]:
    def match(item: Dict[str, Any], meta: Dict[str, Any]) -> bool:
        for k, v in filters.items():
            if k in item:
                if item.get(k) != v:
                    return False
            elif k in meta:
                if meta.get(k) != v:
                    return False
            elif k.endswith('_id'):
                # Attempt to map filter key to parent meta name (e.g., calendar_id -> calendars)
                parent_name = k[:-3] + 's'
                if meta.get(parent_name) != v:
                    return False
            else:
                return False
        return True

    result: List[Dict[str, Any]] = []
    for item, meta in items_with_meta:
        if match(item, meta):
            result.append(item)
            if len(result) >= limit:
                break
    return result


def get_entity_by_path(context_data: Dict[str, Any], path: str, entity_id: str) -> Optional[Dict[str, Any]]:
    tokens = parse_path(path)
    if not tokens:
        return None

    def traverse(current: Any, idx: int, parent_meta: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        token = tokens[idx]
        container = None

        if token.selector is None:
            # Simple container
            if not isinstance(current, dict):
                return None
            container = current.get(token.name)
            if container is None:
                return None
            if idx == len(tokens) - 1:
                return _get_by_id(container, entity_id)
            else:
                return traverse(container, idx + 1, parent_meta)

        elif token.selector == '*':
            # Wildcard traverse across all children
            if not isinstance(current, dict):
                return None
            container = current.get(token.name)
            if container is None:
                return None
            if isinstance(container, dict):
                for key, child in container.items():
                    res = traverse(child, idx + 1, {**parent_meta, token.name: key})
                    if res is not None:
                        return res
            elif isinstance(container, list):
                for child in container:
                    res = traverse(child, idx + 1, parent_meta)
                    if res is not None:
                        return res
            return None

        else:
            # Specific selector (ID)
            if not isinstance(current, dict):
                return None
            collection = current.get(token.name)
            if collection is None:
                return None
            if isinstance(collection, dict):
                next_current = collection.get(token.selector)
            elif isinstance(collection, list):
                next_current = _find_in_list_by_id(collection, token.selector)
            else:
                next_current = None
            if next_current is None:
                return None
            if idx == len(tokens) - 1:
                return _get_by_id(next_current, entity_id)
            else:
                return traverse(next_current, idx + 1, {**parent_meta, token.name: token.selector})

    return traverse(context_data, 0, {})


def list_entities_by_path(context_data: Dict[str, Any], path: str, filters: Dict[str, Any], limit: int) -> List[Dict[str, Any]]:
    tokens = parse_path(path)
    if not tokens:
        return []

    def collect(current: Any, idx: int, parent_meta: Dict[str, Any]) -> List[Tuple[Dict[str, Any], Dict[str, Any]]]:
        token = tokens[idx]
        if token.selector is None:
            if not isinstance(current, dict):
                return []
            container = current.get(token.name)
            if container is None:
                return []
            if idx == len(tokens) - 1:
                return _items_from_container(container, parent_meta)
            return collect(container, idx + 1, parent_meta)

        elif token.selector == '*':
            if not isinstance(current, dict):
                return []
            container = current.get(token.name)
            if container is None:
                return []
            results: List[Tuple[Dict[str, Any], Dict[str, Any]]] = []
            if isinstance(container, dict):
                for key, child in container.items():
                    results.extend(collect(child, idx + 1, {**parent_meta, token.name: key}))
            elif isinstance(container, list):
                for child in container:
                    results.extend(collect(child, idx + 1, parent_meta))
            return results

        else:
            if not isinstance(current, dict):
                return []
            collection = current.get(token.name)
            if collection is None:
                return []
            if isinstance(collection, dict):
                next_current = collection.get(token.selector)
            elif isinstance(collection, list):
                next_current = _find_in_list_by_id(collection, token.selector)
            else:
                next_current = None
            if next_current is None:
                return []
            if idx == len(tokens) - 1:
                return _items_from_container(next_current, {**parent_meta, token.name: token.selector})
            return collect(next_current, idx + 1, {**parent_meta, token.name: token.selector})

    items_with_meta = collect(context_data, 0, {})
    return _apply_filters(items_with_meta, filters, limit)


def _generate_calendar_id() -> str:
    suffix = ''.join(random.choices(string.ascii_lowercase + string.digits, k=8))
    return f"feishu.cn_{suffix}@group.calendar.feishu.cn"


def _generate_event_id() -> str:
    return f"{uuid.uuid4()}_0"


def _generate_chat_id() -> str:
    return "oc_" + ''.join(random.choices(string.ascii_lowercase + string.digits, k=14))


def _generate_generic_id() -> str:
    return uuid.uuid4().hex


def _generate_id_for_entity_name(name: str) -> str:
    lname = (name or "").lower()
    if lname.startswith("calendar"):
        return _generate_calendar_id()
    elif lname.startswith("event"):
        return _generate_event_id()
    elif lname.startswith("chat"):
        return _generate_chat_id()
    return _generate_generic_id()


def _set_id_field(entity_data: Dict[str, Any], collection_name: str, entity_id: str) -> None:
    lname = (collection_name or "").lower()
    if lname.startswith("calendar"):
        entity_data.setdefault("calendar_id", entity_id)
    elif lname.startswith("event"):
        entity_data.setdefault("event_id", entity_id)
    elif lname.startswith("chat"):
        entity_data.setdefault("chat_id", entity_id)
    else:
        entity_data.setdefault("id", entity_id)


def create_entity_by_path(context_data: Dict[str, Any], path: str, entity_data: Dict[str, Any], entity_id: Optional[str]) -> Dict[str, Any]:
    tokens = parse_path(path)
    if not tokens:
        raise ValueError("Invalid path: empty")

    current = context_data
    for i, token in enumerate(tokens):
        is_last = i == len(tokens) - 1
        if token.selector == '*':
            raise ValueError("Wildcard not allowed in creation path")

        if not isinstance(current, dict):
            raise KeyError(f"Expected dict for segment '{token.name}'")

        # Access collection/container
        container = current.get(token.name)
        if token.selector is None:
            if is_last:
                # This is the entity collection where we will create the entity
                if container is None or not isinstance(container, (dict, list)):
                    # Initialize as dict container
                    current[token.name] = {}
                    container = current[token.name]
                # Normalize container to dict
                if isinstance(container, list):
                    # Convert list to dict keyed by id if possible
                    new_container: Dict[str, Any] = {}
                    for item in container:
                        if isinstance(item, dict):
                            item_id = item.get("id") or item.get("event_id") or item.get("calendar_id") or item.get("chat_id")
                            if item_id:
                                new_container[item_id] = item
                    container = new_container
                    current[token.name] = container
                eid = entity_id or _generate_id_for_entity_name(token.name)
                if isinstance(container, dict) and eid in container:
                    raise ValueError(f"Entity already exists at {token.name}[{eid}]")
                _set_id_field(entity_data, token.name, eid)
                container[eid] = entity_data
                return entity_data
            else:
                # Traverse deeper into nested container
                if container is None:
                    # Parent must exist; do not create intermediate nodes
                    raise KeyError(f"Missing container '{token.name}' in path")
                current = container
        else:
            # Select specific parent entity
            if container is None:
                raise KeyError(f"Missing container '{token.name}' in path")
            next_current = None
            if isinstance(container, dict):
                next_current = container.get(token.selector)
            elif isinstance(container, list):
                next_current = _find_in_list_by_id(container, token.selector)
            else:
                next_current = None
            if next_current is None:
                # Special cases like 'primary' must be present as keys to pass validation
                raise KeyError(f"Parent entity '{token.name}[{token.selector}]' not found")
            current = next_current

    raise ValueError("Invalid creation path")


def update_entity_by_path(context_data: Dict[str, Any], path: str, entity_id: str, updates: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    tokens = parse_path(path)
    if not tokens:
        return None

    current = context_data
    for i, token in enumerate(tokens):
        is_last = i == len(tokens) - 1

        if not isinstance(current, dict):
            return None

        container = current.get(token.name)
        if token.selector is None:
            if container is None:
                return None
            if is_last:
                # This is the collection containing the target entity
                target = _get_by_id(container, entity_id)
                if target is None or not isinstance(target, dict):
                    return None
                target.update(updates)
                return target
            else:
                current = container
        elif token.selector == '*':
            # Wildcard not supported for update; need explicit path
            return None
        else:
            if container is None:
                return None
            if isinstance(container, dict):
                next_current = container.get(token.selector)
            elif isinstance(container, list):
                next_current = _find_in_list_by_id(container, token.selector)
            else:
                next_current = None
            if next_current is None:
                return None
            if is_last:
                # Treat last token as an entity collection that is already selected?
                target = _get_by_id(next_current, entity_id)
                if target is None or not isinstance(target, dict):
                    return None
                target.update(updates)
                return target
            else:
                current = next_current

    return None


def delete_entity_by_path(context_data: Dict[str, Any], path: str, entity_id: str) -> bool:
    tokens = parse_path(path)
    if not tokens:
        return False

    current = context_data
    for i, token in enumerate(tokens):
        is_last = i == len(tokens) - 1

        if not isinstance(current, dict):
            return False

        container = current.get(token.name)
        if token.selector is None:
            if container is None:
                return False
            if is_last:
                if isinstance(container, dict):
                    if entity_id in container:
                        del container[entity_id]
                        return True
                    else:
                        return False
                elif isinstance(container, list):
                    target = _find_in_list_by_id(container, entity_id)
                    if target is None:
                        return False
                    try:
                        container.remove(target)
                        return True
                    except ValueError:
                        return False
                else:
                    return False
            else:
                current = container
        elif token.selector == '*':
            # Wildcard not supported for delete; need explicit path
            return False
        else:
            if container is None:
                return False
            if isinstance(container, dict):
                next_current = container.get(token.selector)
            elif isinstance(container, list):
                next_current = _find_in_list_by_id(container, token.selector)
            else:
                next_current = None
            if next_current is None:
                return False
            if is_last:
                if isinstance(next_current, dict):
                    if entity_id in next_current:
                        del next_current[entity_id]
                        return True
                    else:
                        return False
                elif isinstance(next_current, list):
                    target = _find_in_list_by_id(next_current, entity_id)
                    if target is None:
                        return False
                    try:
                        next_current.remove(target)
                        return True
                    except ValueError:
                        return False
                else:
                    return False
            else:
                current = next_current

    return False


def select_context(context_data: Union[Dict[str, Dict[str, Any]], List[Dict[str, Any]]]) -> Union[Dict[str, Any], List[Dict[str, Any]], None]:
    """
    Context Selection Logic:
    - Reads CONTEXT_ID from environment variable.
    - If context_data is dict format: use context_id as key to query context_data[context_id].
      If CONTEXT_ID == "all": returns list(context_data.values()).
    - If context_data is list format (old): finds by matching "user_id" field.
    """
    context_id = os.environ.get("CONTEXT_ID")
    if context_id is None:
        # No selection; return as-is
        return context_data  # type: ignore

    if isinstance(context_data, dict):
        if context_id == "all":
            return list(context_data.values())
        return context_data.get(context_id)
    elif isinstance(context_data, list):
        if context_id == "all":
            return context_data
        for user_ctx in context_data:
            if isinstance(user_ctx, dict) and user_ctx.get("user_id") == context_id:
                return user_ctx
        return None
    return None