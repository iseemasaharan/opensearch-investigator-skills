---
name: opensearch-baseline-compare
version: 1.1.0
description: Compares current metric values against the same time window N days ago to catch slow regressions that the adaptive ML anomaly detector has learned to ignore. Use this skill when performance feels degraded but no anomaly alert has fired, or for weekly regression checks after deployments.
license: Apache-2.0
compatibility: OpenSearch 2.12+
allowed-tools: Bash
tags: [observability, regression-detection, slo, baseline]
---

# opensearch-baseline-compare

Detects slow regressions that OpenSearch's adaptive anomaly detector misses because it learned the degraded state as normal. Runs two aggregation queries in parallel and returns deviation percentage, direction, and a breach verdict — all in under 200ms.

## When to use

- Performance degraded gradually over days/weeks without triggering an anomaly alert
- After a deploy that changed latency by 20–50% (below the anomaly detector's threshold)
- SLO burn-rate check: "is error rate higher than last Tuesday at this time?"
- Capacity planning: "is throughput lower than last week?"
- As a fast pre-screen before running the full investigation pipeline

## How it works

Fetches two time windows simultaneously using `ThreadPoolExecutor`:

1. **Current window** — last `window_minutes` ending now
2. **Baseline window** — same `window_minutes` from `baseline_days` ago (same weekday, same hour)

Computes:
```
avg_deviation_pct = (current_avg - baseline_avg) / baseline_avg * 100
```

Fires a breach if `|avg_deviation_pct| >= breach_threshold_pct` (default: 50%).

Both windows return `avg`, `max`, and `percentiles` aggregations. The breach verdict uses `avg`; `p95` is included for SLO analysis.

## Prerequisites

- OpenSearch cluster accessible at `$OPENSEARCH_URL` (default: `http://localhost:9200`)
- At least `baseline_days` of historical data in the target index
- Index: `prod-metrics` (or any time-series index with a `@timestamp` field)

## Inputs

| Parameter | Type | Required | Description |
|---|---|---|---|
| `index` | string | no | Target index (default: `prod-metrics`) |
| `metric_field` | string | no | Numeric field to compare (default: `p99_latency_ms`) |
| `service` | string | no | Service to filter on (default: `payment-service`) |
| `window_minutes` | int | no | Window size in minutes (default: `10`) |
| `baseline_days` | int | no | Days back for baseline (default: `7`) |
| `breach_threshold_pct` | float | no | Deviation % that triggers a breach (default: `50`) |
| `opensearch_url` | string | no | Cluster endpoint (default: `$OPENSEARCH_URL` env var) |

## Usage

```bash
OPENSEARCH_URL=https://user:pass@host:9200 python solution/custom_tools/baseline_comparator_tool.py
```

```python
import os
os.environ["OPENSEARCH_URL"] = "https://user:pass@host:9200"
from solution.custom_tools.baseline_comparator_tool import BaselineComparisonTool

result = BaselineComparisonTool().execute({
    "service": "payment-service",
    "metric_field": "p99_latency_ms",
    "window_minutes": 10,
    "breach_threshold_pct": 50,
})
print(result["verdict"])            # "CRITICAL — p99_latency_ms degraded 860% vs 7d ago"
print(result["avg_deviation_pct"])  # 860.4
print(result["breached"])           # True
```

## Output

```json
{
  "metric": "p99_latency_ms",
  "service": "payment-service",
  "window_minutes": 10,
  "baseline_days": 7,
  "current":  { "avg": 1152.52, "max": 2800.0, "p95": 2400.0, "samples": 48 },
  "baseline": { "avg": 120.0,   "max": 180.0,  "p95": 150.0,  "samples": 51 },
  "avg_deviation_pct": 860.4,
  "max_deviation_pct": 1455.6,
  "breach_threshold": 50.0,
  "breached": true,
  "direction": "degraded",
  "verdict": "CRITICAL — p99_latency_ms degraded 860% vs 7d ago"
}
```

## Implementation

`solution/custom_tools/baseline_comparator_tool.py`

Two `date_histogram` aggregations are submitted concurrently via `ThreadPoolExecutor`. Each returns `avg`, `max`, and `percentiles` (p95) for the target metric, filtered by service using a `term` query. Results are joined and deviation is computed in Python after both futures complete.
