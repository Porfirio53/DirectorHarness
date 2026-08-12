from __future__ import annotations

import json
import os
import random
import string
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional, Union


@dataclass
class PathToken:
    name: str
    selector: Optional[str]  # None for no selector, '*' for wildcard, or specific ID


def parse_path(path: str) -> List[PathToken]:
    tokens: List[PathToken] = []
    i = 0
    while i < len(path):
        if path[i] == '.':
            i += 1
            continue
        segment_end = i
        bracket_start = -1
        bracket_end = -1
        while segment_end < len(path):
            if path[segment_end] == '[':
                bracket_start = segment_end
                bracket_end = bracket_start + 1
                depth = 1
                while bracket_end < len(path) and depth > 0:
                    if path[bracket_end] == '[':
                        depth += 1
                    elif path[bracket_end] == ']':
                        depth -= 1
                    bracket_end += 1
                if depth == 0:
                    bracket_end -= 1
                segment_end = bracket_end + 1
                # Continue scanning until '.' or end
                while segment_end < len(path) and path[segment_end] != '.':
                    segment_end += 1
                break
            elif path[segment_end] == '.':
                break
            segment_end += 1
        segment = path[i:segment_end]
        if segment:
            if '[' in segment and ']' in segment:
                name = segment[:segment.index('[')]
                selector = segment[segment.index('[') + 1 : segment.rindex(']')]
                tokens.append(PathToken(name=name, selector=selector))
            else:
                tokens.append(PathToken(name=segment, selector=None))
        i = segment_end
        if i < len(path) and path[i] == '.':
            i += 1
    return tokens


def load_context(context_file_path: Path) -> Union[Dict[str, Dict[str, Any]], List[Dict[str, Any]]]:
    if not context_file_path.exists():
        return {}
    try:
        with context_file_path.open("r", encoding="utf-8") as f:
            data = json.load(f)
            if isinstance(data, dict):
                return data
            elif isinstance(data, list):
                return data
            else:
                return {}
    except Exception:
        return {}


def save_context(
    context_file_path: Path,
    context_data: Union[Dict[str, Dict[str, Any]], List[Dict[str, Any]]],
) -> None:
    context_file_path.parent.mkdir(parents=True, exist_ok=True)
    with context_file_path.open("w", encoding="utf-8") as f:
        json.dump(context_data, f, ensure_ascii=False, indent=2)


def select_context(
    context_data: Union[Dict[str, Dict[str, Any]], List[Dict[str, Any]]]
) -> Union[Dict[str, Any], List[Dict[str, Any]]]:
    context_id = os.environ.get("INSTAGRAM_CONTEXT_ID")
    if isinstance(context_data, dict):
        if context_id == "all":
            return list(context_data.values())
        if context_id:
            return context_data.get(context_id, {})
        # No context_id -> return empty dict (single user not specified)
        return {}
    elif isinstance(context_data, list):
        if context_id == "all" or not context_id:
            return context_data
        # Backward compatibility: find by "user_id", with fallback to "id"
        for user in context_data:
            uid = user.get("user_id")
            if uid is None:
                uid = user.get("id")
            if uid is not None and str(uid) == str(context_id):
                return user
        return {}
    return {}


def _label_key_for_collection(name: str) -> str:
    if name.endswith("s") and len(name) > 1:
        singular = name[:-1]
    else:
        singular = name
    return f"{singular}_id"


def _random_alphanum(n: int = 8) -> str:
    return "".join(random.choice(string.ascii_lowercase + string.digits) for _ in range(n))


def generate_id_by_path(path: str, entity_data: Dict[str, Any]) -> str:
    tokens = parse_path(path)
    if not tokens:
        return str(uuid.uuid4())
    collection_name = tokens[-1].name
    if collection_name == "calendars":
        return f"feishu.cn_{_random_alphanum(10)}@group.calendar.feishu.cn"
    elif collection_name == "events":
        return f"{uuid.uuid4()}_0"
    elif collection_name in ("chats", "chat"):
        return f"oc_{_random_alphanum(12)}"
    else:
        return str(uuid.uuid4())


def _find_in_container(container: Any, entity_id: str) -> Optional[Dict[str, Any]]:
    if entity_id is None:
        return None
    match_id = str(entity_id)
    if isinstance(container, dict):
        val = container.get(match_id)
        if isinstance(val, dict):
            # Ensure returned entity has 'id'
            if "id" not in val:
                res = dict(val)
                res["id"] = match_id
                return res
            return val
        return None
    elif isinstance(container, list):
        for item in container:
            if isinstance(item, dict) and str(item.get("id")) == match_id:
                return item
        return None
    return None


