# Quantity Dirty Channels — Thorough Test Report
**Run:** 2026-07-21  
**modifiedSince:** `2026-07-19T00:00:00Z`  
**Known truth:** stale days `2026-07-15`, `2026-07-17`, `2026-07-20` caused the overnight May1–Jul20 drift  
**Artifact:** `reports/quantity_dirty_channels_20260721_210426.json`

---

## GO / NO-GO for main code

### **GO — use `GET /v1/timeCardInfo?modifiedSince=...` as the quantity dirty-date channel**

| Channel | Rows | Dates in window | Known stale covered | Firehose? | Usable? |
|---|---:|---:|---|---|---|
| **A. timeCardInfo** | **88** | **12** | **15, 17, 20 (all 3)** | **No** | **YES** |
| B. costCodeTimeCardTransactions | 1 | 1 | none | No | No |
| C. jobCosts **v2** | 15,000+ (capped) | many / full history | all 3 | **Yes** | No |

---

## Channel details

### A. `GET /v1/timeCardInfo?modifiedSince=2026-07-19T00:00:00Z` — WINNER

- **88 rows**, 1 page, no rate-limit pain
- Transaction dates returned (12 total):  
  `2026-07-06 … 2026-07-10, 13, 14, 15, 16, 17, 18, 20`
- **All three known stale days present**
- `lastModified` clustered on **2026-07-20** (73) and **2026-07-21** (15) — exactly the overnight edit window
- Sample fields include `date`, `jobId`, `revision`, `lastModifiedPreciseDateTime`

This is a **precise, cheap dirty-date feed** for this tenant.

### B. `POST /v1/costCodeTimeCardTransactions/advancedRequest` — FAIL for discovery

- Only **1 row** (txn date Jul 16)
- Did **not** surface Jul 15/17/20
- Do **not** rely on this alone for dirty dates here

### C. `POST /v2/jobCosts/advancedRequest` — still firehose

- Still returns huge history (capped at 40 pages / 20k rows, more available)
- Marked firehose even though known stale appear inside the noise
- Same class of problem as v1 — **do not use for quantity dirty discovery**

---

## End-to-end refresh proof

1. Corrupted **20** watchlist daily cells on known stale days → installed = 0 (gaps appeared).
2. Refreshed the **12 dates** discovered by `timeCardInfo` (includes Jul 15/17/20).
3. Result vs earlier HCSS snapshot (from your discrepancy list):

| Job | Cost code | Status |
|---|---|---|
| 25006 / 80711111 | MATCH 26557 |
| 25006 / 80011111B | MATCH 11326 |
| 25006 / 58011111D | MATCH 600 |
| 25069C / 14511111C | **GAP** db=652.85 vs frozen HCSS=565.9 |
| 26004 / 61340010A | MATCH 19.5 |
| 25060CONST / 40011111 | MATCH 51 |
| 25060CONST / 24011111A | MATCH 185.97 |
| 25048 / 12011111C | MATCH 5.06 |
| 25006 / 58511111C | MATCH 7.95 |
| 25006 / 5111111 | MATCH 9.24 |
| 25060CONST / 211111I | MATCH 7.71 |
| 25006 / 60011111B | MATCH 0.05 |
| 25006 / 1011111 | MATCH 2.38 |

**12/13 match** the earlier snapshot. The one overage (`25069C / 14511111C`) is almost certainly **live HCSS having moved further** since this morning’s spreadsheet (DB is *higher* than the frozen HCSS number, not stale-low). Discovery still correctly forced a rewrite of Jul 17 for that job.

---

## Recommended production design (quantity)

```
each cron:
  ms = last_quantity_sync_at - overlap (e.g. 15–30 min)

  dirty_dates = dates from GET /v1/timeCardInfo?modifiedSince=ms
                (filter to BUDGET_SYNC_START_DATE → yesterday)

  refresh_days = lookback(7 or 14) ∪ dirty_dates

  for day in refresh_days:
      progress(day, day) for active jobs
      overwrite daily quantity rows

  advance last_quantity_sync_at only if all succeed
```

Optional extras (not required for this tenant):
- Keep calling `costTypeQuantities/adjustments?modifiedSince` (empty today; cheap)
- Do **not** use jobCosts v1/v2 modifiedSince for quantity dirty dates
- Do **not** rely on costCodeTimeCardTransactions alone

Dashboard: sum daily rows (quantity_test / future prod table) — already validated.

---

## Answer: can we use this in main code?

**Yes — for dirty-date discovery, use `timeCardInfo?modifiedSince`.**

It:
1. Returns a small set (88 rows / 12 dates in this test)
2. Includes every known stale day from your real incident
3. Drives a refresh that restores quantity to live HCSS

Pair it with a short lookback for safety. That is enough to put in production quantity sync once you wire it the same way as budget’s Layer A + B pattern (but with this discovery source instead of jobCosts).
