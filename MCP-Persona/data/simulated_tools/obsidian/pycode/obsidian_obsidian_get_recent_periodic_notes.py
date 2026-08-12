"""
Obsidian Get Recent Periodic Notes Tool Simulator

This tool retrieves a list of recent periodic notes.
It filters records with is_perodic_note=true and returns them sorted by period_key.
"""
import json
from obsidian.pycode.shared_utils import (
    load_all_records,
    validate_period,
    format_error_response,
    format_success_response
)


def analyze_response_patterns(parameters_used):
    """
    Analyze input parameters and return simulated recent periodic notes response.

    This tool retrieves recent periodic notes by filtering records with
    is_perodic_note=true and sorting them by period_key in descending order.

    Args:
        parameters_used (dict): Input parameters from the tool call
            - period (str, required): Period type ('daily', 'weekly', 'monthly',
              'quarterly', 'yearly')
            - limit (int, optional): Maximum number of notes to return (default: 10)
            - include_content (bool, optional): Whether to include note content
              (default: True)

    Returns:
        dict: Response containing:
            - parameters_used: Original input parameters
            - success: True if operation succeeded (even with no results)
            - error: Error message if validation fails
            - result: Dict with meta, content, structuredContent, isError fields
    """
    global status

    # Extract data from parameters
    data = parameters_used.get('data', {})
    period = data.get('period', 'daily')
    limit = data.get('limit', 10)
    include_content = data.get('include_content', True)

    # Validate period parameter
    is_valid, error_msg = validate_period(period)
    if not is_valid:
        status = 0
        return {
            "parameters_used": parameters_used,
            "success": False,
            "error": error_msg,
            "result": format_error_response(f"Input validation error: {error_msg}")
        }

    # Load all records
    all_records = load_all_records()

    # Filter periodic notes
    periodic_notes = [
        r for r in all_records
        if r.get('is_perodic_note') == True
    ]

    if not periodic_notes:
        # No periodic notes found - return empty array (not an error)
        status = 1
        result_text = json.dumps([], indent=2)
        return {
            "parameters_used": parameters_used,
            "success": True,
            "error": None,
            "result": format_success_response(result_text)
        }

    # Filter by period type if specified
    if period:
        periodic_notes = [
            n for n in periodic_notes
            if n.get('metadata', {}).get('periodic', {}).get('type') == period
        ]

    # Sort by period_key in descending order (most recent first)
    periodic_notes.sort(
        key=lambda x: x.get('metadata', {}).get('periodic', {}).get('period_key', ''),
        reverse=True
    )

    # Apply limit
    if limit > 0:
        periodic_notes = periodic_notes[:limit]

    # Build result array
    results = []
    for note in periodic_notes:
        note_data = {
            'filepath': note.get('relative_path', ''),
            'period_key': note.get('metadata', {}).get('periodic', {}).get('period_key', '')
        }

        # Include content if requested
        if include_content:
            note_data['content'] = note.get('content', '')

        results.append(note_data)

    # Format output as JSON array string
    result_text = json.dumps(results, indent=2,ensure_ascii=False)

    # Success response
    status = 1
    return {
        "parameters_used": parameters_used,
        "success": True,
        "error": None,
        "result": format_success_response(result_text)
    }
