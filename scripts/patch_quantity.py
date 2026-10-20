"""
patch_quantity.py
─────────────────
Manually fix wrong quantity values in the quantity_tracking_results DB cache.

USE CASE:
    The frontend shows incorrect planned/installed quantities that don't
    match HCSS.  This script lets you overwrite a specific row directly
    in the DB and recalculates all derived fields so everything stays
    consistent (remaining_quantity, percent_complete, status, alert).

WARNING:
    The next unified cron run will overwrite your fix if HCSS still
    returns bad data.  Fix the source in HCSS for a permanent solution.

USAGE:
    # Interactive mode — prompts you for all inputs
    python patch_quantity.py

    # CLI mode — pass everything as arguments
    python patch_quantity.py \
        --date 2026-05-01 \
        --cost-code 13701111 \
        --job-code 23045 \
        --planned 1.0 \
        --installed 0.0

    # Dry-run — shows what WOULD change without touching the DB
    python patch_quantity.py --date 2026-05-01 --cost-code 13701111 \
        --job-code 23045 --planned 1.0 --installed 0.0 --dry-run

    # List all rows for a date — useful to find the right cost_code/job_code
    python patch_quantity.py --list --date 2026-05-01

    # List rows for a specific date filtered by job
    python patch_quantity.py --list --date 2026-05-01 --job-code 23045
"""
from __future__ import annotations

import argparse
import sys
from datetime import datetime, timezone
from pathlib import Path

# ── Ensure repo root is on path ───────────────────────────────────────────────
_REPO_ROOT = Path(__file__).resolve().parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from dotenv import load_dotenv
load_dotenv(_REPO_ROOT / ".env")

# ── Threshold mirrors quantity_tracking/tracker.py and quantity_routes.py ─────
ALERT_THRESHOLD     = 75.0
NEAR_THRESHOLD      = 75.0
OVER_THRESHOLD      = 100.0

# ── Terminal colours ──────────────────────────────────────────────────────────
GREEN  = "\033[92m"
RED    = "\033[91m"
YELLOW = "\033[93m"
CYAN   = "\033[96m"
BOLD   = "\033[1m"
RESET  = "\033[0m"

def _ok(msg):   print(f"{GREEN}  ✓ {msg}{RESET}")
def _fail(msg): print(f"{RED}  ✗ {msg}{RESET}")
def _warn(msg): print(f"{YELLOW}  ⚠ {msg}{RESET}")
def _info(msg): print(f"{CYAN}  → {msg}{RESET}")
def _sep():     print("─" * 65)


# ── Business logic ────────────────────────────────────────────────────────────

def _recalculate(planned: float, installed: float) -> dict:
    """
    Recalculate all derived fields from planned + installed.
    Mirrors the exact logic in quantity_routes.py _aggregate() and
    quantity_tracking/tracker.py _calculate_status().
    """
    remaining   = round(planned - installed, 4)
    percent     = round((installed / planned) * 100, 2) if planned > 0 else 0.0

    if percent >= OVER_THRESHOLD:
        status = "OVER_RISK"
    elif percent >= NEAR_THRESHOLD:
        status = "NEAR_COMPLETION"
    else:
        status = "ON_TRACK"

    alert = percent >= ALERT_THRESHOLD

    return {
        "remaining_quantity": remaining,
        "percent_complete":   percent,
        "status":             status,
        "alert":              alert,
    }


def _status_colour(status: str) -> str:
    return {
        "OVER_RISK":       RED,
        "NEAR_COMPLETION": YELLOW,
        "ON_TRACK":        GREEN,
    }.get(status, RESET)


# ── DB helpers ────────────────────────────────────────────────────────────────

def _get_db():
    from api.database import SessionLocal
    return SessionLocal()


def _find_rows(db, tracking_date: str, cost_code: str | None, job_code: str | None):
    """
    Find matching rows in quantity_tracking_results.
    Supports partial match: can filter by date only, date+cost_code,
    date+job_code, or date+cost_code+job_code.
    """
    from api.database import QuantityTrackingResult

    query = db.query(QuantityTrackingResult).filter(
        QuantityTrackingResult.tracking_date == tracking_date
    )

    if cost_code:
        # Exact match first, then contains — handles both "13701111" and "137011"
        query = query.filter(
            QuantityTrackingResult.cost_code.ilike(f"%{cost_code}%")
        )

    if job_code:
        query = query.filter(
            QuantityTrackingResult.job_code.ilike(f"%{job_code}%")
        )

    return query.all()


