"""
HeavyJob portal browser automation for Construction Operations API.

Drives the HCSS HeavyJob web portal (https://heavyjob.hcssapps.com/DailyTimeCard)
with Playwright (async API) to Approve / Reject timecards based on the result of
the daily payroll verification (``payroll_verification.verifier``).

Status -> portal action mapping:
    APPROVED -> Approve
    FLAGGED  -> Approve   (soft warnings only)
    REJECTED -> Reject

Match policy: a verification result is matched to a portal timecard by
``job_code`` + ``foreman`` name, so only the exact timecard the backend
flagged is touched.

Callers must ``await run_portal_actions(...)``.
"""
from payroll_automation.runner import run_portal_actions

__all__ = ["run_portal_actions"]