def _traverse_specific_path(
    context_data: Dict[str, Any], tokens: List[PathToken], create_missing: bool = False
) -> Optional[Dict[str, Any]]:
    current: Any = context_data
    for idx, token in enumerate(tokens):
        if not isinstance(current, dict):
            return None
        name = token.name
        sel = token.selector
        if name not in current:
            if create_missing:
                current[name] = {}
            else:
                return None
        collection = current[name]
        if sel is None:
            current = collection
        elif sel == "*":
            # Wildcard not supported in specific traversal
            return None
        else:
            if isinstance(collection, dict):
                if sel not in collection:
                    if create_missing:
                        collection[sel] = {}
                    else:
                        return None
                current = collection[sel]
            elif isinstance(collection, list):
                found = None
                for item in collection:
                    if isinstance(item, dict) and str(item.get("id")) == sel:
                        found = item
                        break
                if found is None:
                    if create_missing:
                        new_item = {"id": sel}
                        collection.append(new_item)
                        found = new_item
                    else:
                        return None
                current = found
            else:
                return None
    return current


def get_entity_by_path(context_data: Dict[str, Any], path: str, entity_id: str) -> Optional[Dict[str, Any]]:
    tokens = parse_path(path)
    if not tokens:
        return None
    last_token = tokens[-1]
    root = _traverse_specific_path(context_data, tokens)
    if root is None:
        return None
    if last_token.selector is None:
        # Path points to a collection; find entity by id inside it
        return _find_in_container(root, entity_id)
    else:
        # Path points to a specific entity
        ent = root
        if not isinstance(ent, dict):
            return None
        if entity_id is None:
            return ent
        if str(ent.get("id")) == str(entity_id):
            return ent
        # If entity has no id, but selector matches, return entity
        if "id" not in ent and str(last_token.selector) == str(entity_id):
            return ent
        return None


def _attach_labels(entity: Dict[str, Any], labels: Dict[str, Any]) -> Dict[str, Any]:
    e = dict(entity)
    for k, v in labels.items():
        if k not in e:
            e[k] = v
    return e


def _collect_entities(context_data: Dict[str, Any], tokens: List[PathToken]) -> List[Dict[str, Any]]:
    def recurse(current: Any, idx: int, labels: Dict[str, Any]) -> List[Dict[str, Any]]:
        if idx >= len(tokens):
            # Current is the endpoint container or entity
            if isinstance(current, dict):
                # If it's a dict of entities
                results: List[Dict[str, Any]] = []
                # Determine if dict represents a mapping of id->entity or a single entity
                # Heuristic: if keys look like IDs and values are dicts, treat as mapping
                # We'll iterate mapping:
                for key, val in current.items():
                    if isinstance(val, dict):
                        ent = dict(val)
                        if "id" not in ent:
                            ent["id"] = key
                        results.append(_attach_labels(ent, labels))
                if not results:
                    # Treat current itself as single entity
                    results.append(_attach_labels(current, labels))
                return results
            elif isinstance(current, list):
                results = []
                for item in current:
                    if isinstance(item, dict):
                        results.append(_attach_labels(item, labels))
                return results
            else:
                # Non-container, return empty
                return []
        token = tokens[idx]
        if not isinstance(current, dict) or token.name not in current:
            return []
        collection = current[token.name]
        if token.selector is None:
            return recurse(collection, idx + 1, labels)
        elif token.selector == "*":
            results: List[Dict[str, Any]] = []
            label_key = _label_key_for_collection(token.name)
            if isinstance(collection, dict):
                for key, child in collection.items():
                    new_labels = dict(labels)
                    new_labels[label_key] = key
                    results.extend(recurse(child, idx + 1, new_labels))
            elif isinstance(collection, list):
                for child in collection:
                    new_labels = dict(labels)
                    if isinstance(child, dict) and "id" in child:
                        new_labels[label_key] = str(child["id"])
                    results.extend(recurse(child, idx + 1, new_labels))
            return results
        else:
            if isinstance(collection, dict):
                if token.selector not in collection:
                    return []
                child = collection[token.selector]
            elif isinstance(collection, list):
                child = None
                for item in collection:
                    if isinstance(item, dict) and str(item.get("id")) == str(token.selector):
                        child = item
                        break
                if child is None:
                    return []
            else:
                return []
            new_labels = dict(labels)
            new_labels[_label_key_for_collection(token.name)] = token.selector
            return recurse(child, idx + 1, new_labels)

    return recurse(context_data, 0, {})


def list_entities_by_path(context_data: Dict[str, Any], path: str, filters: Dict[str, Any], limit: int) -> List[Dict[str, Any]]:
    tokens = parse_path(path)
    if not tokens:
        return []
    entities = _collect_entities(context_data, tokens)
    if filters:
        def match(ent: Dict[str, Any]) -> bool:
            for k, v in filters.items():
                if str(ent.get(k)) != str(v):
                    return False
            return True
        entities = [e for e in entities if match(e)]
    if limit is not None and limit > 0:
        return entities[:limit]
    return entities


