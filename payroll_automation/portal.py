"""
Playwright (async) wrapper around the HCSS HeavyJob DailyTimeCard portal.

Confirmed selectors (discovered live 2026-06-18):
  Login (identity.hcssapps.com, two-step, with honeypot fields):
    username : input[name='username'][type='text']   (NOT the hidden decoys)
    next btn : button:has-text('NEXT')
    password : input[name='password'][type='password']
    login btn: button:has-text('LOG IN')
  Filter panel:
    open     : text=Filter
    date mode: #DateRangeSelect  (option label 'Custom')
    start    : #StartDate   (MM/DD/YYYY)
    end      : #EndDate
    jobs mode: input[name='J1'][value='Custom']
    job pick : text=Click to select  -> modal
    modal    : .modal input[type='search'] , row checkbox name=CheckList , OK = #MJST_SaveBtn
    apply    : #runBtn
  Timecard detail (per open card):
    approve  : #Approve   (dropdown-toggle)
    reject   : #Reject
    header   : contains 'Submitted by <Foreman>'
"""
from __future__ import annotations

import logging
import re
from typing import Any

from playwright.async_api import Page, TimeoutError as PWTimeout, async_playwright

logger = logging.getLogger(__name__)

PORTAL_URL = "https://heavyjob.hcssapps.com/DailyTimeCard"


def _norm(s: str | None) -> str:
    return re.sub(r"\s+", " ", (s or "").strip()).lower()


