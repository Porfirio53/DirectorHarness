"""
Obsidian Patch Content Tool Simulator

This tool patches content into a specific location within a file.
It supports different target types (heading, block, frontmatter) and operations
(append, prepend, replace). This implementation actually modifies the JSONL data files.
"""
import re
from obsidian.pycode.shared_utils import (
    load_all_records,
    find_record_by_filepath,
    update_record_in_file,
    format_error_response,
    format_success_response
)


def analyze_response_patterns(parameters_used):
    """
    Analyze input parameters and simulate patching content to a file.

    This tool modifies file content by inserting/patching at specific locations.
    It actually updates the JSONL data files to persist changes.

    Args:
        parameters_used (dict): Input parameters from the tool call
            - filepath (str, required): Path to the file to patch
            - content (str, required): Content to insert/patch
            - target_type (str, required): Type of target ('heading', 'block', 'frontmatter')
            - target (str, required): Target identifier (e.g., heading name)
            - operation (str, required): Operation type ('append', 'prepend', 'replace')

    Returns:
        dict: Response containing:
            - parameters_used: Original input parameters
            - success: True if patch succeeded, False otherwise
            - error: Error message if validation fails or target not found
            - result: Dict with meta, content, structuredContent, isError fields
    """
    global status

    # Extract data from parameters
    data = parameters_used.get('data', {})
    filepath = data.get('filepath')
    patch_content = data.get('content')
    target_type = data.get('target_type')
    target = data.get('target')
    operation = data.get('operation')

    # Error pattern 1: Missing required parameters
    if not filepath:
        status = 0
        return {
            "parameters_used": parameters_used,
            "success": False,
            "error": "Missing required parameter: filepath",
            "result": format_error_response("Input validation error: 'filepath' is a required property")
        }

    if not patch_content:
        status = 0
        return {
            "parameters_used": parameters_used,
            "success": False,
            "error": "Missing required parameter: content",
            "result": format_error_response("Input validation error: 'content' is a required property")
        }

    if not target_type:
        status = 0
        return {
            "parameters_used": parameters_used,
            "success": False,
            "error": "Missing required parameter: target_type",
            "result": format_error_response("Input validation error: 'target_type' is a required property")
        }

    if not operation:
        status = 0
        return {
            "parameters_used": parameters_used,
            "success": False,
            "error": "Missing required parameter: operation",
            "result": format_error_response("Input validation error: 'operation' is a required property")
        }

    # Validate operation type
    valid_operations = ['append', 'prepend', 'replace']
    if operation not in valid_operations:
        status = 0
        return {
            "parameters_used": parameters_used,
            "success": False,
            "error": f"Invalid operation: {operation}",
            "result": format_error_response(f"Input validation error: 'operation' must be one of {valid_operations}")
        }

    # Load all records and find the file
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

    # Get current content
    current_content = record.get('content', '')

    # Apply patch based on target_type and operation
    new_content = current_content
    patch_success = False

    try:
        if target_type == 'heading':
            # Find the heading and apply operation
            patch_success, new_content = _patch_heading(
                current_content, target, patch_content, operation
            )
        elif target_type == 'block':
            # For block type, we'll treat target as a delimiter or pattern
            patch_success, new_content = _patch_block(
                current_content, target, patch_content, operation
            )
        elif target_type == 'frontmatter':
            # Handle frontmatter patching
            patch_success, new_content = _patch_frontmatter(
                current_content, target, patch_content, operation
            )
        else:
            # Invalid target_type
            status = 0
            return {
                "parameters_used": parameters_used,
                "success": False,
                "error": f"Invalid target_type: {target_type}",
                "result": format_error_response(
                    f"Input validation error: 'target_type' must be one of ['heading', 'block', 'frontmatter']"
                )
            }
    except Exception as e:
        status = 0
        return {
            "parameters_used": parameters_used,
            "success": False,
            "error": f"Patch failed: {str(e)}",
            "result": format_error_response(
                "Error 40080: The patch you provided could not be applied. "
                "Check that the target exists and is valid."
            )
        }

    # Error pattern 3: Target not found
    if not patch_success:
        status = 0
        return {
            "parameters_used": parameters_used,
            "success": False,
            "error": "Target not found",
            "result": format_error_response(
                "Error 40080: The patch you provided could not be applied. "
                f"Could not find target '{target}' with type '{target_type}'."
            )
        }

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
    result_text = f"Successfully patched content in {filepath}"
    return {
        "parameters_used": parameters_used,
        "success": True,
        "error": None,
        "result": format_success_response(result_text)
    }


