from universal_email.pycode.dynamic_context_handler import get_message_by_uid


def analyze_response_patterns(parameters_used):
    if not isinstance(parameters_used, dict):
        return {
            "success": False,
            "error": "Invalid input: parameters_used must be a dict",
            "result": None,
        }

    uid = parameters_used.get("uid")
    if not uid or not isinstance(uid, str):
        return {
            "success": False,
            "error": "Invalid input: 'uid' is required and must be a string",
            "result": None,
        }

    message = get_message_by_uid(uid)
    if not message:
        return {
            "success": False,
            "error": f"Message with uid '{uid}' not found",
            "result": None,
        }

    return {
        "success": True,
        "result": message,
    }
