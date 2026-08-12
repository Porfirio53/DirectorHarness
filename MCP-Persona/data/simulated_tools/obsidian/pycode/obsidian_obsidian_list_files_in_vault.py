"""
Obsidian List Files in Vault Tool Simulator

This tool lists all files in the root directory of the Obsidian vault.
It reads from all three data sources (clinic, recipes, web3_diary) and returns
a deduplicated list of filenames.
"""
import json
from obsidian.pycode.shared_utils import load_all_records, format_success_response


def analyze_response_patterns(parameters_used):
    """
    Analyze input parameters and return simulated file listing response.

    This tool lists all files in the vault root directory by reading from
    all three data sources (clinic.jsonl, recipes.jsonl, web3_diary.jsonl)
    and returning a deduplicated list of filenames.

    Args:
        parameters_used (dict): Input parameters from the tool call
            - dirpath (str, optional): Directory path (ignored, always lists root)
            - filepath (str, optional): File path (ignored)
            - Any other parameters are ignored

    Returns:
        dict: Response containing:
            - parameters_used: Original input parameters
            - success: True if operation succeeded
            - error: None
            - result: Dict with meta, content, structuredContent, isError fields
    """
    global status

    # Extract data from parameters
    data = parameters_used.get('data', {})

    # Load all records from data files
    all_records = load_all_records()

    # Extract unique filenames (relative_path field)
    filenames_set = set()
    for record in all_records:
        relative_path = record.get('relative_path')
        if relative_path:
            filenames_set.add(relative_path)

    # Convert to sorted list for consistent output
    filenames_list = sorted(list(filenames_set))

    # Format output as JSON array string
    result_text = json.dumps(filenames_list, indent=2,ensure_ascii=False)

    # Return success response
    status = 1
    return {
        "parameters_used": parameters_used,
        "success": True,
        "error": None,
        "result": format_success_response(result_text)
    }
