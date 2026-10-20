from __future__ import annotations

import logging
import os
import smtplib
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from email.utils import formataddr

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

logger = logging.getLogger(__name__)


def _parse_recipients(raw: str | None) -> list[str]:
    if not raw:
        return []
    return [addr.strip() for addr in raw.split(",") if addr.strip()]

def send_time_log_report_email(subject: str, html_body: str) -> None:
    """Send time log report via Microsoft Graph API."""
    from notifications.graph_email_sender import send_graph_email

    recipients_raw = os.environ.get("EMAIL_RECIPIENTS", "")
    recipients = [r.strip() for r in recipients_raw.split(",") if r.strip()]

    if not recipients:
        raise ValueError("EMAIL_RECIPIENTS is not set or empty")

    logger.info("Sending time log report: subject=%r recipients=%s", subject, recipients)
    send_graph_email(subject=subject, html_body=html_body, recipients=recipients)
