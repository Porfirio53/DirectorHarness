import time
import uuid

from universal_email.pycode.dynamic_context_handler import add_sent_message


def analyze_response_patterns(parameters_used):
    if not isinstance(parameters_used, dict):
        return {
            "success": False,
            "error": "Invalid input: parameters_used must be a dict",
            "result": None,
        }

    to = parameters_used.get("to")
    subject = parameters_used.get("subject")
    text = parameters_used.get("text")
    cc = parameters_used.get("cc")
    bcc = parameters_used.get("bcc")
    html = parameters_used.get("html")
    attachments = parameters_used.get("attachments")

    if not to or not isinstance(to, list) or len(to) == 0:
        return {
            "success": False,
            "error": "Invalid input: 'to' is required and must be a non-empty list",
            "result": None,
        }
    if not subject or not isinstance(subject, str):
        return {
            "success": False,
            "error": "Invalid input: 'subject' is required and must be a string",
            "result": None,
        }
    if not text or not isinstance(text, str):
        return {
            "success": False,
            "error": "Invalid input: 'text' is required and must be a string",
            "result": None,
        }

    for addr in to:
        if not isinstance(addr, str) or "@" not in addr:
            return {
                "success": False,
                "error": f"Invalid email address in 'to': {addr}",
                "result": None,
            }

    message_id = f"{uuid.uuid4()}@simulated.local"
    message = {
        "message_id": message_id,
        "to": to,
        "subject": subject,
        "text": text,
        "timestamp": int(time.time()),
        "status": "sent",
    }
    if cc:
        message["cc"] = cc
    if bcc:
        message["bcc"] = bcc
    if html:
        message["html"] = html
    if attachments:
        message["attachments"] = attachments

    add_sent_message(message)

    return {
        "success": True,
        "result": {
            "message_id": message_id,
            "status": "sent",
            "to": to,
            "subject": subject,
        },
    }
