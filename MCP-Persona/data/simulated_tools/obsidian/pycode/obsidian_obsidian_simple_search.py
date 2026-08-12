"""
Obsidian Simple Search Tool Simulator

This tool performs simple text search across all files in the vault.
It searches for the query string in file contents and returns matches with context.
"""
import json
from obsidian.pycode.shared_utils import (
    load_all_records,
    extract_context,
    calculate_score,
    format_error_response,
    format_success_response
)


def analyze_response_patterns(parameters_used):
    """
    Analyze input parameters and return simulated search response.

    This tool performs text search by looking for the query string in the
    content field of all records. It returns matches with context snippets
    and relevance scores.

    Args:
        parameters_used (dict): Input parameters from the tool call
            - query (str, required): Search query string
            - context_length (int, optional): Number of characters for context
              around each match (default: 50)
            - filepath (str, optional): Unused in this function
            - operation (str, optional): Unused in this function
            - target_type (str, optional): Unused in this function
            - target (str, optional): Unused in this function
            - content (str, optional): Unused in this function

    Returns:
        dict: Response containing:
            - parameters_used: Original input parameters
            - success: True if search executed (even with no results)
            - error: Error message if query parameter is missing
            - result: Dict with meta, content, structuredContent, isError fields
    """
    global status

    # Extract data from parameters
    data = parameters_used.get('data', {})
    query = data.get('query')
    context_length = data.get('context_length', 50)

    # Error pattern: Missing required query parameter
    if not query:
        status = 0
        return {
            "parameters_used": parameters_used,
            "success": False,
            "error": "Missing required parameter: query",
            "result": format_error_response("Input validation error: 'query' is a required property")
        }

    # Load all records
    all_records = load_all_records()

    # Perform search
    results = []

    for record in all_records:
        filename = record.get('relative_path', '')
        content = record.get('content', '')

        # Find all occurrences of query in content
        matches = []
        search_start = 0

        while True:
            # Find next occurrence (case-sensitive search)
            match_pos = content.find(query, search_start)

            if match_pos == -1:
                break  # No more matches

            # Extract context around the match
            context = extract_context(content, query, match_pos, context_length)

            # Add match info
            matches.append({
                "context": context,
                "match_position": {
                    "start": match_pos,
                    "end": match_pos + len(query)
                }
            })

            # Move past this match to find next occurrence
            search_start = match_pos + len(query)

        # If we found matches in this file, add to results
        if matches:
            # Calculate score based on first match position
            first_match_pos = matches[0]['match_position']['start']
            score = calculate_score(first_match_pos, len(content))

            results.append({
                "filename": filename,
                "score": score,
                "matches": matches
            })

    # Sort results by score (lower/better scores first)
    results.sort(key=lambda x: x['score'])

    # Format output as JSON array string
    result_text = json.dumps(results, indent=2,ensure_ascii=False)

    # Return success response (even if no results found)
    status = 1
    return {
        "parameters_used": parameters_used,
        "success": True,
        "error": None,
        "result": format_success_response(result_text)
    }
