"""
Obsidian Get Periodic Note Tool Simulator

This tool retrieves the current periodic note based on period type (daily, weekly, etc.).
It searches through clinic.jsonl and web3_diary.jsonl for matching periodic notes.
"""
import json
from datetime import datetime, timedelta
from obsidian.pycode.shared_utils import (
    load_all_records,
    validate_period,
    format_error_response,
    format_success_response
)


def analyze_response_patterns(parameters_used):
    """
    Analyze input parameters and return simulated periodic note retrieval response.

    This tool retrieves periodic notes by filtering records with is_perodic_note=true
    and matching the period type and date.

    Args:
        parameters_used (dict): Input parameters from the tool call
            - period (str, required): Period type ('daily', 'weekly', 'monthly',
              'quarterly', 'yearly')

    Returns:
        dict: Response containing:
            - parameters_used: Original input parameters
            - success: True if periodic note found, False otherwise
            - error: Error message if validation fails or note not found
            - result: Dict with meta, content, structuredContent, isError fields
    """
    global status

    # Extract data from parameters
    data = parameters_used.get('data', {})
    period = data.get('period', 'daily')

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
        status = 0
        return {
            "parameters_used": parameters_used,
            "success": False,
            "error": "No periodic notes found",
            "result": format_error_response(
                "Error 40461: Periodic note does not exist for the specified period"
            )
        }

    # Get current date for matching
    today = datetime.now()
    target_date = None

    # Calculate target date based on period type
    if period == 'daily':
        # For daily, use today's date in YYYY-MM-DD format
        target_date = today.strftime('%Y-%m-%d')
    elif period == 'weekly':
        # For weekly, find the start of the week (Monday)
        target_date = (today - timedelta(days=today.weekday())).strftime('%Y-%m-%d')
    elif period == 'monthly':
        # For monthly, use first day of current month
        target_date = today.replace(day=1).strftime('%Y-%m-%d')
    elif period == 'quarterly':
        # For quarterly, use first day of current quarter
        quarter = (today.month - 1) // 3
        target_date = today.replace(month=(quarter * 3 + 1), day=1).strftime('%Y-%m-%d')
    elif period == 'yearly':
        # For yearly, use January 1st
        target_date = today.replace(month=1, day=1).strftime('%Y-%m-%d')

    # Try to find exact match for target date
    matched_note = None
    for note in periodic_notes:
        metadata = note.get('metadata', {})
        periodic_info = metadata.get('periodic', {})

        if periodic_info.get('period_key') == target_date:
            matched_note = note
            break

    # If no exact match, try to find the most recent periodic note of this type
    if not matched_note:
        # Filter notes by period type
        same_period_notes = [
            n for n in periodic_notes
            if n.get('metadata', {}).get('periodic', {}).get('type') == period
        ]

        if same_period_notes:
            # Sort by period_key (most recent first)
            same_period_notes.sort(
                key=lambda x: x.get('metadata', {}).get('periodic', {}).get('period_key', ''),
                reverse=True
            )
            matched_note = same_period_notes[0]
        else:
            # Fallback: just get the most recent periodic note
            periodic_notes.sort(
                key=lambda x: x.get('metadata', {}).get('periodic', {}).get('period_key', ''),
                reverse=True
            )
            if periodic_notes:
                matched_note = periodic_notes[0]

    # If still no match, return error
    if not matched_note:
        status = 0
        return {
            "parameters_used": parameters_used,
            "success": False,
            "error": "No periodic note found",
            "result": format_error_response(
                f"Error 40461: Periodic note does not exist for period '{period}' "
                f"(target date: {target_date})"
            )
        }

    # Get the content
    content = matched_note.get('content', '')

    # Format content as JSON string
    result_text = json.dumps(content,ensure_ascii=False)

    # Success response
    status = 1
    return {
        "parameters_used": parameters_used,
        "success": True,
        "error": None,
        "result": format_success_response(result_text)
    }
