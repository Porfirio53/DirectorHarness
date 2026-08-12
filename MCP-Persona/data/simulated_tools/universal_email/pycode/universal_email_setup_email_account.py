from universal_email.pycode.dynamic_context_handler import add_account

SUPPORTED_PROVIDERS = {
    "qq": {
        "smtp_host": "smtp.qq.com",
        "smtp_port": 587,
        "imap_host": "imap.qq.com",
        "imap_port": 993,
    },
    "163": {
        "smtp_host": "smtp.163.com",
        "smtp_port": 465,
        "imap_host": "imap.163.com",
        "imap_port": 993,
    },
    "gmail": {
        "smtp_host": "smtp.gmail.com",
        "smtp_port": 587,
        "imap_host": "imap.gmail.com",
        "imap_port": 993,
    },
    "outlook": {
        "smtp_host": "smtp.office365.com",
        "smtp_port": 587,
        "imap_host": "imap-mail.outlook.com",
        "imap_port": 993,
    },
    "exmail": {
        "smtp_host": "smtp.exmail.qq.com",
        "smtp_port": 465,
        "imap_host": "imap.exmail.qq.com",
        "imap_port": 993,
    },
    "aliyun": {
        "smtp_host": "smtp.aliyun.com",
        "smtp_port": 465,
        "imap_host": "imap.aliyun.com",
        "imap_port": 993,
    },
    "sina": {
        "smtp_host": "smtp.sina.com",
        "smtp_port": 587,
        "imap_host": "imap.sina.com",
        "imap_port": 993,
    },
    "sohu": {
        "smtp_host": "smtp.sohu.com",
        "smtp_port": 465,
        "imap_host": "imap.sohu.com",
        "imap_port": 993,
    },
}


def analyze_response_patterns(parameters_used):
    if not isinstance(parameters_used, dict):
        return {
            "success": False,
            "error": "Invalid input: parameters_used must be a dict",
            "result": None,
        }

    email = parameters_used.get("email")
    password = parameters_used.get("password")
    provider = parameters_used.get("provider")

    if not email or not isinstance(email, str) or "@" not in email:
        return {
            "success": False,
            "error": "Invalid input: 'email' is required and must be a valid email address",
            "result": None,
        }
    if not password or not isinstance(password, str):
        return {
            "success": False,
            "error": "Invalid input: 'password' is required and must be a string",
            "result": None,
        }

    # Auto-detect provider if not specified
    if not provider:
        domain = email.split("@")[-1].lower()
        if "qq.com" in domain:
            provider = "qq"
        elif "163.com" in domain:
            provider = "163"
        elif "gmail.com" in domain:
            provider = "gmail"
        elif "outlook.com" in domain or "office365.com" in domain:
            provider = "outlook"
        elif "exmail.qq.com" in domain:
            provider = "exmail"
        elif "aliyun.com" in domain:
            provider = "aliyun"
        elif "sina.com" in domain:
            provider = "sina"
        elif "sohu.com" in domain:
            provider = "sohu"
        else:
            return {
                "success": False,
                "error": f"Cannot auto-detect provider for {email}, please specify 'provider'",
                "result": None,
            }

    if provider not in SUPPORTED_PROVIDERS:
        return {
            "success": False,
            "error": f"Unsupported provider: {provider}",
            "result": None,
        }

    config = SUPPORTED_PROVIDERS[provider]
    account = {
        "email": email,
        "password": password,
        "provider": provider,
        "smtpHost": config["smtp_host"],
        "smtpPort": config["smtp_port"],
        "smtpSecure": True,
        "imapHost": config["imap_host"],
        "imapPort": config["imap_port"],
        "imapSecure": True,
        "status": "configured",
    }

    add_account(account)

    return {
        "success": True,
        "result": {
            "email": email,
            "provider": provider,
            "status": "configured",
            "smtp_host": config["smtp_host"],
            "imap_host": config["imap_host"],
        },
    }
