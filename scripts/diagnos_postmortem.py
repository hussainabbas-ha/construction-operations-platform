# diagnose_job_25006.py

import json
import sys
import os
from collections import defaultdict

# Ensure project root is on sys.path when run directly as a script
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from payroll_verification.hcss_client import HCSSClient

client = HCSSClient()

BASE = "https://api.hcssapps.com/heavyjob/api/v1"
JOB_CODE = "25006"


# ==============================================================================
# FIND JOB
# ==============================================================================

def find_job_id():
    jobs = client._request(
        "GET",
        f"{BASE}/jobs",
        params={"limit": 500},
    )

    rows = jobs if isinstance(jobs, list) else jobs.get("results", [])

    for job in rows:
        code = str(job.get("code") or job.get("jobCode") or "")
        if code == JOB_CODE:
            return job.get("id")

    raise Exception(f"Job {JOB_CODE} not found")


job_id = find_job_id()

print("=" * 100)
print("JOB ID:", job_id)


# ==============================================================================
# COST CODES (BUDGET SIDE)
# ==============================================================================

costcodes_response = client._request(
    "GET",
    f"{BASE}/costCodes",
    params={"jobId": job_id, "limit": 500},
)

costcodes = (
    costcodes_response
    if isinstance(costcodes_response, list)
    else costcodes_response.get("results", [])
)

print("\n" + "=" * 100)
print("COST CODE METADATA")
print("=" * 100)

print("Returned Cost Codes:", len(costcodes))


# ==============================================================================
# BUDGET CALCULATION + MISSING DETECTION
# ==============================================================================

labor = equip = material = sub = supply = custom = 0

missing_estimate_link = 0
estimate_linked_costcodes = 0

zero_budget_costcodes = 0

for cc in costcodes:

    labor += float(cc.get("laborDollars") or 0)
    equip += float(cc.get("equipmentDollars") or 0)
    material += float(cc.get("materialDollars") or 0)
    sub += float(cc.get("subcontractDollars") or 0)
    supply += float(cc.get("supplyDollars") or 0)

    custom_arr = cc.get("customCostTypeDollars") or []
    if isinstance(custom_arr, list):
        custom += sum(float(x.get("dollars") or 0) for x in custom_arr if isinstance(x, dict))

    # ==========================================================
    # IMPORTANT NEW DEBUG LOGIC
    # ==========================================================

    if not cc.get("heavyBidEstimateCode"):
        missing_estimate_link += 1
    else:
        estimate_linked_costcodes += 1

    if (
        cc.get("laborDollars", 0) == 0
        and cc.get("equipmentDollars", 0) == 0
        and cc.get("materialDollars", 0) == 0
        and cc.get("subcontractDollars", 0) == 0
    ):
        zero_budget_costcodes += 1


budget_total = labor + equip + material + sub + supply + custom

print("\n" + "=" * 100)
print("COST CODE BUDGET TOTALS")
print("=" * 100)

print(f"Labor Budget       : ${labor:,.2f}")
print(f"Equipment Budget   : ${equip:,.2f}")
print(f"Material Budget    : ${material:,.2f}")
print(f"Subcontract Budget : ${sub:,.2f}")
print(f"Supply Budget      : ${supply:,.2f}")
print(f"Custom Budget      : ${custom:,.2f}")

print("-" * 100)
print(f"API TOTAL BUDGET   : ${budget_total:,.2f}")

print("\n" + "=" * 100)
print("BUDGET QUALITY CHECK")
print("=" * 100)

print("Estimate-linked cost codes:", estimate_linked_costcodes)
print("Missing estimate link:", missing_estimate_link)
print("Zero-budget cost codes:", zero_budget_costcodes)


# ==============================================================================
# SHOW SAMPLE COST CODE (FULL TRACE)
# ==============================================================================

print("\n" + "=" * 100)
print("SAMPLE COST CODE TRACE")
print("=" * 100)

for i, cc in enumerate(costcodes[:3], start=1):
    print(f"\nCOST CODE #{i}")
    print(json.dumps(cc, indent=2, default=str))


# ==============================================================================
# JOB ADVANCED (NO BUDGET HERE)
# ==============================================================================

print("\n" + "=" * 100)
print("JOB ADVANCED RAW")
print("=" * 100)

advanced = client._request(
    "POST",
    f"{BASE}/jobs/advanced",
    json={"jobIds": [job_id]},
)

job = (advanced.get("results", [{}])[0] if isinstance(advanced, dict) else advanced[0])

print(json.dumps(job, indent=2, default=str))


# ==============================================================================
# ACTUAL COSTS
# ==============================================================================

actual_response = client._request(
    "POST",
    f"{BASE}/jobCostsToDate/search",
    json={"jobIds": [job_id], "limit": 500},
)

actual_rows = (
    actual_response
    if isinstance(actual_response, list)
    else actual_response.get("results", [])
)

print("\n" + "=" * 100)
print("ACTUAL COST RESPONSE INFO")
print("=" * 100)

print("Rows Returned:", len(actual_rows))


labor_actual = equip_actual = material_actual = sub_actual = truck_actual = 0

zero_cost_rows = 0

for row in actual_rows:

    l = float(row.get("totalLaborCost") or 0)
    e = float(row.get("totalEquipmentCost") or 0)
    m = float(row.get("totalMaterialCost") or 0)
    s = float(row.get("totalSubcontractCost") or 0)
    t = float(row.get("totalTruckingCost") or 0)

    labor_actual += l
    equip_actual += e
    material_actual += m
    sub_actual += s
    truck_actual += t

    if l == 0 and e == 0 and m == 0 and s == 0 and t == 0:
        zero_cost_rows += 1


actual_total = labor_actual + equip_actual + material_actual + sub_actual + truck_actual


print("\n" + "=" * 100)
print("ACTUAL COST TOTALS")
print("=" * 100)

print(f"Labor Actual       : ${labor_actual:,.2f}")
print(f"Equipment Actual   : ${equip_actual:,.2f}")
print(f"Material Actual    : ${material_actual:,.2f}")
print(f"Subcontract Actual : ${sub_actual:,.2f}")
print(f"Trucking Actual    : ${truck_actual:,.2f}")
print("-" * 100)
print(f"API TOTAL ACTUAL   : ${actual_total:,.2f}")

print("\nZero-cost rows detected:", zero_cost_rows)


# ==============================================================================
# FINAL DIAGNOSTIC SUMMARY (THIS IS KEY)
# ==============================================================================

print("\n" + "=" * 100)
print("FINAL DIAGNOSTIC SUMMARY")
print("=" * 100)

print(f"Cost Code Budget Total : ${budget_total:,.2f}")
print(f"Actual Cost Total      : ${actual_total:,.2f}")

print(f"Budget vs Actual Gap   : ${budget_total - actual_total:,.2f}")

print("\nNOTE:")
print("- If UI shows different budget than this → estimate layer exists outside costCodes")
print("- If gap ≈ 606K → missing estimate-level adjustments (heavyBid / job summary layer)")
print("- If estimate-linked cost codes < 100% → budget is partially external")
print("testt")