# Database Statistics API

## Overview

The `/api/db/stats` endpoint provides comprehensive statistics about all tracking modules in the database.

## Endpoint

```
GET /api/db/stats
```

## Response Structure

```json
{
  "generated_at": "2026-05-06T20:30:00Z",
  
  "timelog_verification": {
    "total_cache_entries": 45,
    "total_detail_records": 1250,
    "date_range": {
      "oldest": "2026-03-01",
      "newest": "2026-05-05",
      "days_covered": 45
    },
    "status_breakdown": {
      "approved": 1100,
      "flagged": 100,
      "rejected": 50,
      "total_timecards": 1250
    },
    "recent_activity": [
      {
        "date": "2026-05-05",
        "total": 28,
        "approved": 25,
        "flagged": 2,
        "rejected": 1,
        "updated_at": "2026-05-05T18:00:00Z"
      }
    ]
  },
  
  "quantity_tracking": {
    "total_cache_entries": 45,
    "total_detail_records": 850,
    "date_range": {
      "oldest": "2026-03-01",
      "newest": "2026-05-05",
      "days_covered": 45
    },
    "status_breakdown": {
      "on_track": 700,
      "near_completion": 100,
      "over_risk": 50,
      "total_cost_codes": 850
    },
    "cost_type_breakdown": {
      "self_perform": 600,
      "subcontractor": 250
    },
    "recent_activity": [
      {
        "date": "2026-05-05",
        "total": 20,
        "on_track": 15,
        "near_completion": 3,
        "over_risk": 2,
        "updated_at": "2026-05-05T18:00:00Z"
      }
    ]
  },
  
  "budget_tracking": {
    "total_cache_entries": 45,
    "total_detail_records": 900,
    "date_range": {
      "oldest": "2026-03-01",
      "newest": "2026-05-05",
      "days_covered": 45
    },
    "status_breakdown": {
      "on_track": 750,
      "over_risk": 150,
      "total_cost_codes": 900
    },
    "financial_summary": {
      "total_expected_budget": 5000000.00,
      "total_actual_cost": 4200000.00,
      "total_variance": -800000.00,
      "overall_utilization_percentage": 84
    },
    "recent_activity": [
      {
        "date": "2026-05-05",
        "total": 22,
        "on_track": 18,
        "over_risk": 4,
        "updated_at": "2026-05-05T18:00:00Z"
      }
    ]
  },
  
  "cron_jobs": {
    "total_executions": 150,
    "status_breakdown": {
      "success": 140,
      "failed": 8,
      "retrying": 2
    },
    "job_type_breakdown": {
      "timelog": 50,
      "quantity": 50,
      "budget": 50
    },
    "recent_executions": [
      {
        "job_type": "timelog",
        "execution_date": "2026-05-05",
        "status": "success",
        "records_processed": 28,
        "attempt_count": 1,
        "created_at": "2026-05-05T18:00:00Z",
        "error_message": null
      }
    ],
    "failed_jobs": [
      {
        "job_type": "budget",
        "execution_date": "2026-04-15",
        "status": "retrying",
        "attempt_count": 2,
        "max_retries": 3,
        "error_message": "HCSS API timeout",
        "next_retry_at": "2026-05-06T20:30:00Z"
      }
    ]
  },
  
  "summary": {
    "total_cache_entries": 135,
    "total_detail_records": 3000,
    "total_days_cached": 135,
    "modules_active": 3
  }
}
```

## Usage Examples

### cURL

```bash
# Get all database statistics
curl http://localhost:8000/api/db/stats

# Pretty print with jq
curl http://localhost:8000/api/db/stats | jq .

# Get only timelog stats
curl http://localhost:8000/api/db/stats | jq .timelog_verification

# Get only summary
curl http://localhost:8000/api/db/stats | jq .summary

# Check financial summary
curl http://localhost:8000/api/db/stats | jq .budget_tracking.financial_summary
```

### Python

```python
import requests

response = requests.get("http://localhost:8000/api/db/stats")
stats = response.json()

print(f"Total days cached: {stats['summary']['total_days_cached']}")
print(f"Total records: {stats['summary']['total_detail_records']}")
print(f"Active modules: {stats['summary']['modules_active']}")

# Check timelog coverage
timelog = stats['timelog_verification']
print(f"Timelog: {timelog['date_range']['oldest']} to {timelog['date_range']['newest']}")
print(f"Total timecards: {timelog['status_breakdown']['total_timecards']}")

# Check budget utilization
budget = stats['budget_tracking']
financial = budget['financial_summary']
print(f"Budget utilization: {financial['overall_utilization_percentage']}%")
print(f"Total variance: ${financial['total_variance']:,.2f}")
```

