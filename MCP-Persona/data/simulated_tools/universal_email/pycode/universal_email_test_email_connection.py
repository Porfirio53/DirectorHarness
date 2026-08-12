from universal_email.pycode.dynamic_context_handler import load_context


def analyze_response_patterns(parameters_used):
    if not isinstance(parameters_used, dict):
        parameters_used = {}

    test_type = parameters_used.get("testType", "both")
    if test_type not in ("smtp", "imap", "both"):
        return {
            "success": False,
            "error": "Invalid input: 'testType' must be 'smtp', 'imap', or 'both'",
            "result": None,
        }

    context = load_context()
    accounts = context.get("accounts", [])

    if not accounts:
        return {
            "success": False,
            "error": "No email account configured. Please call setup_email_account first.",
            "result": None,
        }

    # Test the first configured account
    account = accounts[0]
    results = {}

    if test_type in ("smtp", "both"):
        results["smtp"] = {
            "status": "connected",
            "host": account.get("smtpHost"),
            "port": account.get("smtpPort"),
        }

    if test_type in ("imap", "both"):
        results["imap"] = {
            "status": "connected",
            "host": account.get("imapHost"),
            "port": account.get("imapPort"),
        }

    return {
        "success": True,
        "result": {
            "email": account.get("email"),
            "test_type": test_type,
            "connections": results,
            "all_passed": True,
        },
    }
