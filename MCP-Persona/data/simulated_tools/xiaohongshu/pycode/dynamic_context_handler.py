from __future__ import annotations

import json
import uuid
import random
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple, Union, Iterable


# Types
ContextDict = Dict[str, Any]
ContextMap = Dict[str, ContextDict]
ContextList = List[ContextDict]
ContextData = Union[ContextMap, ContextList]


@dataclass
class PathToken:
    name: str
    selector: Optional[str] = None


def load_context(context_file_path: Path) -> Union[Dict[str, Dict[str, Any]], List[Dict[str, Any]]]:
    if not context_file_path.exists():
        # Default to new format (dict) for absence
        return {}
    with context_file_path.open("r", encoding="utf-8") as f:
        try:
            data = json.load(f)
        except json.JSONDecodeError:
            return {}
    if isinstance(data, dict):
        return data
    if isinstance(data, list):
        return data
    # If unknown structure, return empty dict for safety
    return {}


def save_context(context_file_path: Path, context_data: Union[Dict[str, Dict[str, Any]], List[Dict[str, Any]]]) -> None:
    context_file_path.parent.mkdir(parents=True, exist_ok=True)
    with context_file_path.open("w", encoding="utf-8") as f:
        json.dump(context_data, f, ensure_ascii=False, indent=2)


def parse_path(path: str) -> List[PathToken]:
    # Parse brackets FIRST, then split by '.' outside brackets
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
            if path[segment_end] == '[':
                bracket_start = segment_end
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
                # Now move until next '.' or end after the closing bracket
                while segment_end < n and path[segment_end] != '.':
                    segment_end += 1
                break
            elif path[segment_end] == '.':
                break
            segment_end += 1
        segment = path[i:segment_end]
        if segment:
            # Parse name and selector inside this segment
            # Find bracket positions relative to this segment
            lb = segment.find('[')
            rb = segment.rfind(']')
            if lb != -1 and rb != -1 and rb > lb:
                name = segment[:lb]
                selector = segment[lb + 1:rb]
                tokens.append(PathToken(name=name, selector=selector))
            else:
                tokens.append(PathToken(name=segment, selector=None))
        i = segment_end + (1 if segment_end < n and path[segment_end] == '.' else 0)
    return tokens


def default_id_field(name: str) -> str:
    # Hand-tuned defaults for common entities; else fall back
    explicit = {
        "calendars": "calendar_id",
        "events": "event_id",
        "chats": "chat_id",
        "attendees": "attendee_id",
        "messages": "message_id",
        "users": "user_id",
        "accounts": "account_id",
        "threads": "thread_id",
        "notes": "note_id",
        "tasks": "task_id",
        "projects": "project_id",
    }
    if name in explicit:
        return explicit[name]
    if name.endswith("ies"):
        # e.g., "companies" -> "company_id"
        return f"{name[:-3]}y_id"
    if name.endswith('s') and len(name) > 1:
        return f"{name[:-1]}_id"
    return f"{name}_id"


def generate_id_for_collection(name: str) -> str:
    lname = name.lower()
    if lname == "calendars":
        # feishu.cn_xxx@group.calendar.feishu.cn
        base = uuid.uuid4().hex[:10]
        return f"feishu.cn_{base}@group.calendar.feishu.cn"
    if lname == "events":
        # uuid_0 (uuid style with suffix)
        return f"{uuid.uuid4()}_{random.randint(0,9)}"
    if lname == "chats":
        # oc_xxx
        return f"oc_{uuid.uuid4().hex[:12]}"
    # generic
    return f"id_{uuid.uuid4().hex}"


def _get_value_by_dot_path(d: Dict[str, Any], dot_path: str, default: Any = None) -> Any:
    cur: Any = d
    for part in dot_path.split("."):
        if not isinstance(cur, dict):
            return default
        if part not in cur:
            return default
        cur = cur[part]
    return cur


def _find_in_collection(collection: Any, entity_id: str, id_field: str) -> Optional[Dict[str, Any]]:
    if isinstance(collection, dict):
        val = collection.get(entity_id)
        if isinstance(val, dict):
            return val
        return None
    if isinstance(collection, list):
        for item in collection:
            if isinstance(item, dict) and item.get(id_field) == entity_id:
                return item
    return None


def _delete_in_collection(collection: Any, entity_id: str, id_field: str) -> bool:
    if isinstance(collection, dict):
        if entity_id in collection:
            del collection[entity_id]
            return True
        return False
    if isinstance(collection, list):
        for i, item in enumerate(collection):
            if isinstance(item, dict) and item.get(id_field) == entity_id:
                del collection[i]
                return True
    return False


