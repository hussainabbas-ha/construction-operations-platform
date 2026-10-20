# Quantity Dirty-Date via jobCosts?modifiedSince — Findings
**Run date:** 2026-07-21  
**Goal:** Test whether `POST /v1/jobCosts/advancedRequest?modifiedSince` can be the dirty-date discovery signal for quantity (since `costTypeQuantities/adjustments` returned 0).

---

## Headline

| Question | Answer |
|---|---|
| Does jobCosts carry production quantity? | **Yes** — docs: `quantity` = "The production quantity"; `entryType` enum includes `quantity` |
| Does `modifiedSince` **contain** the dirty days (Jul 15/17/20)? | **Yes — all 3 appear** |
| Is `modifiedSince` a **precise** dirty signal here? | **No — it returns the ENTIRE history every call** |
| Is it usable as-is in cron? | **No — firehose + hard rate limiting (HTTP 429)** |

**Conclusion:** jobCosts `modifiedSince` technically covers the dirty dates, but this tenant bumps `SyncTimestamp` across its whole dataset, so `modifiedSince` returns ~everything. It cannot distinguish "actually edited" from "re-synced." It is not a clean dirty-date detector.

---

## Evidence

### 1. Volume is enormous for any window

| modifiedSince window | Rows before cap | Behavior |
|---|---|---|
| last 5 days | **167,000+** (still going) | killed; continuous HTTP 429 |
| last 3 hours | **60,000+** | killed; continuous HTTP 429 |
| last 24 hours (cap 30 pages) | 15,000 in 30 pages, **has_more=true** | rate-limited |

Even a **3-hour** window (what budget cron uses) returned 60k+ rows and rate-limited.

### 2. modifiedSince returns the full job history, not recent edits

24-hour window, rows returned spanned:

- **min transaction date: 2002-07-28**
- **max transaction date: 2026-07-20**
- Distinct dates: **3,941**, roughly even across **every month from 2009 → 2026**

That is a 24-year spread for a "modified in last 24h" query → `SyncTimestamp` is being bumped globally (bulk re-sync), so `modifiedSince` ≈ "give me everything."

### 3. But it DOES contain the dirty days

From the same 24h sample:

```
known_stale_seen: ["2026-07-15", "2026-07-17", "2026-07-20"]
recent_dates:     Jul 1,2,6,7,8,9,10,13,14,15,16,17,20
```

So coverage is real — the problem is precision and cost, not coverage.

---

## Why this matters for the budget module too

`budget_tracking/hcss_client.py → discover_modified_transaction_dates()` calls the **same** `jobCosts?modifiedSince` with **no date bounds** and paginates all rows:

```414:437:budget_tracking/hcss_client.py
        payload_base = {"modifiedSince": modified_since_str, "limit": 500}
        ...
        job_cost_rows = self._paginate_post(_JOB_COSTS_V1, dict(payload_base))
        _collect(job_cost_rows, "jobCosts")
```

If SyncTimestamp is globally bumped, budget's Layer B is also pulling the entire history every 3 hours (just discarding out-of-window dates after download). **Worth reviewing** — it likely works only because it still functions correctly, but it's expensive and 429-prone. (Not changed here.)

---

## The three discovery channels, summarized

| Channel | modifiedSince? | Result in this tenant |
|---|---|---|
| `costTypeQuantities/adjustments` | Yes | **0 rows** — tenant doesn't use manual web adjustments |
| `jobCosts/advancedRequest` | Yes | **Firehose** — whole history returned; contains dirty days but not precise |
| `costCode/progress/advancedRequest` | **No** | Value source; can only query by date range |

There is **no cheap, precise "what quantity changed since T" API** for this tenant.

---

## Recommended fix (given the evidence)

Precise dirty discovery isn't available, but we proved two things:

1. **Overwrite fixes values** (earlier experiment: refreshing Jul 15/17/20 → all 13 watchlist rows matched HCSS).
2. **Real drift clusters in recent days** — every stale day this incident was within the last **6 days**.

So the robust, cheap design:

### Primary: rolling lookback refresh
- Each cron run, re-fetch + overwrite the last **N days** (recommend **N = 30**, safely covers late postings/edits).
- Cost: `N × active_jobs` progress calls (e.g. 30 × ~45 ≈ 1,350) — same style you already run, no firehose.
- Would have caught Jul 15/17/20 automatically.

### Secondary (safety net for old edits): periodic full reconcile
- Weekly (or nightly off-peak) full re-populate of the season (May 1 → today) into the daily table.
- Catches the rare edit older than the lookback window.
- This is exactly what your `populate_quantity_test.py --overwrite` already does.

### Not recommended
- ❌ `jobCosts?modifiedSince` as a per-run dirty detector — firehose + 429 in this tenant.
- ❌ `adjustments?modifiedSince` — empty in this tenant.

---

## If you still want a modifiedSince-based detector later

Test whether **`modifiedSince` + `startDate`/`endDate` (transaction-date bounds)** meaningfully reduces volume, e.g. bound to the last 90 days. The schema allows combining them. But given SyncTimestamp is globally bumped, expect it to still return *all* transactions dated in that bound (just fewer because the date window is smaller) — i.e. equivalent to a lookback refresh anyway. Lookback is simpler and predictable.

---

## Net answer to "is there a way?"

- The data **is** exposed (jobCosts carries production quantity, progress computes it).
- What HCSS does **not** give this tenant is a **precise incremental change feed** — `modifiedSince` is effectively "everything," and `adjustments` is empty.
- Therefore the correct engineering answer is **schedule-based refresh (lookback + periodic full reconcile)** over the daily table, not change-detection. That is proven to reconcile with HCSS.

Artifacts:
- `reports/jobcosts_datespread_*.json` (24-year spread proof)
- Earlier: `reports/quantity_stale_day_refresh_20260721_194824.json` (overwrite → all match)
- Scripts: `scripts/test_quantity_jobcosts_dirty.py`, `scripts/probe_jobcosts_windows.py`, `scripts/probe_jobcosts_datespread.py`
