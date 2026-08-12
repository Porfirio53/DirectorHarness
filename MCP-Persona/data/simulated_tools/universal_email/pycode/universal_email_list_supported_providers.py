SUPPORTED_PROVIDERS = [
    {
        "name": "qq",
        "smtp_host": "smtp.qq.com",
        "smtp_port": 587,
        "imap_host": "imap.qq.com",
        "imap_port": 993,
    },
    {
        "name": "163",
        "smtp_host": "smtp.163.com",
        "smtp_port": 465,
        "imap_host": "imap.163.com",
        "imap_port": 993,
    },
    {
        "name": "gmail",
        "smtp_host": "smtp.gmail.com",
        "smtp_port": 587,
        "imap_host": "imap.gmail.com",
        "imap_port": 993,
    },
    {
        "name": "outlook",
        "smtp_host": "smtp.office365.com",
        "smtp_port": 587,
        "imap_host": "imap-mail.outlook.com",
        "imap_port": 993,
    },
    {
        "name": "exmail",
        "smtp_host": "smtp.exmail.qq.com",
        "smtp_port": 465,
        "imap_host": "imap.exmail.qq.com",
        "imap_port": 993,
    },
    {
        "name": "aliyun",
        "smtp_host": "smtp.aliyun.com",
        "smtp_port": 465,
        "imap_host": "imap.aliyun.com",
        "imap_port": 993,
    },
    {
        "name": "sina",
        "smtp_host": "smtp.sina.com",
        "smtp_port": 587,
        "imap_host": "imap.sina.com",
        "imap_port": 993,
    },
    {
        "name": "sohu",
        "smtp_host": "smtp.sohu.com",
        "smtp_port": 465,
        "imap_host": "imap.sohu.com",
        "imap_port": 993,
    },
]


def analyze_response_patterns(parameters_used=None):
    return {
        "success": True,
        "result": {
            "providers": SUPPORTED_PROVIDERS,
            "count": len(SUPPORTED_PROVIDERS),
        },
    }