def _ensure_collection(parent: Dict[str, Any], name: str) -> Any:
    if name not in parent or parent[name] is None:
        # Default to dict for collection
        parent[name] = {}
    return parent[name]


def _traverse_with_ancestry(root: Any, tokens: List[PathToken]) -> List[Tuple[Any, Dict[str, Any]]]:
    # Returns list of tuples: (value, ancestry_id_map)
    # ancestry_id_map: mapping of id field name -> id selected along the path
    results: List[Tuple[Any, Dict[str, Any]]] = [(root, {})]
    for token in tokens:
        next_results: List[Tuple[Any, Dict[str, Any]]] = []
        for current, ancestry in results:
            if not isinstance(current, dict):
                continue
            if token.name not in current:
                continue
            sub = current[token.name]
            if token.selector is None:
                # Direct property traversal
                next_results.append((sub, dict(ancestry)))
            elif token.selector == "*":
                # Wildcard: expand over dict values or list items
                if isinstance(sub, dict):
                    id_field = default_id_field(token.name)
                    for key, val in sub.items():
                        new_ancestry = dict(ancestry)
                        new_ancestry[id_field] = key
                        next_results.append((val, new_ancestry))
                elif isinstance(sub, list):
                    id_field = default_id_field(token.name)
                    for item in sub:
                        new_ancestry = dict(ancestry)
                        if isinstance(item, dict) and id_field in item:
                            new_ancestry[id_field] = item.get(id_field)
                        next_results.append((item, new_ancestry))
                else:
                    # Not iterable, skip
                    continue
            else:
                # Specific selector id
                if isinstance(sub, dict):
                    selected = sub.get(token.selector)
                    if selected is not None:
                        new_ancestry = dict(ancestry)
                        id_field = default_id_field(token.name)
                        new_ancestry[id_field] = token.selector
                        next_results.append((selected, new_ancestry))
                elif isinstance(sub, list):
                    id_field = default_id_field(token.name)
                    found: Optional[Dict[str, Any]] = None
                    for item in sub:
                        if isinstance(item, dict) and item.get(id_field) == token.selector:
                            found = item
                            break
                    if found is not None:
                        new_ancestry = dict(ancestry)
                        new_ancestry[id_field] = token.selector
                        next_results.append((found, new_ancestry))
        results = next_results
    return results


def get_entity_by_path(context_data: Dict, path: str, entity_id: str) -> Optional[Dict]:
    tokens = parse_path(path)
    if not tokens:
        # Path points directly to root collection
        if isinstance(context_data, dict):
            # Use entity_id at root
            return context_data.get(entity_id) if isinstance(context_data.get(entity_id), dict) else None
        return None
    # Traverse to final target specified by tokens
    traversed = _traverse_with_ancestry(context_data, tokens)
    if not traversed:
        return None
    # If the final target is a collection, use entity_id to retrieve
    # Otherwise, if a specific entity is already selected, return it
    # In case of multiple (unlikely for get), return first match using entity_id if applicable
    for target, _ancestry in traversed:
        if entity_id is None:
            if isinstance(target, dict):
                return target
            return None
        # Determine collection type
        last_name = tokens[-1].name
        id_field = default_id_field(last_name)
        if isinstance(target, dict) or isinstance(target, list):
            found = _find_in_collection(target, entity_id, id_field)
            if found is not None:
                return found
        # If target is a dict representing an entity and entity_id matches its id field, return it
        if isinstance(target, dict) and target.get(id_field) == entity_id:
            return target
    return None


