from __future__ import annotations

import csv
import io
from dataclasses import asdict
from typing import Any

from payroll_verification.verifier import TimecardResult


def group_results(results: list[TimecardResult]) -> dict[str, list[TimecardResult]]:
    grouped: dict[str, list[TimecardResult]] = {"REJECTED": [], "FLAGGED": [], "APPROVED": []}
    for r in results:
        grouped[r.status].append(r)
    return grouped


def results_to_rows(results: list[TimecardResult]) -> list[dict[str, Any]]:
    # asdict() yields exactly the dataclass fields (incl. the flat `reasons` list
    # and job_description). No derived/duplicate fields are added.
    return [asdict(r) for r in results]


def results_to_csv_bytes(results: list[TimecardResult]) -> bytes:
    rows = results_to_rows(results)
    out = io.StringIO()
    # Fixed column order derived from the dataclass so an empty result still
    # produces a valid CSV with headers.
    from dataclasses import fields as _fields
    fieldnames = [f.name for f in _fields(TimecardResult)]
    writer = csv.DictWriter(out, fieldnames=fieldnames)
    writer.writeheader()
    for row in rows:
        csv_row = dict(row)
        csv_row["reasons"] = "; ".join(row["reasons"]) if row["reasons"] else ""
        writer.writerow(csv_row)
    return out.getvalue().encode("utf-8")
