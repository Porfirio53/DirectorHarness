"""
Obsidian Batch Get File Contents Tool Simulator

This tool retrieves contents of multiple files at once.
It concatenates the contents with filename headers.
"""
from obsidian.pycode.shared_utils import (
    load_all_records,
    find_record_by_filepath,
    format_error_response,
    format_success_response
)


def analyze_response_patterns(parameters_used):
    """
    Analyze input parameters and return simulated batch file contents retrieval response.

    This tool retrieves contents of multiple files and concatenates them with
    filename headers for easy reading.

    Args:
        parameters_used (dict): Input parameters from the tool call
            - filepaths (list, required): List of file paths to retrieve

    Returns:
        dict: Response containing:
            - parameters_used: Original input parameters
            - success: True if batch retrieval succeeded
            - error: Error message if validation fails
            - result: Dict with meta, content, structuredContent, isError fields
    """
    global status

    # Extract data from parameters
    data = parameters_used.get('data', {})
    filepaths = data.get('filepaths')

    # Error pattern 1: Missing required filepaths parameter
    if filepaths is None:
        status = 0
        return {
            "parameters_used": parameters_used,
            "success": False,
            "error": "Missing required parameter: filepaths",
            "result": format_error_response("Input validation error: 'filepaths' is a required property")
        }

    # Error pattern 2: filepaths must be a list
    if not isinstance(filepaths, list):
        status = 0
        return {
            "parameters_used": parameters_used,
            "success": False,
            "error": "filepaths must be a list",
            "result": format_error_response("Input validation error: 'filepaths' must be an array")
        }

    # Error pattern 3: Empty array
    if len(filepaths) == 0:
        status = 1
        return {
            "parameters_used": parameters_used,
            "success": True,
            "error": None,
            "result": format_success_response("")
        }

    # Load all records once for efficiency
    all_records = load_all_records()

    # Build concatenated content
    content_parts = []

    for filepath in filepaths:
        # Add filename header
        content_parts.append(f"# {filepath}\n")

        # Find the file
        record = find_record_by_filepath(filepath, all_records)

        if record:
            # Add file content
            file_content = record.get('content', '')
            content_parts.append(file_content)
        else:
            # File not found - add error message
            content_parts.append(f"[Error: File '{filepath}' not found]")

        # Add separator between files
        content_parts.append("\n\n")

    # Combine all parts
    result_text = ''.join(content_parts).strip()

    # Success response
    status = 1
    return {
        "parameters_used": parameters_used,
        "success": True,
        "error": None,
        "result": format_success_response(result_text)
    }
