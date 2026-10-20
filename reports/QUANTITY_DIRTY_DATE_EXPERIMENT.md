# Quantity Dirty-Date Experiment Report
**Run date:** 2026-07-21  
**Comparison window:** 2026-05-01 → 2026-07-20  
**Table tested:** `quantity_test` only (production untouched)

---

## Executive verdict

| Question | Result |
|---|---|
| Does `adjustments?modifiedSince` find dirty dates? | **FAIL — 0 rows** (all probes) |
| If we refresh the correct stale days, do gaps close? | **PASS — all 13 watchlist rows matched HCSS** |
| Are missing calendar days the cause? | **No — 81/81 days present** |

**Bottom line:** Daily overwrite works. Quantity dirty discovery via the adjustments API does **not** work for this HCSS tenant (no quantity-adjustment records). Fresh populate matching yesterday only proved math; overnight drift was late edits on **Jul 15 / 17 / 20** that were never rewritten.

---

## Part 1 — What you reported

Yesterday: quantity-test API matched HCSS.  
Today (same range May 1 → Jul 20): 13 discrepancies, all HCSS > Dashboard (undercount).

Largest gap: **25006 / 80711111** = +1,300 TNS.

---

## Part 2 — `modifiedSince` / adjustments API probes

Endpoint: `GET /v1/costTypeQuantities/adjustments`

| Probe | Params | Rows returned |
|---|---|---|
| A | `modifiedSince=now-72h` | **0** |
| B | `modifiedSince=2026-07-07` | **0** |
| C | `modifiedSince=2026-07-01` | **0** |
| D | `modifiedSince=2026-05-01` | **0** |
| E | `startDate=2026-05-01&endDate=2026-07-20` | **0** |
| F | `startDate=2026-07-14&endDate=2026-07-20` | **0** |

HTTP status was **200** every time — not an auth/error failure. This tenant simply has **no quantity adjustment records** in that API for the period.

### Implication

Budget dirty sync uses cost endpoints (`jobCosts` / `materialInstalled` / `subcontractWork`) that actually return edits.  
Quantity’s analogous discovery channel (`costTypeQuantities/adjustments`) returns nothing here.

So a cron Layer B copied from budget **would not have refreshed any of these 13 gaps**.

Artifacts:
- `reports/quantity_dirty_refresh_20260721_191738.json`
- `reports/adj_probe_*.json`

---

## Part 3 — DB coverage check

Before any refresh:

- Days requested: **81**
- Days present in `quantity_test`: **81**
- Missing days: **none**

Watchlist snapshot matched your spreadsheet exactly (e.g. 80711111 DB=25257, gap=1300).

So the problem is **stale day contents**, not missing days.

---

## Part 4 — Finding the real stale days (day-by-day progress)

Because adjustments gave 0 dates, we scanned live HCSS `progress(day,day)` for the 5 watchlist jobs vs `quantity_test` for all 81 days.

### Dirty dates found

1. **2026-07-15**
2. **2026-07-17**
3. **2026-07-20**

### Cell-level deltas (HCSS day − DB day)

| Date | Job | Cost code | HCSS day | DB day | Delta |
|---|---|---|---:|---:|---:|
| 2026-07-20 | 25006 | 80711111 | 1300.0 | 0.0 | +1300.0 |
| 2026-07-20 | 25006 | 80011111B | 194.0 | 0.0 | +194.0 |
| 2026-07-17 | 25006 | 58011111D | 35.0 | 0.0 | +35.0 |
| 2026-07-17 | 25069C | 14511111C | 127.65 | 107.3 | +20.35 |
| 2026-07-15 | 25060CONST | 40011111 | 17.0 | 0.0 | +17.0 |
| 2026-07-17 | 26004 | 61340010A | 14.5 | 3.0 | +11.5 |
| 2026-07-20 | 26004 | 61340010A | 5.0 | 0.0 | +5.0 |
| 2026-07-15 | 25060CONST | 24011111A | 3.0 | 0.0 | +3.0 |
| 2026-07-15 | 25048 | 12011111C | 1.0 | 0.0 | +1.0 |
| 2026-07-17 | 25006 | 58511111C | 0.25 | 0.0 | +0.25 |
| 2026-07-20 | 25006 | 5111111 | 0.15 | 0.0 | +0.15 |
| 2026-07-15 | 25060CONST | 211111I | 0.1 | 0.0 | +0.1 |
| 2026-07-20 | 25006 | 60011111B | 0.05 | 0.0 | +0.05 |