class HeavyJobPortal:
    """Drives the HeavyJob portal. Use as an async context manager."""

    def __init__(self, user: str, password: str, *, headed: bool = False, slow_mo: int = 0) -> None:
        self._user = user
        self._password = password
        self._headed = headed
        self._slow_mo = slow_mo
        self._pw = None
        self._browser = None
        self._ctx = None
        self.page: Page = None  # type: ignore[assignment]
        # if set (env PORTAL_AUTOMATION_SHOTDIR), screenshots are written per action step
        import os as _os
        self._shotdir = _os.getenv("PORTAL_AUTOMATION_SHOTDIR") or None
        self._shot_n = 0

    async def _shot(self, name: str) -> None:
        if not self._shotdir:
            return
        try:
            self._shot_n += 1
            await self.page.screenshot(
                path=f"{self._shotdir}/{self._shot_n:02d}_{name}.png",
                full_page=False,
            )
        except Exception:
            logger.warning("screenshot failed: %s", name, exc_info=True)

    # ── lifecycle ─────────────────────────────────────────────────────────
    async def __aenter__(self) -> "HeavyJobPortal":
        self._pw = await async_playwright().start()
        self._browser = await self._pw.chromium.launch(
            headless=not self._headed,
            slow_mo=self._slow_mo,
        )
        self._ctx = await self._browser.new_context(viewport={"width": 1500, "height": 950})
        self._ctx.set_default_timeout(45000)
        self.page = await self._ctx.new_page()
        return self

    async def __aexit__(self, *exc: Any) -> None:
        try:
            if self._browser:
                await self._browser.close()
        finally:
            if self._pw:
                await self._pw.stop()

    # ── login ─────────────────────────────────────────────────────────────
    async def login(self) -> None:
        p = self.page
        logger.info("Portal: navigating to login")
        await p.goto(PORTAL_URL, wait_until="domcontentloaded", timeout=60000)
        await p.wait_for_timeout(3000)

        # already authenticated (session)?
        if "heavyjob.hcssapps.com" in p.url and "identity" not in p.url:
            logger.info("Portal: already authenticated")
            await self._wait_app_ready()
            return

        # Step 1: username -> NEXT  (target the visible text field, avoid honeypots)
        await p.fill("input[name='username'][type='text']", self._user)
        await self._click_visible("button:has-text('NEXT')")
        # Step 2: password -> LOG IN  (the visible login button; a hidden decoy also matches)
        pwd = p.locator("input[name='password'][type='password']").locator("visible=true").first
        await pwd.wait_for(state="visible", timeout=20000)
        await pwd.fill(self._password)
        await self._click_visible("button:has-text('LOG IN')")

        await p.wait_for_url("**heavyjob.hcssapps.com/**", timeout=60000)
        await self._wait_app_ready()
        logger.info("Portal: logged in (url=%s)", p.url)

    async def _click_visible(self, selector: str, timeout: int = 20000) -> None:
        loc = self.page.locator(selector).locator("visible=true").first
        await loc.wait_for(state="visible", timeout=timeout)
        await loc.click()

    async def _wait_app_ready(self) -> None:
        # networkidle may never fire on the SPA (keep-alive/polling connections),
        # so treat it as best-effort and fall through to the fixed settle wait.
        try:
            await self.page.wait_for_load_state("networkidle", timeout=20000)
        except PWTimeout:
            logger.info("Portal: networkidle not reached in 20s — continuing anyway")
        await self.page.wait_for_timeout(6000)
        if "DailyTimeCard" not in self.page.url:
            await self.page.goto(PORTAL_URL, wait_until="domcontentloaded", timeout=60000)
            await self.page.wait_for_timeout(6000)

    # ── filtering ───────────────────────────────────────────────────────────
    async def filter_to(self, *, date_mmddyyyy: str, job_code: str) -> None:
        """Open the filter, set Custom single-day range + exactly one custom job, Apply.

        Robust to persisted filter state (the portal remembers prior selections).
        """
        p = self.page
        logger.info("Portal: filtering date=%s job=%s", date_mmddyyyy, job_code)
        await p.click("text=Filter")
        await p.wait_for_selector("#DateRangeSelect", timeout=20000)

        # Date -> Custom single day (datepicker overrides typed text, so set via JS)
        await p.select_option("#DateRangeSelect", label="Custom")
        await p.wait_for_timeout(600)
        await self._set_date_range(date_mmddyyyy)

        # Jobs -> Custom (click the ACE label so the reveal handler fires)
        await p.locator("#jobRadioGroup label", has_text="Custom").click()
        await p.wait_for_timeout(800)

        # open the job picker modal, select ONLY the target job, save
        await self._open_job_modal()
        await self._select_only_job(job_code)
        await p.click("#MJST_SaveBtn")
        await p.wait_for_timeout(1200)

        # Force sort = Foreman so the result list is grouped by foreman
        # (grayBackgroundHeader) with a per-card date cell (.width-90). The
        # portal persists the sort and may be on Date/Job, which changes the
        # list DOM and breaks foreman matching — pin it here for determinism.
        try:
            foreman_sort = p.locator("label:has(input[name='S1'][value='2'])").locator("visible=true").first
            if await foreman_sort.count():
                await foreman_sort.click()
                await p.wait_for_timeout(500)
        except Exception:
            logger.warning("Could not set sort=Foreman (continuing)", exc_info=True)

        # Apply
        await p.click("#runBtn")
        await p.wait_for_timeout(6000)
        await self._shot("applied")

        # ── HARD DATE GUARDRAIL ────────────────────────────────────────────
        # The datepicker has a UTC off-by-one bug when set programmatically, so
        # verify the *rendered* timecard dates actually equal the target before
        # any Approve/Reject can run. Refuse to proceed on mismatch.
        await self._assert_list_date(date_mmddyyyy)
        logger.info("Portal: filter applied (date verified)")

    async def _set_date_range(self, date_mmddyyyy: str) -> None:
        """Set Start==End==date by typing + Enter.

        Setting the value programmatically (JS) triggers a bootstrap-datepicker
        UTC parse that shifts the filter back one day, so we type like a user and
        commit with Enter, then verify the input text.
        """
        p = self.page
        for fid in ("#StartDate", "#EndDate"):
            await p.click(fid)
            await p.fill(fid, "")
            await p.type(fid, date_mmddyyyy, delay=30)
            await p.keyboard.press("Enter")
            await p.wait_for_timeout(400)
        start, end = await p.input_value("#StartDate"), await p.input_value("#EndDate")
        if start != date_mmddyyyy or end != date_mmddyyyy:
            raise RuntimeError(
                f"Could not set date range to {date_mmddyyyy} (Start={start} End={end})"
            )

    @staticmethod
    def _portal_date(date_mmddyyyy: str) -> str:
        """'06/17/2026' -> '6/17/2026' (portal renders without leading zeros)."""
        mm, dd, yyyy = date_mmddyyyy.split("/")
        return f"{int(mm)}/{int(dd)}/{yyyy}"

    async def _assert_list_date(self, date_mmddyyyy: str) -> None:
        """Best-effort pre-Apply check that the rendered cards are the target date.

        Only the per-card detail check (_open_card_matches) is authoritative; this
        is a cheap early guard against the datepicker's UTC off-by-one. If the list
        exposes no date cells (portal UI/sort variations), we do NOT abort — the
        per-card check still gates every Approve/Reject.
        """
        expected = self._portal_date(date_mmddyyyy)
        dates = await self.page.eval_on_selector_all(
            ".width-90",
            "els => els.filter(e=>e.offsetParent!==null)"
            ".map(e=>(e.innerText||'').trim()).filter(t=>/\\d{1,2}\\/\\d{1,2}\\/\\d{4}/.test(t))",
        )
        if not dates:
            logger.warning(
                "Date guardrail: no date cells rendered for %s — relying on per-card "
                "detail date check instead", expected,
            )
            return
        bad = [d for d in dates if d != expected]
        if bad:
            raise RuntimeError(
                f"Date guardrail FAILED: expected all cards == {expected} but saw {sorted(set(dates))}. "
                "Refusing to act (filter applied the wrong date)."
            )

    async def _open_job_modal(self) -> None:
        """Click #filterCustomJobs ('Click to select' / 'N selected') until the
        Select Jobs modal (#MJST_SaveBtn) is visible."""
        p = self.page
        save = p.locator("#MJST_SaveBtn")
        for _ in range(6):
            if await save.count() and await save.is_visible():
                return
            try:
                await p.locator("#filterCustomJobs").locator("visible=true").first.click(timeout=4000)
            except PWTimeout:
                pass
            await p.wait_for_timeout(1500)
        if await save.count() and await save.is_visible():
            return
        raise RuntimeError("Could not open the Select Jobs modal")

    async def _select_only_job(self, job_code: str) -> None:
        """In the open job modal, clear any prior selection, then select *job_code*.

        Tabs: #li1 = 'All', #li2 = 'Selected (N)'. Search box = input[type=search].input-sm.
        """
        p = self.page
        # clear previously-selected jobs via the 'Selected (N)' tab
        # (#li1/#li2 each appear twice: a hidden template + the live one — scope to visible)
        try:
            sel_tab = p.locator("#li2").locator("visible=true").first
            if await sel_tab.count() and await sel_tab.is_visible():
                await sel_tab.click()
                await p.wait_for_timeout(800)
                for _ in range(50):
                    boxes = p.locator("tbody tr input[type='checkbox']:checked").locator("visible=true")
                    if not await boxes.count():
                        break
                    await boxes.first.uncheck(force=True)
                    await p.wait_for_timeout(300)
                await p.locator("#li1").locator("visible=true").first.click()  # back to 'All' tab
                await p.wait_for_timeout(600)
        except Exception:
            logger.warning("Could not fully clear prior job selection (continuing)", exc_info=True)

        # search + check the exact job-code row (scope to the visible search box)
        search = p.locator("input[type='search'].input-sm").locator("visible=true").first
        await search.fill(job_code)
        await p.wait_for_timeout(1500)
        row = p.locator("tbody tr", has_text=job_code).locator("visible=true").first
        await row.wait_for(state="visible", timeout=15000)
        cb = row.locator("input[type='checkbox']")
        if not await cb.is_checked():
            await cb.check(force=True)
        await p.wait_for_timeout(300)

    # ── matching + actions ───────────────────────────────────────────────────
    async def open_card_for_foreman(self, *, foreman: str, job_code: str, date_mmddyyyy: str) -> bool:
        """
        Open the timecard for *foreman* + *job_code* on *date_mmddyyyy* in the
        detail panel, then verify the open card really matches. Returns False if
        no matching card exists or verification fails (caller must not act).

        Right list is a table: foreman is a group header <td class=grayBackgroundHeader>,
        each card is a following <tr> containing a date cell (.width-90).
        """
        p = self.page
        # foreman group header (scope to the visible list)
        header = p.locator("td.grayBackgroundHeader", has_text=foreman).locator("visible=true").first
        if not await header.count():
            logger.warning("Foreman header not found in list: %s", foreman)
            return False

        # the first card row after this foreman's header (a <tr> with a date cell)
        card = header.locator(
            "xpath=ancestor::tr[1]/following-sibling::tr[.//*[contains(@class,'width-90')]][1]"
        )
        if not await card.count():
            logger.warning("No card row under foreman %s", foreman)
            return False
        await card.first.click()
        await p.wait_for_timeout(3500)

        if not await self._open_card_matches(foreman=foreman, job_code=job_code, date_mmddyyyy=date_mmddyyyy):
            logger.warning("Opened card did not verify for foreman=%s job=%s date=%s",
                           foreman, job_code, date_mmddyyyy)
            return False
        return True

    async def _open_card_matches(self, *, foreman: str, job_code: str, date_mmddyyyy: str) -> bool:
        """Verify the open detail panel really is this foreman's card for job + date.

        Foreman is read from the detail's name elements (div.name / h2.name /
        div.description) — NOT the status line, because once a card is actioned the
        status line becomes 'Approved by <approver>' (the approver, not the foreman).
        Those name elements always show the open card's foreman regardless of state.
        """
        p = self.page
        foreman_ok = await p.evaluate(
            """(fm) => {
                const norm = s => (s || '').replace(/\\s+/g, ' ').trim().toLowerCase();
                const want = norm(fm);
                return [...document.querySelectorAll('div.name, h2.name, div.description')]
                    .some(e => e.offsetParent !== null && norm(e.innerText) === want);
            }""",
            foreman,
        )
        if not foreman_ok:
            logger.warning("Detail foreman check failed — want %r not in detail name elements", foreman)
            return False

        # detail header shows e.g. 'WEDNESDAY, June 17' / 'YESTERDAY, June 17'
        mm, dd, _ = date_mmddyyyy.split("/")
        month = ["January", "February", "March", "April", "May", "June", "July",
                 "August", "September", "October", "November", "December"][int(mm) - 1]
        body = _norm(await p.locator("body").inner_text())
        if _norm(f"{month} {int(dd)}") not in body:
            logger.warning("Detail date check failed (expected %s %s)", month, int(dd))
            return False
        return job_code.lower() in body

    async def open_card_state(self) -> str:
        """Current portal state of the open card, read from the statusText class
        token: 'approved' | 'rejected' | 'submitted' | 'revised' | '' (unknown)."""
        try:
            loc = self.page.locator("[class*='statusText']").locator("visible=true").first
            cls = (await loc.get_attribute("class") or "").lower() if await loc.count() else ""
        except Exception:
            cls = ""
        for state in ("approved", "rejected", "submitted", "revised"):
            if state in cls:
                return state
        return ""

    async def approve_open_card(self) -> None:
        await self._click_action("#Approve", approve=True)

    async def reject_open_card(self) -> None:
        await self._click_action("#Reject", approve=False)

    async def _click_action(self, selector: str, *, approve: bool) -> None:
        """Click Approve/Reject and best-effort confirm any follow-up dialog.

        NOTE: the confirmation/menu flow after the click is not yet verified live
        (build was dry-run). The defensive handling below covers the common cases:
          - Approve is a dropdown-toggle -> may open a menu with an 'Approve' item.
          - Reject may open a dialog needing a reason + a confirm button.
        Verify on the first live run.
        """
        p = self.page
        await self._shot("before_action")
        await p.locator(selector).first.click()
        await p.wait_for_timeout(1200)
        await self._shot("after_click")

        if approve:
            # #Approve is a dropdown-toggle: clicking it opens a menu with a
            # plain 'Approve' item that performs the approval (no further dialog).
            menu_item = p.locator(".dropdown-menu:visible a:has-text('Approve'), "
                                  ".dropdown-menu:visible li:has-text('Approve')").first
            if await menu_item.count() and await menu_item.is_visible():
                await menu_item.click()
                await p.wait_for_timeout(1500)
            await self._shot("before_confirm")
            await self._shot("after_confirm")
            return

        # Reject opens a 'Rejection Notes' modal: fill the note, then click Save.
        reason = p.locator("textarea:visible").first
        if await reason.count() and await reason.is_visible():
            await reason.fill("Auto-rejected by Gould APM verification")
            await p.wait_for_timeout(300)
        await self._shot("before_confirm")
        save = p.locator("button:has-text('Save')").locator("visible=true").first
        if await save.count() and await save.is_visible():
            await save.click()
            await p.wait_for_timeout(1800)
        else:
            # fallback for any other confirm dialog shape
            confirm = p.locator(
                ".modal button:has-text('OK'), .modal button:has-text('Confirm'), "
                ".modal button:has-text('Yes')"
            ).locator("visible=true").first
            if await confirm.count() and await confirm.is_visible():
                await confirm.click()
                await p.wait_for_timeout(1800)
        await self._shot("after_confirm")
