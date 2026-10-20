"""
Email sender for budget tracking reports.
Reuses existing SMTP infrastructure from email_sender.py.
"""
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


#def send_budget_email(subject: str, html_body: str, recipients: list[str]) -> None:
#    """
#    Send budget report email using existing SMTP infrastructure.
#    
#    Args:
#        subject: Email subject line
#        html_body: HTML email body
#        recipients: List of recipient email addresses
#        
#    Raises:
#        ValueError: If SMTP configuration is invalid
#        SMTPException: If email sending fails
#    """
#    host = os.environ.get("SMTP_HOST", "").strip()
#    port_str = os.environ.get("SMTP_PORT", "587").strip()
#    username = os.environ.get("SMTP_USERNAME", "").strip()
#    password = os.environ.get("SMTP_PASSWORD", "")
#    from_name = os.environ.get("SMTP_FROM_NAME", "Gould APM Bot").strip()
#    
#    if not host:
#        raise ValueError("SMTP_HOST is not set")
#    if not username:
#        raise ValueError("SMTP_USERNAME is not set")
#    if not recipients:
#        raise ValueError("No recipients provided")
#    
#    try:
#        port = int(port_str)
#    except ValueError as e:
#        raise ValueError(f"SMTP_PORT must be an integer, got {port_str!r}") from e
#    
#    logger.info(
#        "Sending budget email: subject=%r recipients=%s smtp=%s:%s",
#        subject,
#        recipients,
#        host,
#        port,
#    )
#    
#    msg = MIMEMultipart("alternative")
#    msg["Subject"] = subject
#    msg["From"] = formataddr((from_name, username))
#    msg["To"] = ", ".join(recipients)
#    msg.attach(MIMEText(html_body, "html", "utf-8"))
#    
#    try:
#        with smtplib.SMTP(host, port, timeout=60) as server:
#            server.starttls()
#            if password:
#                server.login(username, password)
#            server.sendmail(username, recipients, msg.as_string())
#        logger.info(
#            "Budget email sent successfully: subject=%r to=%s",
#            subject,
#            recipients,
#        )
#    except smtplib.SMTPAuthenticationError as exc:
#        logger.error("SMTP authentication failed for user=%s: %s", username, exc)
#        raise
#    except smtplib.SMTPException as exc:
#        logger.error("SMTP error while sending budget email: %s", exc)
#        raise
#    except OSError as exc:
#        logger.error("Network error connecting to SMTP %s:%s — %s", host, port, exc)
#        raise

def send_budget_email(subject: str, html_body: str, recipients: list[str]) -> None:
    """Send budget report via Microsoft Graph API."""
    from notifications.graph_email_sender import send_graph_email

    if not recipients:
        raise ValueError("No recipients provided")

    logger.info("Sending budget email: subject=%r recipients=%s", subject, recipients)
    send_graph_email(subject=subject, html_body=html_body, recipients=recipients)
