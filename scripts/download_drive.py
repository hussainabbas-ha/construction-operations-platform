"""
Download all files from the the construction operations platform Google Drive folder,
skipping the 'Estimating' folder.

Usage:
    python download_drive.py

Files are saved to: downloads/
Google Docs  → exported as .pdf
Google Sheets → exported as .xlsx
Google Slides → exported as .pdf
All other files → downloaded as-is
"""
from __future__ import annotations

import io
import logging
import os
from pathlib import Path

from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError
from googleapiclient.http import MediaIoBaseDownload

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)-8s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger(__name__)

# ── Config ────────────────────────────────────────────────────────────────────

TARGET_FOLDER_ID  = "0AP9QW31FGsN1Uk9PVA"   # root shared drive folder
SKIP_FOLDER_NAME  = None                      # set to None to download everything
DOWNLOAD_DIR      = Path(r"D:\AI-Project-Manager-v2")
CREDENTIALS_FILE  = "credentials.json"
TOKEN_FILE        = "token.json"
SCOPES            = ["https://www.googleapis.com/auth/drive.readonly"]

# Google Workspace MIME types → export format
EXPORT_MAP = {
    "application/vnd.google-apps.document":     ("application/pdf",                                    ".pdf"),
    "application/vnd.google-apps.spreadsheet":  ("application/vnd.openxmlformats-officedocument.spreadsheetml.sheet", ".xlsx"),
    "application/vnd.google-apps.presentation": ("application/pdf",                                    ".pdf"),
    "application/vnd.google-apps.drawing":      ("image/png",                                          ".png"),
}

# ── Auth ──────────────────────────────────────────────────────────────────────

def _get_service():
    creds = None
    if os.path.exists(TOKEN_FILE):
        try:
            creds = Credentials.from_authorized_user_file(TOKEN_FILE, SCOPES)
        except Exception as e:
            logger.warning("Could not load token.json: %s", e)

    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            logger.info("Refreshing expired token...")
            creds.refresh(Request())
        else:
            logger.info("Starting OAuth flow — browser will open...")
            flow = InstalledAppFlow.from_client_secrets_file(CREDENTIALS_FILE, SCOPES)
            creds = flow.run_local_server(port=0)
        with open(TOKEN_FILE, "w") as f:
            f.write(creds.to_json())
        logger.info("Token saved to %s", TOKEN_FILE)

    return build("drive", "v3", credentials=creds)


# ── Drive helpers ─────────────────────────────────────────────────────────────

def list_items(service, folder_id: str) -> list[dict]:
    """List all files and folders inside a given folder (handles pagination)."""
    items = []
    page_token = None
    while True:
        resp = service.files().list(
            q=f"'{folder_id}' in parents and trashed = false",
            supportsAllDrives=True,
            includeItemsFromAllDrives=True,
            pageSize=200,
            fields="nextPageToken, files(id, name, mimeType)",
            pageToken=page_token,
        ).execute()
        items.extend(resp.get("files", []))
        page_token = resp.get("nextPageToken")
        if not page_token:
            break
    return items


def download_file(service, file_id: str, mime_type: str, dest_path: Path) -> bool:
    """Download a single file. Returns True on success."""
    try:
        dest_path.parent.mkdir(parents=True, exist_ok=True)

        # Google Workspace file — export to a real format
        if mime_type in EXPORT_MAP:
            export_mime, ext = EXPORT_MAP[mime_type]
            # Add extension if not already there
            if not dest_path.suffix:
                dest_path = dest_path.with_suffix(ext)
            request = service.files().export_media(
                fileId=file_id,
                mimeType=export_mime,
            )
        else:
            # Regular binary file
            request = service.files().get_media(
                fileId=file_id,
                supportsAllDrives=True,
            )

        buf = io.BytesIO()
        downloader = MediaIoBaseDownload(buf, request)
        done = False
        while not done:
            _, done = downloader.next_chunk()

        dest_path.write_bytes(buf.getvalue())
        logger.info("  ✓ %s", dest_path)
        return True

    except HttpError as e:
        logger.error("  ✗ Failed to download %s: %s", dest_path.name, e)
        return False