def list_entities_by_path(context_data: Dict, path: str, filters: Dict, limit: int) -> List[Dict]:
    tokens = parse_path(path)
    results: List[Dict] = []
    if not tokens:
        # list at root level
        containers = [(context_data, {})]
    else:
        containers = _traverse_with_ancestry(context_data, tokens)
    last_name = tokens[-1].name if tokens else ""
    id_field_last = default_id_field(last_name) if last_name else None

    # Flatten containers into entities
    entities_with_ancestry: List[Tuple[Dict, Dict[str, Any]]] = []
    for value, ancestry in containers:
        if isinstance(value, dict):
            # If this dict is a collection, extend with its values
            # Heuristic: treat as collection if values are dicts and keys are ids
            if all(isinstance(v, dict) for v in value.values()) and len(value) >= 1:
                for v in value.values():
                    if isinstance(v, dict):
                        entities_with_ancestry.append((v, ancestry))
            else:
                # Could be single entity; include as is
                entities_with_ancestry.append((value, ancestry))
        elif isinstance(value, list):
            for v in value:
                if isinstance(v, dict):
                    entities_with_ancestry.append((v, ancestry))
        else:
            # Non-dict entity; skip
            continue

    # Filtering
    def match_filters(entity: Dict, ancestry: Dict[str, Any], filters: Dict) -> bool:
        for k, expected in (filters or {}).items():
            actual = _get_value_by_dot_path(entity, k, default=None)
            if actual is None:
                # Try ancestry id mapping
                if k in ancestry:
                    actual = ancestry.get(k)
            if actual != expected:
                return False
        return True

    for ent, anc in entities_with_ancestry:
        if match_filters(ent, anc, filters):
            results.append(ent)
            if limit and len(results) >= limit:
                break
    return results


def create_entity_by_path(context_data: Dict, path: str, entity_data: Dict, entity_id: str) -> Dict:
    tokens = parse_path(path)
    if not tokens:
        raise ValueError("Path must not be empty for creation")
    # Parent tokens: all but last; last token is the collection name where to insert
    parent_tokens = tokens[:-1]
    collection_token = tokens[-1]
    # Validate: collection token should be a name (may not need selector here)
    collection_name = collection_token.name
    # Traverse to parent entity
    if parent_tokens:
        traversed = _traverse_with_ancestry(context_data, parent_tokens)
        if not traversed:
            raise KeyError("Parent path does not exist")
        # Using first traversed parent (should be unique when specific id is provided)
        parent, _ = traversed[0]
    else:
        parent = context_data
    if not isinstance(parent, dict):
        raise TypeError("Parent is not a dictionary structure")
    # Ensure collection exists
    collection = parent.get(collection_name)
    if collection is None:
        # Initialize as dict
        collection = {}
        parent[collection_name] = collection
    id_field = default_id_field(collection_name)
    # Generate entity_id if not provided
    if not entity_id:
        entity_id = generate_id_for_collection(collection_name)
    # Insert entity
    if isinstance(collection, dict):
        if entity_id in collection:
            raise KeyError(f"Entity with id {entity_id} already exists in {collection_name}")
        # Ensure id field consistency
        if isinstance(entity_data, dict) and id_field not in entity_data:
            entity_data[id_field] = entity_id
        collection[entity_id] = entity_data
        return entity_data
    elif isinstance(collection, list):
        # Check duplicates
        for item in collection:
            if isinstance(item, dict) and item.get(id_field) == entity_id:
                raise KeyError(f"Entity with id {entity_id} already exists in {collection_name}")
        if isinstance(entity_data, dict) and id_field not in entity_data:
            entity_data[id_field] = entity_id
        collection.append(entity_data)
        return entity_data
    else:
        # Unexpected type; convert to dict
        new_collection: Dict[str, Any] = {}
        if isinstance(entity_data, dict) and id_field not in entity_data:
            entity_data[id_field] = entity_id
        new_collection[entity_id] = entity_data
        parent[collection_name] = new_collection
        return entity_data


def update_entity_by_path(context_data: Dict, path: str, entity_id: str, updates: Dict) -> Optional[Dict]:
    tokens = parse_path(path)
    if not tokens:
        return None
    parent_tokens = tokens
    traversed = _traverse_with_ancestry(context_data, parent_tokens)
    if not traversed:
        return None
    last_name = tokens[-1].name
    id_field = default_id_field(last_name)
    for target, _anc in traversed:
        if isinstance(target, dict) or isinstance(target, list):
            entity = _find_in_collection(target, entity_id, id_field)
            if entity is not None and isinstance(entity, dict):
                entity.update(updates or {})
                return entity
        # If target is a dict representing an entity itself
        if isinstance(target, dict) and target.get(id_field) == entity_id:
            target.update(updates or {})
            return target
    return None


def delete_entity_by_path(context_data: Dict, path: str, entity_id: str) -> bool:
    tokens = parse_path(path)
    if not tokens:
        return False
    traversed = _traverse_with_ancestry(context_data, tokens)
    if not traversed:
        return False
    last_name = tokens[-1].name
    id_field = default_id_field(last_name)
    deleted = False
    for target, _anc in traversed:
        if isinstance(target, dict) or isinstance(target, list):
            if _delete_in_collection(target, entity_id, id_field):
                deleted = True
    return deleted