# ── LIST mode ─────────────────────────────────────────────────────────────────

def cmd_list(args):
    """Print all rows for a date, optionally filtered by job_code."""
    if not args.date:
        args.date = input("Tracking date (YYYY-MM-DD): ").strip()

    _validate_date(args.date)

    db = _get_db()
    try:
        rows = _find_rows(db, args.date, cost_code=None, job_code=args.job_code)
    finally:
        db.close()

    if not rows:
        _warn(f"No rows found in DB for date={args.date}"
              + (f" job_code contains '{args.job_code}'" if args.job_code else ""))
        _warn("The cron job may not have synced this date yet.")
        return

    print(f"\n{BOLD}Rows in quantity_tracking_results for date={args.date}"
          + (f", job_code contains '{args.job_code}'" if args.job_code else "")
          + f"  ({len(rows)} row(s)){RESET}")
    _sep()

    # Header
    print(f"  {'Cost Code':<15}  {'Job Code':<12}  {'Type':<14}  "
          f"{'Unit':<6}  {'Planned':>10}  {'Installed':>10}  {'Remaining':>10}  "
          f"{'%':>6}  Status")
    _sep()

    for r in rows:
        sc = _status_colour(r.status)
        print(
            f"  {r.cost_code:<15}  {r.job_code:<12}  {r.cost_type:<14}  "
            f"{r.unit:<6}  {r.planned_quantity:>10,.4f}  "
            f"{r.installed_quantity:>10,.4f}  {r.remaining_quantity:>10,.4f}  "
            f"{r.percent_complete:>5.1f}%  "
            f"{sc}{r.status}{RESET}"
        )

    print()


# ── PATCH mode ────────────────────────────────────────────────────────────────

