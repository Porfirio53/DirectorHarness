from universal_email.pycode.dynamic_context_handler import get_recent_messages


def analyze_response_patterns(parameters_used):
    if not isinstance(parameters_used, dict):
        parameters_used = {}

    limit = parameters_used.get("limit", 20)
    days = parameters_used.get("days", 3)

    if not isinstance(limit, (int, float)) or limit < 1:
        return {
            "success": False,
            "error": "Invalid input: 'limit' must be a positive number",
            "result": None,
        }
    if not isinstance(days, (int, float)) or days < 1:
        return {
            "success": False,
            "error": "Invalid input: 'days' must be a positive number",
            "result": None,
        }

    messages = get_recent_messages(days=int(days), limit=int(limit))

    return {
        "success": True,
        "result": {
            "count": len(messages),
            "days": days,
            "messages": messages,
        },
    }
