"""
Obsidian Append Content Tool Simulator

This tool appends content to the end of a file.
It actually modifies the JSONL data files to persist the changes.
"""
from obsidian.pycode.shared_utils import (
    load_all_records,
    find_record_by_filepath,
    update_record_in_file,
    format_error_response,
    format_success_response
)


def analyze_response_patterns(parameters_used):
    """
    Analyze input parameters and simulate appending content to a file.

    This tool appends new content to the end of an existing file.
    It actually updates the JSONL data files to persist changes.

    Args:
        parameters_used (dict): Input parameters from the tool call
            - filepath (str, required): Path to the file to append to
            - content (str, required): Content to append

    Returns:
        dict: Response containing:
            - parameters_used: Original input parameters
            - success: True if append succeeded, False otherwise
            - error: Error message if validation fails or file not found
            - result: Dict with meta, content, structuredContent, isError fields
    """
    global status

    # Extract data from parameters
    data = parameters_used.get('data', {})
    filepath = data.get('filepath')
    append_content = data.get('content')

    # Error pattern 1: Missing required filepath parameter
    if not filepath:
        status = 0
        return {
            "parameters_used": parameters_used,
            "success": False,
            "error": "Missing required parameter: filepath",
            "result": format_error_response("Input validation error: 'filepath' is a required property")
        }

    # Error pattern 2: Missing required content parameter
    if not append_content:
        status = 0
        return {
            "parameters_used": parameters_used,
            "success": False,
            "error": "Missing required parameter: content",
            "result": format_error_response("Input validation error: 'content' is a required property")
        }

    # Load all records and find the file
    all_records = load_all_records()
    record = find_record_by_filepath(filepath, all_records)

    # Error pattern 3: File not found
    if not record:
        status = 0
        return {
            "parameters_used": parameters_used,
            "success": False,
            "error": "File not found",
            "result": format_error_response("Error 40400: Not Found")
        }

    # Get current content and append new content
    current_content = record.get('content', '')

    # Ensure proper spacing between current and new content
    if current_content and not current_content.endswith('\n'):
        current_content += '\n'

    # Add extra newline if content doesn't end with one
    if append_content and not append_content.startswith('\n'):
        new_content = current_content + '\n' + append_content
    else:
        new_content = current_content + append_content

    # Update the record in the data file
    record['content'] = new_content
    update_success = update_record_in_file(record)

    if not update_success:
        status = 0
        return {
            "parameters_used": parameters_used,
            "success": False,
            "error": "Failed to update file",
            "result": format_error_response("Error: Failed to persist changes to data file")
        }

    # Success response
    status = 1
    result_text = f"Successfully appended content to {filepath}"
    return {
        "parameters_used": parameters_used,
        "success": True,
        "error": None,
        "result": format_success_response(result_text)
    }