def cmd_patch(args):
    """
    Find and update a specific row.

    Rules:
      - Provide --date, at least one of --cost-code / --job-code,
        and the new --planned / --installed values.
      - When ALL of those are supplied via CLI args, the update runs
        immediately with no prompts and no confirmation.
      - When any value is missing the script prompts only for that
        missing value — it never re-asks for something already provided.
    """

    # ── full-CLI mode: true when everything was passed as arguments ──────
    # Used later to skip the confirmation prompt entirely.
    full_cli = bool(
        args.date
        and (args.cost_code or args.job_code)
        and args.planned is not None
        and args.installed is not None
    )

    # ── date ─────────────────────────────────────────────────────────────
    if not args.date:
        args.date = input("Tracking date (YYYY-MM-DD): ").strip()
    _validate_date(args.date)

    # ── identifier: require at least one of cost_code / job_code ─────────
    # Never prompt for one that was already given on the command line.
    if not args.cost_code and not args.job_code:
        _info("Provide at least one identifier to locate the row.")
        args.cost_code = input("  Cost code (e.g. 13701111)  [or leave blank]: ").strip() or None
        args.job_code  = input("  Job code  (e.g. 23045)     [or leave blank]: ").strip() or None

    if not args.cost_code and not args.job_code:
        _fail("At least one of --cost-code or --job-code is required.")
        sys.exit(1)

    # ── find matching rows ────────────────────────────────────────────────
    db = _get_db()
    try:
        rows = _find_rows(db, args.date, args.cost_code, args.job_code)

        if not rows:
            filters = f"date={args.date}"
            if args.cost_code:
                filters += f"  cost_code contains '{args.cost_code}'"
            if args.job_code:
                filters += f"  job_code contains '{args.job_code}'"
            _fail(f"No matching row found for {filters}")
            _warn("Use --list to browse available rows for this date.")
            return

        # multiple matches — let user pick (only happens in interactive mode;
        # in full-CLI mode this is unlikely but handled safely)
        if len(rows) > 1:
            print(f"\n{YELLOW}  ⚠ Found {len(rows)} matching rows — pick one:{RESET}")
            for i, r in enumerate(rows):
                print(
                    f"  [{i}]  cost_code={r.cost_code}  job_code={r.job_code}  "
                    f"unit={r.unit}  planned={r.planned_quantity}  "
                    f"installed={r.installed_quantity}  status={r.status}"
                )
            choice = input("  Enter index [0]: ").strip() or "0"
            try:
                row = rows[int(choice)]
            except (ValueError, IndexError):
                _fail("Invalid choice")
                return
        else:
            row = rows[0]

        # ── show current values ──────────────────────────────────────────
        print(f"\n{BOLD}Current values:{RESET}")
        _sep()
        _info(f"Row ID         : {row.id}")
        _info(f"Cost Code      : {row.cost_code}")
        _info(f"Description    : {row.description}")
        _info(f"Job Code       : {row.job_code}")
        _info(f"Unit           : {row.unit}")
        _info(f"Cost Type      : {row.cost_type}")
        _info(f"Planned Qty    : {row.planned_quantity}")
        _info(f"Installed Qty  : {row.installed_quantity}")
        _info(f"Remaining Qty  : {row.remaining_quantity}")
        _info(f"% Complete     : {row.percent_complete}")
        _info(f"Status         : {row.status}")
        _info(f"Alert          : {row.alert}")
        print()

        # ── collect new values — only prompt if NOT already in args ──────
        if args.planned is None:
            raw = input(
                f"  New planned_quantity [{row.planned_quantity}] "
                "(press Enter to keep): "
            ).strip()
            new_planned = float(raw) if raw else row.planned_quantity
        else:
            new_planned = args.planned

        if args.installed is None:
            raw = input(
                f"  New installed_quantity [{row.installed_quantity}] "
                "(press Enter to keep): "
            ).strip()
            new_installed = float(raw) if raw else row.installed_quantity
        else:
            new_installed = args.installed

        # ── recalculate derived fields ───────────────────────────────────
        derived = _recalculate(new_planned, new_installed)

        # ── show diff ────────────────────────────────────────────────────
        print(f"\n{BOLD}Changes preview:{RESET}")
        _sep()

        def _diff(label, old, new, fmt="{:.4f}"):
            if old != new:
                print(
                    f"  {label:<22}  "
                    f"{RED}{fmt.format(old)}{RESET}  →  "
                    f"{GREEN}{fmt.format(new)}{RESET}"
                )
            else:
                print(f"  {label:<22}  {fmt.format(old)}  (unchanged)")

        _diff("planned_quantity",   row.planned_quantity,   new_planned)
        _diff("installed_quantity", row.installed_quantity, new_installed)
        _diff("remaining_quantity", row.remaining_quantity, derived["remaining_quantity"])
        _diff("percent_complete",   row.percent_complete,   derived["percent_complete"], fmt="{:.2f}")
        _diff("status",             row.status,             derived["status"],           fmt="{}")
        _diff("alert",              row.alert,              derived["alert"],             fmt="{}")
        print()

        # ── dry-run guard ────────────────────────────────────────────────
        if args.dry_run:
            _warn("DRY RUN — nothing written to the database.")
            return

        # ── confirmation — skipped when all args were on the command line ─
        if not full_cli:
            confirm = input("  Apply these changes? [y/N]: ").strip().lower()
            if confirm not in ("y", "yes"):
                _warn("Aborted — no changes made.")
                return

        # ── Apply ────────────────────────────────────────────────────────
        row.planned_quantity   = new_planned
        row.installed_quantity = new_installed
        row.remaining_quantity = derived["remaining_quantity"]
        row.percent_complete   = derived["percent_complete"]
        row.status             = derived["status"]
        row.alert              = derived["alert"]
        row.updated_at         = datetime.now(timezone.utc)

        # Also update the cache summary counts for this date so the
        # /count endpoint returns accurate numbers
        _update_cache_summary(db, row.tracking_date)

        db.commit()
        _ok(
            f"Row updated: cost_code={row.cost_code}  job_code={row.job_code}  "
            f"date={row.tracking_date}  "
            f"planned={row.planned_quantity}  installed={row.installed_quantity}  "
            f"status={row.status}"
        )

    except Exception as exc:
        db.rollback()
        _fail(f"DB error: {exc}")
        raise
    finally:
        db.close()