def create_entity_by_path(
    context_data: Dict[str, Any],
    path: str,
    entity_data: Dict[str, Any],
    entity_id: Optional[str],
) -> Dict[str, Any]:
    tokens = parse_path(path)
    if not tokens:
        raise ValueError("Invalid path: empty")
    if any(t.selector == "*" for t in tokens):
        raise ValueError("Wildcard not allowed in create path")
    last_token = tokens[-1]
    if last_token.selector is not None:
        raise ValueError("Create path must end at a collection segment without selector (e.g., calendars[calendar_id].events)")
    # Validate parent exists (all tokens except last)
    parent_tokens = tokens[:-1]
    parent_obj = _traverse_specific_path(context_data, parent_tokens, create_missing=False)
    if parent_obj is None:
        raise KeyError("Parent path does not exist")
    # Ensure the collection exists
    if not isinstance(parent_obj, dict):
        raise TypeError("Parent object must be a dictionary to hold child collections")
    collection_name = last_token.name
    if collection_name not in parent_obj:
        parent_obj[collection_name] = {}
    collection = parent_obj[collection_name]
    # Determine ID
    new_id = entity_id if entity_id else generate_id_by_path(path, entity_data)
    if not isinstance(new_id, str):
        new_id = str(new_id)
    # Insert into collection
    if isinstance(collection, dict):
        if new_id in collection:
            raise KeyError(f"Entity with id {new_id} already exists at {path}")
        ent = dict(entity_data)
        ent["id"] = new_id
        collection[new_id] = ent
        return ent
    elif isinstance(collection, list):
        for item in collection:
            if isinstance(item, dict) and str(item.get("id")) == new_id:
                raise KeyError(f"Entity with id {new_id} already exists at {path}")
        ent = dict(entity_data)
        ent["id"] = new_id
        collection.append(ent)
        return ent
    else:
        # If collection is not a container, convert to dict
        ent = dict(entity_data)
        ent["id"] = new_id
        parent_obj[collection_name] = {new_id: ent}
        return ent


def update_entity_by_path(
    context_data: Dict[str, Any],
    path: str,
    entity_id: Optional[str],
    updates: Dict[str, Any],
) -> Optional[Dict[str, Any]]:
    tokens = parse_path(path)
    if not tokens:
        return None
    last_token = tokens[-1]
    root = _traverse_specific_path(context_data, tokens if last_token.selector is not None else tokens[:-1], create_missing=False)
    if root is None:
        return None
    # Determine target
    if last_token.selector is not None:
        # Path points directly to entity
        target = root
        if not isinstance(target, dict):
            return None
        target.update(updates)
        return target
    else:
        # Path points to a collection; find entity by id
        collection = root
        if isinstance(collection, dict):
            if entity_id is None:
                return None
            key = str(entity_id)
            if key not in collection or not isinstance(collection[key], dict):
                return None
            collection[key].update(updates)
            return collection[key]
        elif isinstance(collection, list):
            if entity_id is None:
                return None
            key = str(entity_id)
            for item in collection:
                if isinstance(item, dict) and str(item.get("id")) == key:
                    item.update(updates)
                    return item
            return None
        else:
            return None


def delete_entity_by_path(context_data: Dict[str, Any], path: str, entity_id: Optional[str]) -> bool:
    tokens = parse_path(path)
    if not tokens:
        return False
    last_token = tokens[-1]
    container_parent_tokens = tokens if last_token.selector is not None else tokens[:-1]
    parent = _traverse_specific_path(context_data, container_parent_tokens, create_missing=False)
    if parent is None:
        return False
    if last_token.selector is not None:
        # Delete the entity pointed by selector from its parent collection
        # Need to find the collection that contains this entity (it's the previous token)
        if len(tokens) < 2:
            # No collection info to delete from
            return False
        prev_token = tokens[-2]
        if not isinstance(parent, dict):
            return False
        collection = parent.get(prev_token.name)
        if isinstance(collection, dict):
            if last_token.selector in collection:
                del collection[last_token.selector]
                return True
            return False
        elif isinstance(collection, list):
            idx_to_remove = None
            for idx, item in enumerate(collection):
                if isinstance(item, dict) and str(item.get("id")) == str(last_token.selector):
                    idx_to_remove = idx
                    break
            if idx_to_remove is not None:
                collection.pop(idx_to_remove)
                return True
            return False
        else:
            return False
    else:
        # Path points to a collection; remove by entity_id
        if not isinstance(parent, dict):
            return False
        collection = parent.get(last_token.name)
        if isinstance(collection, dict):
            if entity_id is None:
                return False
            key = str(entity_id)
            if key in collection:
                del collection[key]
                return True
            return False
        elif isinstance(collection, list):
            if entity_id is None:
                return False
            key = str(entity_id)
            idx_to_remove = None
            for idx, item in enumerate(collection):
                if isinstance(item, dict) and str(item.get("id")) == key:
                    idx_to_remove = idx
                    break
            if idx_to_remove is not None:
                collection.pop(idx_to_remove)
                return True
            return False
        else:
            return False