"""
Obsidian List Files in Directory Tool Simulator

This tool lists all files in a specific directory within the Obsidian vault.
It filters files based on the dirpath parameter and returns matching filenames.
"""
import json
import os
from obsidian.pycode.shared_utils import load_all_records, format_success_response


def analyze_response_patterns(parameters_used):
    """
    Analyze input parameters and return simulated directory file listing response.

    This tool lists files in a specific directory by filtering records based on
    their vault_path and relative_path fields.

    Args:
        parameters_used (dict): Input parameters from the tool call
            - dirpath (str): Directory path to list files from
              - Empty string "" lists all files (root level)
              - Specific path like "recipes" filters by that directory
            - Other parameters are ignored

    Returns:
        dict: Response containing:
            - parameters_used: Original input parameters
            - success: True if operation succeeded
            - error: None (empty directories return empty arrays, not errors)
            - result: Dict with meta, content, structuredContent, isError fields
    """
    global status

    # Extract data from parameters
    data = parameters_used.get('data', {})
    dirpath = data.get('dirpath', '')

    # Load all records from data files
    all_records = load_all_records()

    # Filter files based on dirpath
    filtered_files = []

    if not dirpath or dirpath == '/':
        # Empty dirpath - return all root level files (no subdirectories)
        for record in all_records:
            relative_path = record.get('relative_path', '')
            # Only include files at root level (no directory separator)
            if '/' not in relative_path:
                filtered_files.append(relative_path)
    else:
        # Specific directory - filter by vault_path and relative_path
        for record in all_records:
            vault_path = record.get('vault_path', '')
            relative_path = record.get('relative_path', '')

            # Check if the directory path matches either vault_path or is in relative_path
            # Normalize paths for comparison
            normalized_dirpath = dirpath.strip('/')

            # Check if vault_path contains the directory name
            if normalized_dirpath in vault_path or normalized_dirpath in relative_path:
                filtered_files.append(relative_path)

    # Remove duplicates and sort
    filtered_files = sorted(list(set(filtered_files)))

    # Format output as JSON array string
    result_text = json.dumps(filtered_files, indent=2,ensure_ascii=False)

    # Return success response (even if empty - empty directory is valid)
    status = 1
    return {
        "parameters_used": parameters_used,
        "success": True,
        "error": None,
        "result": format_success_response(result_text)
    }
