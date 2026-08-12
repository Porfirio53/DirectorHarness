from universal_email.pycode.dynamic_context_handler import add_account


def analyze_response_patterns(parameters_used):
    if not isinstance(parameters_used, dict):
        return {
            "success": False,
            "error": "Invalid input: parameters_used must be a dict",
            "result": None,
        }

    user = parameters_used.get("user")
    password = parameters_used.get("password")
    smtp_host = parameters_used.get("smtpHost")
    smtp_port = parameters_used.get("smtpPort")
    smtp_secure = parameters_used.get("smtpSecure", True)
    imap_host = parameters_used.get("imapHost")
    imap_port = parameters_used.get("imapPort")
    imap_secure = parameters_used.get("imapSecure", True)

    if not user or not isinstance(user, str):
        return {
            "success": False,
            "error": "Invalid input: 'user' is required and must be a string",
            "result": None,
        }
    if not password or not isinstance(password, str):
        return {
            "success": False,
            "error": "Invalid input: 'password' is required and must be a string",
            "result": None,
        }
    if not smtp_host or not isinstance(smtp_host, str):
        return {
            "success": False,
            "error": "Invalid input: 'smtpHost' is required and must be a string",
            "result": None,
        }
    if not imap_host or not isinstance(imap_host, str):
        return {
            "success": False,
            "error": "Invalid input: 'imapHost' is required and must be a string",
            "result": None,
        }

    account = {
        "email": user,
        "password": password,
        "smtpHost": smtp_host,
        "smtpPort": smtp_port or 587,
        "smtpSecure": bool(smtp_secure),
        "imapHost": imap_host,
        "imapPort": imap_port or 993,
        "imapSecure": bool(imap_secure),
        "status": "manually_configured",
    }

    add_account(account)

    return {
        "success": True,
        "result": {
            "email": user,
            "smtp_host": smtp_host,
            "smtp_port": smtp_port or 587,
            "imap_host": imap_host,
            "imap_port": imap_port or 993,
            "status": "configured",
        },
    }