def _update_cache_summary(db, tracking_date: str) -> None:
    """
    Recount on_track / near_completion / over_risk for the date and
    update the QuantityTrackingCache header row so /count stays accurate.
    """
    from api.database import QuantityTrackingCache, QuantityTrackingResult

    all_rows = db.query(QuantityTrackingResult).filter(
        QuantityTrackingResult.tracking_date == tracking_date
    ).all()

    total    = len(all_rows)
    on_track = sum(1 for r in all_rows if r.status == "ON_TRACK")
    near     = sum(1 for r in all_rows if r.status == "NEAR_COMPLETION")
    over     = sum(1 for r in all_rows if r.status == "OVER_RISK")

    cache_row = db.query(QuantityTrackingCache).filter(
        QuantityTrackingCache.tracking_date == tracking_date
    ).first()

    if cache_row:
        cache_row.total_cost_codes      = total
        cache_row.on_track_count        = on_track
        cache_row.near_completion_count = near
        cache_row.over_risk_count       = over
        cache_row.updated_at            = datetime.now(timezone.utc)
        _info(
            f"Cache summary updated: date={tracking_date}  "
            f"total={total}  on_track={on_track}  "
            f"near_completion={near}  over_risk={over}"
        )
    else:
        _warn(
            f"No QuantityTrackingCache row found for date={tracking_date} — "
            "summary counts not updated (detail row was still patched)."
        )


# ── Validation helpers ────────────────────────────────────────────────────────

def _validate_date(date_str: str) -> None:
    try:
        datetime.strptime(date_str, "%Y-%m-%d")
    except ValueError:
        _fail(f"Invalid date '{date_str}' — expected YYYY-MM-DD")
        sys.exit(1)


# ── Argument parser ───────────────────────────────────────────────────────────

def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Manually patch quantity_tracking_results rows in the DB.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  # Interactive — prompts for everything
  python patch_quantity.py

  # List all rows for a date
  python patch_quantity.py --list --date 2026-05-01

  # List rows filtered by job
  python patch_quantity.py --list --date 2026-05-01 --job-code 23045

  # Patch with all args (no prompts)
  python patch_quantity.py \\
      --date 2026-05-01 \\
      --cost-code 13701111 \\
      --job-code 23045 \\
      --planned 1.0 \\
      --installed 0.0

  # Preview without writing
  python patch_quantity.py \\
      --date 2026-05-01 \\
      --cost-code 13701111 \\
      --job-code 23045 \\
      --planned 1.0 \\
      --installed 0.0 \\
      --dry-run
        """,
    )

    parser.add_argument(
        "--list", action="store_true",
        help="List rows for the given date instead of patching",
    )
    parser.add_argument(
        "--date",
        help="Tracking date YYYY-MM-DD",
    )
    parser.add_argument(
        "--cost-code",
        dest="cost_code",
        help="Cost code to match (partial match supported, e.g. '13701111')",
    )
    parser.add_argument(
        "--job-code",
        dest="job_code",
        help="Job code to match (partial match supported, e.g. '23045')",
    )
    parser.add_argument(
        "--planned",
        type=float,
        default=None,
        help="New planned_quantity value",
    )
    parser.add_argument(
        "--installed",
        type=float,
        default=None,
        help="New installed_quantity value",
    )
    parser.add_argument(
        "--dry-run",
        dest="dry_run",
        action="store_true",
        help="Show what would change without writing to the DB",
    )

    return parser.parse_args()


# ── Entry point ───────────────────────────────────────────────────────────────

def main() -> None:
    print(f"\n{BOLD}{'=' * 65}{RESET}")
    print(f"{BOLD}  Gould APM — Quantity Patch Tool{RESET}")
    print(f"{BOLD}{'=' * 65}{RESET}\n")

    args = _parse_args()

    if args.list:
        cmd_list(args)
    else:
        cmd_patch(args)


if __name__ == "__main__":
    main()