def _patch_heading(content, heading_name, patch_content, operation):
    """
    Patch content at a heading location.

    Args:
        content (str): Current file content
        heading_name (str): Heading to target (can be empty for top of file)
        patch_content (str): Content to insert
        operation (str): Operation type ('append', 'prepend', 'replace')

    Returns:
        tuple: (success, new_content)
    """
    if not heading_name:
        # Empty target means top or bottom of file
        if operation == 'append':
            return True, content + '\n\n' + patch_content
        elif operation == 'prepend':
            return True, patch_content + '\n\n' + content
        else:
            # Replace on empty target means replace entire content
            return True, patch_content

    # Find the heading (supports #, ##, ### levels)
    heading_pattern = rf'^(#{1,6})\s+{re.escape(heading_name)}\s*$'
    match = re.search(heading_pattern, content, re.MULTILINE)

    if not match:
        return False, content

    heading_start = match.start()
    heading_end = match.end()

    # Find the end of this section (next heading of same or higher level, or end of file)
    heading_level = len(match.group(1))
    section_end_pattern = rf'^#{1,{heading_level}}\s+'

    next_heading = re.search(section_end_pattern, content[heading_end:], re.MULTILINE)

    if next_heading:
        section_end = heading_end + next_heading.start()
    else:
        section_end = len(content)

    # Apply operation
    if operation == 'append':
        # Append after the heading
        new_content = (
            content[:heading_end] +
            '\n\n' +
            patch_content +
            content[heading_end:]
        )
    elif operation == 'prepend':
        # Prepend after the heading
        new_content = (
            content[:heading_end] +
            '\n\n' +
            patch_content +
            content[heading_end:]
        )
    else:  # replace
        # Replace the entire section
        new_content = (
            content[:heading_start] +
            f"{match.group(1)} {heading_name}\n\n" +
            patch_content +
            content[section_end:]
        )

    return True, new_content


def _patch_block(content, block_id, patch_content, operation):
    """
    Patch content at a block location.

    Since our data doesn't have explicit block IDs, we treat block_id
    as a text pattern to search for.

    Args:
        content (str): Current file content
        block_id (str): Block identifier or pattern
        patch_content (str): Content to insert
        operation (str): Operation type

    Returns:
        tuple: (success, new_content)
    """
    if not block_id:
        # Empty block_id - append to end of file
        if operation == 'append':
            return True, content + '\n\n' + patch_content
        elif operation == 'prepend':
            return True, patch_content + '\n\n' + content
        else:
            return True, patch_content

    # Search for the block pattern in content
    if block_id not in content:
        return False, content

    # Find position of the block
    pos = content.find(block_id)

    if operation == 'append':
        new_content = content[:pos + len(block_id)] + '\n\n' + patch_content + content[pos + len(block_id):]
    elif operation == 'prepend':
        new_content = content[:pos] + '\n\n' + patch_content + content[pos:]
    else:  # replace
        new_content = content[:pos] + patch_content + content[pos + len(block_id):]

    return True, new_content


def _patch_frontmatter(content, field_name, patch_content, operation):
    """
    Patch content in the frontmatter YAML section.

    Args:
        content (str): Current file content
        field_name (str): Frontmatter field name
        patch_content (str): Content to insert
        operation (str): Operation type

    Returns:
        tuple: (success, new_content)
    """
    # Check if content has frontmatter
    frontmatter_pattern = r'^---\s*\n(.*?)\n---\s*\n'
    match = re.match(frontmatter_pattern, content, re.DOTALL)

    if not match:
        # No frontmatter exists
        if operation == 'append' and not field_name:
            # Create new frontmatter
            new_content = f"---\n{patch_content}\n---\n\n{content}"
            return True, new_content
        return False, content

    frontmatter_start = 0
    frontmatter_end = match.end()
    frontmatter_content = match.group(1)

    if operation == 'append':
        new_frontmatter = frontmatter_content + '\n' + patch_content
    elif operation == 'prepend':
        new_frontmatter = patch_content + '\n' + frontmatter_content
    else:  # replace
        if field_name:
            # Replace specific field
            field_pattern = rf'^{re.escape(field_name)}:\s*.*$'
            if re.search(field_pattern, frontmatter_content, re.MULTILINE):
                new_frontmatter = re.sub(
                    field_pattern,
                    f"{field_name}: {patch_content}",
                    frontmatter_content,
                    flags=re.MULTILINE
                )
            else:
                return False, content
        else:
            new_frontmatter = patch_content

    # Reconstruct content
    new_content = f"---\n{new_frontmatter}\n---\n\n{content[frontmatter_end:]}"

    return True, new_content