# ── Recursive download ────────────────────────────────────────────────────────

def download_folder(service, folder_id: str, local_path: Path, depth: int = 0) -> tuple[int, int]:
    """
    Recursively download all contents of a Drive folder.
    Returns (success_count, fail_count).
    """
    indent = "  " * depth
    items = list_items(service, folder_id)
    logger.info("%s📁 %s (%d items)", indent, local_path.name, len(items))

    success = fail = 0

    for item in items:
        name      = item["name"]
        item_id   = item["id"]
        mime_type = item["mimeType"]

        if mime_type == "application/vnd.google-apps.folder":
            # Recurse into subfolder
            sub_path = local_path / _safe_name(name)
            s, f = download_folder(service, item_id, sub_path, depth + 1)
            success += s
            fail    += f
        else:
            # Skip Google Forms / Maps / other unsupported types
            if mime_type.startswith("application/vnd.google-apps.") and mime_type not in EXPORT_MAP:
                logger.info("%s  ⚠ Skipping unsupported type: %s (%s)", indent, name, mime_type)
                continue

            dest = local_path / _safe_name(name)
            # Add export extension for Google Workspace files
            if mime_type in EXPORT_MAP:
                _, ext = EXPORT_MAP[mime_type]
                if not dest.suffix:
                    dest = dest.with_suffix(ext)

            if dest.exists():
                logger.info("%s  — Already exists, skipping: %s", indent, name)
                success += 1
                continue

            ok = download_file(service, item_id, mime_type, dest)
            if ok:
                success += 1
            else:
                fail += 1

    return success, fail


def _safe_name(name: str) -> str:
    """Replace characters that are invalid in file/folder names on Windows."""
    for ch in r'\/:*?"<>|':
        name = name.replace(ch, "_")
    return name.strip()


# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    logger.info("=" * 60)
    logger.info("GOULD CONSTRUCTION — GOOGLE DRIVE DOWNLOAD")
    logger.info("=" * 60)

    service = _get_service()

    # List top-level folders in the root target folder
    logger.info("Listing root folder contents...")
    root_items = list_items(service, TARGET_FOLDER_ID)

    folders = [i for i in root_items if i["mimeType"] == "application/vnd.google-apps.folder"]
    files   = [i for i in root_items if i["mimeType"] != "application/vnd.google-apps.folder"]

    logger.info("Found %d folder(s) and %d file(s) in root:", len(folders), len(files))
    for f in folders:
        skip = SKIP_FOLDER_NAME and f["name"].lower() == SKIP_FOLDER_NAME.lower()
        logger.info("  %s %s", "⏭ SKIP" if skip else "✓ DOWNLOAD", f["name"])

    DOWNLOAD_DIR.mkdir(parents=True, exist_ok=True)

    total_success = total_fail = 0

    # Download any loose files in root (not in a subfolder)
    for item in files:
        dest = DOWNLOAD_DIR / _safe_name(item["name"])
        if not dest.exists():
            ok = download_file(service, item["id"], item["mimeType"], dest)
            total_success += ok
            total_fail    += not ok

    # Download folders (skip none — download everything)
    for folder in folders:
        if SKIP_FOLDER_NAME and folder["name"].lower() == SKIP_FOLDER_NAME.lower():
            logger.info("Skipping folder: %s", folder["name"])
            continue

        folder_path = DOWNLOAD_DIR / _safe_name(folder["name"])
        s, f = download_folder(service, folder["id"], folder_path)
        total_success += s
        total_fail    += f

    logger.info("=" * 60)
    logger.info("DOWNLOAD COMPLETE")
    logger.info("  ✓ Success : %d files", total_success)
    logger.info("  ✗ Failed  : %d files", total_fail)
    logger.info("  📁 Saved to: %s", DOWNLOAD_DIR.resolve())
    logger.info("=" * 60)


if __name__ == "__main__":
    main()
