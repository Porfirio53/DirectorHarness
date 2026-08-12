"""
Obsidian Get File Contents Tool Simulator

This tool retrieves the content of a single file from the Obsidian vault.
It searches across all three data sources and returns the file content if found.
"""
import json
from obsidian.pycode.shared_utils import (
    load_all_records,
    find_record_by_filepath,
    format_error_response,
    format_success_response
)


def analyze_response_patterns(parameters_used):
    """
    Analyze input parameters and return simulated file content retrieval response.

    This tool retrieves the full content of a single file by searching for its
    relative_path across all three data sources (clinic, recipes, web3_diary).

    Args:
        parameters_used (dict): Input parameters from the tool call
            - filepath (str, required): Path to the file to retrieve

    Returns:
        dict: Response containing:
            - parameters_used: Original input parameters
            - success: True if file found, False otherwise
            - error: Error message if filepath missing or file not found
            - result: Dict with meta, content, structuredContent, isError fields
    """
    global status

    # Extract data from parameters
    data = parameters_used.get('data', {})
    filepath = data.get('filepath')

    # Error pattern 1: Missing required filepath parameter
    if not filepath:
        status = 0
        return {
            "parameters_used": parameters_used,
            "success": False,
            "error": "Missing required parameter: filepath",
            "result": format_error_response("Input validation error: 'filepath' is a required property")
        }

    # Load all records and find the matching file
    all_records = load_all_records()
    record = find_record_by_filepath(filepath, all_records)

    # Error pattern 2: File not found
    if not record:
        status = 0
        return {
            "parameters_used": parameters_used,
            "success": False,
            "error": "File not found",
            "result": format_error_response("Error 40400: Not Found")
        }

    # Success case - return file content
    content = record.get('content', '')

    # Format content as JSON string (preserving newlines and formatting)
    result_text = json.dumps(content,ensure_ascii=False)

    status = 1
    return {
        "parameters_used": parameters_used,
        "success": True,
        "error": None,
        "result": format_success_response(result_text)
    }
