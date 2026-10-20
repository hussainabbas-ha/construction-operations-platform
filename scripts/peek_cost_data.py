"""Drill into Awarded Project Managment / 2026 / <job> / Cost Data and list contents."""
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


def find_child(items: list[dict], name: str) -> dict | None:
    name_l = name.lower()
    for item in items:
        if item["name"].lower() == name_l:
            return item
    return None


def find_folder_fuzzy(items: list[dict], needles: list[str]) -> dict | None:
    for item in items:
        if item["mimeType"] != FOLDER_MIME:
            continue
        n = item["name"].lower()
        if any(needle in n for needle in needles):
            return item
    return None


def main() -> None:
    client = GoogleDriveClient()

    awarded = client.list_folder(AWARDED_ID)
    year_2026 = find_child(awarded, "2026")
    if not year_2026:
        print("Could not find '2026' under Awarded Project Managment")
        print("Top-level:", [i["name"] for i in awarded])
        return

    jobs = [
        i
        for i in client.list_folder(year_2026["id"])
        if i["mimeType"] == FOLDER_MIME and JOB_PATTERN.match(i["name"])
    ]
    print(f"2026 job folders found: {len(jobs)}")
    for j in jobs[:15]:
        print(f"  - {j['name']}")
    if len(jobs) > 15:
        print(f"  ... and {len(jobs) - 15} more")

    if not jobs:
        print("No job folders found under 2026")
        return

    # Prefer a job that has a Cost Data folder
    chosen = None
    cost_folder = None
    for job in jobs:
        children = client.list_folder(job["id"])
        cost = find_folder_fuzzy(
            children,
            ["cost data", "costdata", "cost-data", "costs"],
        )
        if cost:
            chosen = job
            cost_folder = cost
            job_children = children
            break

    if not chosen:
        chosen = jobs[0]
        job_children = client.list_folder(chosen["id"])
        print(f"\nNo 'Cost Data' folder found in scanned jobs.")
        print(f"Inspecting first job: {chosen['name']}")
        print("Subfolders/files:")
        for c in job_children:
            kind = "Folder" if c["mimeType"] == FOLDER_MIME else "File"
            print(f"  [{kind}] {c['name']}")
        return

    print(f"\nSelected project: {chosen['name']}")
    print("Project top-level:")
    for c in sorted(job_children, key=lambda x: (x["mimeType"] != FOLDER_MIME, x["name"].lower())):
        kind = "Folder" if c["mimeType"] == FOLDER_MIME else "File"
        print(f"  [{kind}] {c['name']}")

    print(f"\nCost folder found as: '{cost_folder['name']}'")
    contents = client.list_folder(cost_folder["id"], page_size=200)
    print(f"Contents ({len(contents)} items):\n")
    for c in sorted(contents, key=lambda x: (x["mimeType"] != FOLDER_MIME, x["name"].lower())):
        kind = "Folder" if c["mimeType"] == FOLDER_MIME else "File"
        print(f"  [{kind}] {c['name']}")
        print(f"           type={c['mimeType']}")
        print(f"           id={c['id']}")


if __name__ == "__main__":
    main()
