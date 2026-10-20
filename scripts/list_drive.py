"""List the construction operations platform Google Drive contents (read-only browse)."""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from api.google_drive_client import GoogleDriveClient, TARGET_FOLDER_ID

FOLDER_MIME = "application/vnd.google-apps.folder"


def main() -> None:
    client = GoogleDriveClient()

    print("=" * 60)
    print(f"Connected! Listing root folder: {TARGET_FOLDER_ID}")
    print("=" * 60)

    items = client.list_folder(TARGET_FOLDER_ID)
    folders = [i for i in items if i["mimeType"] == FOLDER_MIME]
    files = [i for i in items if i["mimeType"] != FOLDER_MIME]

    print(f"\nFound {len(folders)} folder(s) and {len(files)} file(s) at root:\n")

    for f in sorted(folders, key=lambda x: x["name"].lower()):
        print(f"  [Folder] {f['name']}")
        print(f"           id={f['id']}")

    for f in sorted(files, key=lambda x: x["name"].lower()):
        print(f"  [File]   {f['name']}")
        print(f"           id={f['id']}  type={f['mimeType']}")

    print("\n" + "=" * 60)
    print("Contents one level deep:")
    print("=" * 60)

    for folder in sorted(folders, key=lambda x: x["name"].lower()):
        children = client.list_folder(folder["id"])
        print(f"\n[DIR] {folder['name']} ({len(children)} items)")
        shown = sorted(
            children,
            key=lambda x: (x["mimeType"] != FOLDER_MIME, x["name"].lower()),
        )[:40]
        for c in shown:
            kind = "Folder" if c["mimeType"] == FOLDER_MIME else "File"
            print(f"    [{kind}] {c['name']}")
        if len(children) > 40:
            print(f"    ... and {len(children) - 40} more")


if __name__ == "__main__":
    main()
