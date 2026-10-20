"""
schedule_monitoring/test_schedule.py
──────────────────────────────────────
Quick smoke-test for the schedule monitoring module.

Runs the engine against last week's data (from DB + HCSS job details)
and prints a summary to stdout.  No data is stored.

Usage:
    .venv\Scripts\python.exe -m schedule_monitoring.test_schedule
    .venv\Scripts\python.exe -m schedule_monitoring.test_schedule --start 2026-05-01 --end 2026-05-31
    .venv\Scripts\python.exe -m schedule_monitoring.test_schedule --burn-rate
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import date, timedelta


def _last_friday() -> str:
    today = date.today()
    days_since_friday = (today.weekday() - 4) % 7
    return (today - timedelta(days=days_since_friday)).strftime("%Y-%m-%d")


def run_analysis(start: str, end: str) -> None:
    from schedule_monitoring.engine import run_schedule_monitoring

    print("=" * 70)
    print(f"SCHEDULE MONITORING  |  {start} → {end}")
    print("=" * 70)

    report = run_schedule_monitoring(start_date=start, end_date=end)

    print(f"\nTotal jobs analysed : {report.total_jobs}")
    print(f"Total alerts        : {report.total_alerts}")
    print(f"  Critical          : {report.critical_count}")
    print(f"  Warnings          : {report.warning_count}")
    print(f"Jobs with critical  : {report.jobs_with_critical}")
    print(f"Jobs with warnings  : {report.jobs_with_warnings}")

    if report.job_summaries:
        print("\n── Job Summaries ────────────────────────────────────────────────")
        for j in sorted(report.job_summaries,
                        key=lambda x: (0 if x["status"] == "CRITICAL" else
                                       1 if x["status"] == "WARNING" else 2)):
            bar = f"cost {j['cost_pct']:5.1f}%  qty {j['quantity_pct']:5.1f}%"
            print(
                f"  [{j['status']:<8}] {j['job_code']:<12} {bar}"
                f"  crit={j['critical_alerts']} warn={j['warning_alerts']}"
                f"  last_tc={j['last_timecard_date'] or '—'}"
            )

    if report.alerts:
        print("\n── Alerts ───────────────────────────────────────────────────────")
        for a in report.alerts:
            icon = "🚨" if a.severity == "critical" else "⚠️ "
            cc = f" [{a.cost_code_name}]" if a.cost_code_name else ""
            print(f"  {icon} {a.job_code}{cc}: {a.message}")
    else:
        print("\n✅ No alerts — all jobs on track.")

    print("\n" + "=" * 70)


def run_burn_rate(lookback_weeks: int) -> None:
    from schedule_monitoring.burn_rate import calculate_burn_rates

    print("=" * 70)
    print(f"BURN RATE ANALYSIS  |  last {lookback_weeks} weeks")
    print("=" * 70)

    rates = calculate_burn_rates(lookback_weeks=lookback_weeks)

    if not rates:
        print("No data in DB for the requested period.")
        return

    print(f"\nJobs with burn rate data: {len(rates)}")
    print(
        f"Jobs at budget overrun risk: "
        f"{sum(1 for j in rates if any(cc.budget_overrun_risk for cc in j.cost_codes))}"
    )

    print("\n── Per-Job Burn Rates ───────────────────────────────────────────────")
    for j in rates:
        exhaust = j.projected_budget_exhaust or "N/A"
        print(
            f"  {j.job_code:<14}"
            f"  cost {j.cost_pct:5.1f}%  ${j.weekly_cost_rate:>10,.0f}/wk"
            f"  budget exhausts: {exhaust}"
            f"  trend: {j.cost_trend}"
        )

    print("\n── Cost Codes at Budget Overrun Risk ───────────────────────────────")
    found = False
    for j in rates:
        for cc in j.cost_codes:
            if cc.budget_overrun_risk:
                found = True
                print(
                    f"  {j.job_code}  {cc.cost_code:<12}  {cc.description[:40]}"
                    f"  budget exhausts {cc.projected_budget_exhaust}"
                    f"  qty done {cc.projected_qty_completion or 'N/A'}"
                )
    if not found:
        print("  None — all cost codes within budget pace.")

    print("\n" + "=" * 70)


def main() -> None:
    parser = argparse.ArgumentParser(description="Schedule monitoring smoke test")
    parser.add_argument("--start", default=None, help="Start date YYYY-MM-DD")
    parser.add_argument("--end",   default=None, help="End date YYYY-MM-DD")
    parser.add_argument("--burn-rate", action="store_true", help="Run burn rate analysis")
    parser.add_argument("--lookback", type=int, default=8, help="Burn rate lookback weeks")
    args = parser.parse_args()

    if args.burn_rate:
        run_burn_rate(args.lookback)
        return

    friday = _last_friday()
    start  = args.start or (date.fromisoformat(friday) - timedelta(days=6)).strftime("%Y-%m-%d")
    end    = args.end   or friday

    run_analysis(start, end)


if __name__ == "__main__":
    main()