### JavaScript

```javascript
fetch('http://localhost:8000/api/db/stats')
  .then(response => response.json())
  .then(stats => {
    console.log('Summary:', stats.summary);
    console.log('Timelog:', stats.timelog_verification);
    console.log('Quantity:', stats.quantity_tracking);
    console.log('Budget:', stats.budget_tracking);
    console.log('Cron Jobs:', stats.cron_jobs);
  });
```

## Response Fields

### Timelog Verification

- **total_cache_entries**: Number of days with cached data
- **total_detail_records**: Total number of individual timecard records
- **date_range**: Oldest and newest dates with data
- **status_breakdown**: Counts of approved, flagged, and rejected timecards
- **recent_activity**: Last 5 days of activity

### Quantity Tracking

- **total_cache_entries**: Number of days with cached data
- **total_detail_records**: Total number of cost code quantity records
- **date_range**: Oldest and newest dates with data
- **status_breakdown**: Counts by status (on_track, near_completion, over_risk)
- **cost_type_breakdown**: Counts by cost type (self_perform, subcontractor)
- **recent_activity**: Last 5 days of activity

### Budget Tracking

- **total_cache_entries**: Number of days with cached data
- **total_detail_records**: Total number of cost code budget records
- **date_range**: Oldest and newest dates with data
- **status_breakdown**: Counts by status (on_track, over_risk)
- **financial_summary**: Total budget, actual cost, variance, and utilization percentage
- **recent_activity**: Last 5 days of activity

### Cron Jobs

- **total_executions**: Total number of cron job executions
- **status_breakdown**: Counts by status (success, failed, retrying)
- **job_type_breakdown**: Counts by job type (timelog, quantity, budget)
- **recent_executions**: Last 10 executions
- **failed_jobs**: Jobs that failed or are retrying (up to 5)

### Summary

- **total_cache_entries**: Total cache entries across all modules
- **total_detail_records**: Total detail records across all modules
- **total_days_cached**: Total days with cached data
- **modules_active**: Number of modules with data (0-3)

## Use Cases

### 1. Monitor Data Coverage

Check if data is being synced properly:

```bash
curl http://localhost:8000/api/db/stats | jq '.summary'
```

### 2. Check Date Ranges

Verify which dates have data:

```bash
curl http://localhost:8000/api/db/stats | jq '.timelog_verification.date_range'
```

### 3. Monitor Budget Health

Check overall budget utilization:

```bash
curl http://localhost:8000/api/db/stats | jq '.budget_tracking.financial_summary'
```

### 4. Check Cron Job Health

Monitor automatic sync jobs:

```bash
curl http://localhost:8000/api/db/stats | jq '.cron_jobs.status_breakdown'
```

### 5. Identify Failed Jobs

Find jobs that need attention:

```bash
curl http://localhost:8000/api/db/stats | jq '.cron_jobs.failed_jobs'
```

## Monitoring Dashboard

You can use this endpoint to build a monitoring dashboard:

```javascript
// Refresh stats every 30 seconds
setInterval(async () => {
  const stats = await fetch('/api/db/stats').then(r => r.json());
  
  // Update UI
  document.getElementById('total-days').textContent = stats.summary.total_days_cached;
  document.getElementById('total-records').textContent = stats.summary.total_detail_records;
  document.getElementById('modules-active').textContent = stats.summary.modules_active;
  
  // Show warnings for failed jobs
  if (stats.cron_jobs.failed_jobs.length > 0) {
    showWarning('Some cron jobs have failed');
  }
}, 30000);
```

## Migration from Old Endpoint

The old `/api/cache/stats` endpoint is deprecated. Use `/api/db/stats` instead:

**Old (Deprecated):**
```bash
curl http://localhost:8000/api/cache/stats
```

**New (Recommended):**
```bash
curl http://localhost:8000/api/db/stats
```

The new endpoint provides much more comprehensive information across all modules.

## Performance

- **Response time**: < 500ms for typical database sizes
- **Caching**: Not cached (always returns current data)
- **Database queries**: ~15 queries (optimized with aggregations)

## Error Handling

If any module fails to retrieve stats, it will return an error object:

```json
{
  "timelog_verification": {
    "error": "Database connection failed",
    "total_cache_entries": 0,
    "total_detail_records": 0
  }
}
```

The endpoint will still return data for other modules that succeeded.
