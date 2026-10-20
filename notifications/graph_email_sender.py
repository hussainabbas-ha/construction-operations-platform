"""
notifications/graph_email_sender.py
─────────────────────────────────────
Sends HTML emails via Microsoft Graph API using OAuth2 client credentials.
Replaces the SMTP sender for the Office 365 account.

Reads from .env:
    GRAPH_TENANT_ID       — Azure AD tenant ID
    GRAPH_CLIENT_ID       — App registration client ID
    GRAPH_CLIENT_SECRET   — App registration client secret
    GRAPH_SENDER_EMAIL    — The mailbox to send from (operations@neuralogicgroup.com)
    SMTP_FROM_NAME        — Display name (Gould)

No SMTP port, host, or password needed. No Gmail app password needed.
Token is fetched fresh on each call (cached by msal internally).
"""
from __future__ import annotations

import logging
import os

import msal
import requests

logger = logging.getLogger(__name__)


def _get_access_token() -> str:
    """
    Fetch an OAuth2 access token from Azure AD using client credentials flow.
    msal caches the token internally and reuses it until 5 minutes before expiry.
    """
    tenant_id     = os.environ.get("GRAPH_TENANT_ID", "").strip()
    client_id     = os.environ.get("GRAPH_CLIENT_ID", "").strip()
    client_secret = os.environ.get("GRAPH_CLIENT_SECRET", "").strip()

    if not all([tenant_id, client_id, client_secret]):
        raise ValueError(
            "GRAPH_TENANT_ID, GRAPH_CLIENT_ID, and GRAPH_CLIENT_SECRET "
            "must all be set in .env"
        )

    authority = f"https://login.microsoftonline.com/{tenant_id}"
    scope     = ["https://graph.microsoft.com/.default"]

    app = msal.ConfidentialClientApplication(
        client_id=client_id,
        client_credential=client_secret,
        authority=authority,
    )

    result = app.acquire_token_for_client(scopes=scope)

    if "access_token" not in result:
        error   = result.get("error", "unknown")
        desc    = result.get("error_description", "no description")
        raise RuntimeError(
            f"Failed to acquire Microsoft Graph token: {error} — {desc}"
        )

    logger.debug("Graph API token acquired successfully")
    return result["access_token"]


def send_graph_email(
    subject: str,
    html_body: str,
    recipients: list[str],
) -> None:
    """
    Send an HTML email via Microsoft Graph API.

    Args:
        subject:    Email subject line
        html_body:  HTML email body
        recipients: List of recipient email addresses

    Raises:
        ValueError:   If required env vars are missing
        RuntimeError: If token acquisition or API call fails
    """
    sender_email = os.environ.get("GRAPH_SENDER_EMAIL", "").strip()
    from_name    = os.environ.get("SMTP_FROM_NAME", "Gould").strip()

    if not sender_email:
        raise ValueError("GRAPH_SENDER_EMAIL is not set in .env")
    if not recipients:
        raise ValueError("No recipients provided")

    token = _get_access_token()

    # Build the Graph API message payload
    to_recipients = [
        {"emailAddress": {"address": addr}} for addr in recipients
    ]

    message = {
        "message": {
            "subject": subject,
            "body": {
                "contentType": "HTML",
                "content": html_body,
            },
            "from": {
                "emailAddress": {
                    "name":    from_name,
                    "address": sender_email,
                }
            },
            "toRecipients": to_recipients,
        },
        "saveToSentItems": "true",
    }

    url = f"https://graph.microsoft.com/v1.0/users/{sender_email}/sendMail"
    headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type":  "application/json",
    }

    logger.info(
        "Sending email via Graph API: subject=%r from=%s to=%s",
        subject, sender_email, recipients,
    )

    response = requests.post(url, headers=headers, json=message, timeout=30)

    if response.status_code == 202:
        logger.info(
            "Email sent successfully via Graph API: subject=%r to=%s",
            subject, recipients,
        )
    else:
        error_detail = ""
        try:
            error_detail = response.json().get("error", {}).get("message", "")
        except Exception:
            error_detail = response.text[:300]
        raise RuntimeError(
            f"Graph API send failed (HTTP {response.status_code}): {error_detail}"
        )