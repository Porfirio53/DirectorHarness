"""
Shared utility functions for Obsidian tool simulators.

This module provides common helper functions for all Obsidian tool implementations,
including data loading, file searching, and JSONL file manipulation.
"""

import json
import os
from datetime import datetime
from typing import List, Dict, Optional, Tuple


def get_data_file_path(file_type: str) -> str:
    """
    Get the absolute path to a data file.

    Args:
        file_type (str): Type of data file ('clinic', 'recipes', or 'web3_diary')

    Returns:
        str: Absolute path to the data file

    Raises:
        ValueError: If OBSIDIAN_SANDBOX_PATHS environment variable is not set or invalid
    """
    env_var = os.environ.get("OBSIDIAN_SANDBOX_PATHS")
    if not env_var:
        raise ValueError("Environment variable OBSIDIAN_SANDBOX_PATHS is not set")

    try:
        paths = json.loads(env_var)
    except json.JSONDecodeError as e:
        raise ValueError(f"OBSIDIAN_SANDBOX_PATHS contains invalid JSON: {e}")

    if not isinstance(paths, list) or len(paths) == 0:
        raise ValueError("OBSIDIAN_SANDBOX_PATHS must be a non-empty list")

    return paths[0]


def load_all_records() -> List[Dict]:
    """
    Load all records from the three data files.

    Returns:
        list: List of all records, each with '_data_file' field indicating source
    """
    records = []
    for file_type in ["clinic"]:
        file_path = get_data_file_path(file_type)
        try:
            with open(file_path, "r", encoding="utf-8") as f:
                for line in f:
                    if line.strip():
                        record = json.loads(line)
                        record["_data_file"] = file_type
                        records.append(record)
        except FileNotFoundError:
            pass
    return records


def find_record_by_filepath(
    filepath: str, records: Optional[List[Dict]] = None
) -> Optional[Dict]:
    """
    Find a record by its relative path.

    Args:
        filepath (str): File path to search for
        records (list): Optional list of records (to avoid reloading)

    Returns:
        dict or None: Matched record with '_index' field, or None if not found
    """
    if records is None:
        records = load_all_records()

    for idx, record in enumerate(records):
        if record.get("relative_path") == filepath:
            record["_index"] = idx
            return record
    return None


def update_record_in_file(record: Dict) -> bool:
    """
    Update a record in its JSONL source file.

    Args:
        record (dict): Record to update (must include '_data_file' and 'relative_path')

    Returns:
        bool: True if update was successful, False otherwise
    """
    file_type = record.get("_data_file")
    if not file_type:
        return False

    file_path = get_data_file_path(file_type)
    filepath = record.get("relative_path")

    # Read all records from the file
    records = []
    try:
        with open(file_path, "r", encoding="utf-8") as f:
            for line in f:
                if line.strip():
                    records.append(json.loads(line))
    except FileNotFoundError:
        return False

    # Find and update the record
    updated = False
    for i, r in enumerate(records):
        if r.get("relative_path") == filepath:
            # Update modified_time
            if "metadata" not in record:
                record["metadata"] = {}
            record["metadata"]["modified_time"] = datetime.utcnow().strftime(
                "%Y-%m-%dT%H:%M:%SZ"
            )

            # Remove internal fields
            record_copy = {
                k: v for k, v in record.items() if k not in ["_data_file", "_index"]
            }
            records[i] = record_copy
            updated = True
            break

    if not updated:
        return False

    # Rewrite the entire file
    with open(file_path, "w", encoding="utf-8") as f:
        for r in records:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")

    return True


def delete_record_from_file(filepath: str, file_type: str) -> bool:
    """
    Delete a record from its JSONL source file.

    Args:
        filepath (str): File path to delete
        file_type (str): Data file type ('clinic', 'recipes', or 'web3_diary')

    Returns:
        bool: True if deletion was successful, False otherwise
    """
    file_path = get_data_file_path(file_type)

    # Read all records from the file
    records = []
    try:
        with open(file_path, "r", encoding="utf-8") as f:
            for line in f:
                if line.strip():
                    records.append(json.loads(line))
    except FileNotFoundError:
        return False

    # Filter out the record to be deleted
    original_count = len(records)
    records = [r for r in records if r.get("relative_path") != filepath]

    if len(records) == original_count:
        return False  # Record not found

    # Rewrite the file
    with open(file_path, "w", encoding="utf-8") as f:
        for r in records:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")

    return True


def extract_context(
    content: str, query: str, match_pos: int, context_length: int
) -> str:
    """
    Extract context around a match position.

    Args:
        content (str): Full content string
        query (str): Query string that was matched
        match_pos (int): Position where match starts
        context_length (int): Number of characters to extract

    Returns:
        str: Extracted context string
    """
    if context_length <= 0:
        return content

    half_length = context_length // 2
    start = max(0, match_pos - half_length)
    end = min(len(content), match_pos + len(query) + half_length)

    context = content[start:end]

    # Add ellipsis if truncated
    if start > 0:
        context = "..." + context
    if end < len(content):
        context = context + "..."

    return context


def calculate_score(match_position: int, content_length: int) -> float:
    """
    Calculate a relevance score for a match.

    Args:
        match_position (int): Position of the match in the content
        content_length (int): Total length of the content

    Returns:
        float: Relevance score (lower is better)
    """
    # Normalize position to 0-1 range
    if content_length == 0:
        return 0.0

    normalized_pos = match_position / content_length

    # Calculate score (prefer matches earlier in the document)
    score = -normalized_pos

    # Round to 4 decimal places
    return round(score, 4)


def validate_period(period: str) -> Tuple[bool, Optional[str]]:
    """
    Validate period parameter for periodic notes.

    Args:
        period (str): Period type to validate

    Returns:
        tuple: (is_valid, error_message)
    """
    valid_periods = ["daily", "weekly", "monthly", "quarterly", "yearly"]

    if not period:
        return True, None  # period is optional in some contexts

    if period not in valid_periods:
        return (
            False,
            f"Invalid period value. Must be one of: {', '.join(valid_periods)}",
        )

    return True, None


def format_error_response(error_message: str) -> Dict:
    """
    Format an error response in the standard structure.

    Args:
        error_message (str): Error message to return

    Returns:
        dict: Formatted error response
    """
    return {
        "meta": None,
        "content": [
            {"type": "text", "text": error_message, "annotations": None, "meta": None}
        ],
        "structuredContent": None,
        "isError": True,
    }


def format_success_response(content_text: str) -> Dict:
    """
    Format a success response in the standard structure.

    Args:
        content_text (str): Content to return

    Returns:
        dict: Formatted success response
    """
    return {
        "meta": None,
        "content": [
            {"type": "text", "text": content_text, "annotations": None, "meta": None}
        ],
        "structuredContent": None,
        "isError": False,
    }
