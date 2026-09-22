---
name: opensearch-multi-signal-correlate
version: 1.1.0
description: Runs four detection signals in parallel — anomaly results, error logs, deploy events, and metric spikes — then cross-correlates them into a single confidence score and a ranked suspect list. Use this skill for a fast comprehensive incident assessment in one call instead of running four separate queries.
license: Apache-2.0
compatibility: OpenSearch 2.12+, anomaly-detection plugin
allowed-tools: Bash
tags: [incident-response, correlation, observability, confidence-scoring]
---

# opensearch-multi-signal-correlate

Fetches and correlates four signals simultaneously using `ThreadPoolExecutor(max_workers=4)`. Returns a unified confidence score (0–0.95), a ranked suspect list, and a human-readable summary — all in under 500ms. Use it as the first step in any automated investigation pipeline.

## When to use

- You want a single call that replaces running anomaly, log, deploy, and metric queries separately
- You need a confidence score before deciding whether to page the on-call engineer
- As the intake step of an automated investigation pipeline (replaces the first 4 tools of `opensearch-anomaly-investigate`)
- When you want to know "how sure are we that something is actually wrong?"

## How it works

Four queries are dispatched concurrently:

| Signal | Index | What it measures |
|---|---|---|
| `anomaly_results` | `.opendistro-anomaly-results*` | ML-detected anomalies with grade and score |
| `error_logs` | `prod-logs` | ERROR/WARN log volume and top exception messages |
| `deploy_events` | `deploy-events` | Recent deploys within the lookback window |
| `metric_spike` | `prod-metrics` | Max P99 latency and error rate |

**Confidence scoring (additive, capped at 0.95):**
```
+0.35  if any anomaly_grade > 0.7
+0.25  if error_rate > 5% OR exception count > 0
+0.25  if deploy exists in window for same service
+0.15  if max P99 > 500ms (2× typical baseline)
```

Suspects are ranked by individual contribution and labeled by type.

## Prerequisites

- OpenSearch cluster accessible at `$OPENSEARCH_URL` (default: `http://localhost:9200`)
- Indices: `prod-metrics`, `prod-logs`, `deploy-events`, `.opendistro-anomaly-results*`

## Inputs

| Parameter | Type | Required | Description |
|---|---|---|---|
| `service` | string | no | Service to filter all signals on (default: `payment-service`) |
| `window_minutes` | int | no | Lookback window in minutes (default: `10`) |
| `anomaly_detector_id` | string | no | Detector ID to scope anomaly result filtering |
| `opensearch_url` | string | no | Cluster endpoint (default: `$OPENSEARCH_URL` env var) |

## Usage

```bash
OPENSEARCH_URL=https://user:pass@host:9200 python solution/custom_tools/multi_signal_tool.py
```

```python
import os
os.environ["OPENSEARCH_URL"] = "https://user:pass@host:9200"
from solution.custom_tools.multi_signal_tool import MultiSignalCorrelatorTool

result = MultiSignalCorrelatorTool().execute({
    "service": "payment-service",
    "window_minutes": 10,
})

summary = result["correlation_summary"]
print(summary["confidence"])   # 0.9
print(summary["signals_fired"])  # ["anomaly", "errors", "deploy", "metric_spike"]

for suspect in summary["suspects"]:
    print(f'{suspect["type"]}: {suspect["value"]} (contribution: {suspect["contribution"]})')
```

## Output

```json
{
  "anomaly_results": {
    "count": 2,
    "max_grade": 0.91,
    "max_score": 0.84
  },
  "error_logs": {
    "total": 47,
    "errors": 38,
    "warnings": 9,
    "top_messages": ["NullPointerException in PaymentProcessor", "DB connection timeout"]
  },
  "deploy_events": {
    "count": 1,
    "latest": {
      "service": "payment-service",
      "version_from": "v2.3.0",
      "version_to": "v2.3.1",
      "deployed_by": "alice",
      "minutes_before_anomaly": 28
    }
  },
  "metric_spike": {
    "max_p99_latency_ms": 2444,
    "max_error_rate_pct": 12.3
  },
  "correlation_summary": {
    "confidence": 0.90,
    "signals_fired": ["anomaly", "errors", "deploy", "metric_spike"],
    "suspects": [
      { "type": "deploy",        "value": "v2.3.1 by alice (28m before anomaly)", "contribution": 0.90 },
      { "type": "latency_spike", "value": "2444ms P99",                           "contribution": 0.85 },
      { "type": "error_surge",   "value": "47 errors in 10min",                   "contribution": 0.60 }
    ]
  }
}
```

## Implementation

`solution/custom_tools/multi_signal_tool.py`

Four `pool.submit()` calls are dispatched simultaneously. Results are joined after all futures complete, then passed to `_correlate()` which computes the additive confidence score and builds the ranked suspect list. Total wall-clock time is bounded by the slowest single query, not the sum of all four.