These three dates alone explain **all** of your reported period gaps.

Pattern: mostly **DB had 0 for that day, HCSS later had quantity** (posted/edited after your populate).

---

## Part 5 — Refresh those 3 days

Rewrote full active-job daily snapshots into `quantity_test` for:

- 2026-07-15 → 3521 rows  
- 2026-07-17 → 3521 rows  
- 2026-07-20 → 3521 rows  

### Watchlist after refresh vs your HCSS Actual

| Job | Cost code | Before DB | After DB | HCSS | Gap after | Status |
|---|---|---:|---:|---:|---:|---|
| 25006 | 80711111 | 25257.0 | 26557.0 | 26557.0 | 0.00 | MATCH |
| 25006 | 80011111B | 11132.0 | 11326.0 | 11326.0 | 0.00 | MATCH |
| 25006 | 58011111D | 565.0 | 600.0 | 600.0 | 0.00 | MATCH |
| 25069C | 14511111C | 545.55 | 565.9 | 565.9 | 0.00 | MATCH |
| 26004 | 61340010A | 3.0 | 19.5 | 19.5 | 0.00 | MATCH |
| 25060CONST | 40011111 | 34.0 | 51.0 | 51.0 | 0.00 | MATCH |
| 25060CONST | 24011111A | 182.97 | 185.97 | 185.97 | 0.00 | MATCH |
| 25048 | 12011111C | 4.06 | 5.06 | 5.06 | 0.00 | MATCH |
| 25006 | 58511111C | 7.7 | 7.95 | 7.95 | 0.00 | MATCH |
| 25006 | 5111111 | 9.09 | 9.24 | 9.24 | 0.00 | MATCH |
| 25060CONST | 211111I | 7.61 | 7.71 | 7.71 | 0.00 | MATCH |
| 25006 | 60011111B | 0.0 | 0.05 | 0.05 | 0.00 | MATCH |
| 25006 | 1011111 | 2.36 | 2.37 | 2.38 | 0.01 | MATCH (tol) |

**13/13 matched** (one within 0.01 rounding).

Artifact: `reports/quantity_stale_day_refresh_20260721_194824.json`

---

## What this means for real production logic

### Proven
1. **Daily table + sum-on-read is correct** when days are fresh.  
2. **Overwrite of a known dirty day fixes drift** (same idea as budget).  
3. Your overnight discrepancies were **late edits on Jul 15/17/20**, not a broken sum formula.

### Not proven / failed for this tenant
4. **`costTypeQuantities/adjustments?modifiedSince` cannot be the dirty-date source** — it returns empty even for the full May–Jul window.

### Practical cron design implication

| Layer | Quantity recommendation |
|---|---|
| Daily rows + sum on read | Keep (validated) |
| Lookback (e.g. last 7 days) | **Required** — would have caught Jul 15/17/20 if rewritten on a schedule |
| Dirty via adjustments API | **Do not rely on** for this company (0 data) |
| Older-than-lookback edits | Still unsolved without another signal or periodic reconcile |

Today is Jul 21; the stale days were Jul 15–20 — inside a 7-day lookback.  
So for *this* incident, **scheduled lookback refresh alone would have been enough**. Adjustments dirty layer would not have helped anyway.

---

## Your next check

Re-hit:

```http
GET /api/quantity-test/verify?start_date=2026-05-01&end_date=2026-07-20
```

and compare to HCSS again — watchlist should now match.

Scripts added for re-runs:
- `scripts/test_quantity_dirty_refresh.py` — adjustments discovery + refresh  
- `scripts/probe_quantity_adjustments.py` — multi-window adjustments probes  
- `scripts/find_and_refresh_stale_quantity_days.py` — day-diff discovery when adjustments is empty  
