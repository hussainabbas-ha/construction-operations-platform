"""List Cost Data contents for every 2026 Awarded project."""
from __future__ import annotations

import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from api.google_drive_client import GoogleDriveClient

FOLDER_MIME = "application/vnd.google-apps.folder"
AWARDED_ID = "1XZkfZjNMBPcOqQ9hmVSz7W-Z-nC_HspD"
JOB_PATTERN = re.compile(r"^\d+\w*\-")


def find_cost_folder(items: list[dict]) -> dict | None:
    for item in items:
        if item["mimeType"] != FOLDER_MIME:
            continue
        n = item["name"].lower()
        if any(x in n for x in ("cost data", "costdata", "cost-data")):
            return item
    return None


def main() -> None:
    client = GoogleDriveClient()

    year_2026 = next(
        (i for i in client.list_folder(AWARDED_ID) if i["name"] == "2026"),
        None,
    )
    if not year_2026:
        print("Could not find 2026 folder")
        return

    jobs = [
        i
        for i in client.list_folder(year_2026["id"])
        if i["mimeType"] == FOLDER_MIME and JOB_PATTERN.match(i["name"])
    ]
    jobs = sorted(jobs, key=lambda x: x["name"].lower())

    print(f"Awarded Project Managment / 2026 — {len(jobs)} projects\n")
    print("=" * 70)

    for job in jobs:
        children = client.list_folder(job["id"])
        cost = find_cost_folder(children)
        print(f"\nPROJECT: {job['name']}")
        if not cost:
            print("  Cost Data: NOT FOUND")
            folders = [c["name"] for c in children if c["mimeType"] == FOLDER_MIME]
            print(f"  Top-level folders: {', '.join(sorted(folders)) or '(none)'}")
            continue

        contents = client.list_folder(cost["id"], page_size=200)
        print(f"  Cost Data folder: '{cost['name']}' ({len(contents)} items)")
        if not contents:
            print("    (empty)")
            continue
        for c in sorted(
            contents,
            key=lambda x: (x["mimeType"] != FOLDER_MIME, x["name"].lower()),
        ):
            kind = "Folder" if c["mimeType"] == FOLDER_MIME else "File"
            print(f"    [{kind}] {c['name']}")


if __name__ == "__main__":
    main()
