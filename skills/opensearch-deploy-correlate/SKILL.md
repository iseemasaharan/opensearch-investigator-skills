---
name: opensearch-deploy-correlate
version: 1.1.0
description: Scores recent deploy events by their likelihood of causing a production anomaly. Uses time proximity, service match, and change-risk keyword analysis to rank suspects. Use this skill first when investigating a production incident — deploys cause roughly 70% of outages and this runs in under 200ms with no ML inference required.
license: Apache-2.0
compatibility: OpenSearch 2.12+
allowed-tools: Bash
tags: [incident-response, deploy-correlation, rca, change-management]
---

# opensearch-deploy-correlate

Ranks recent deploy events by suspicion score relative to an anomaly timestamp. Single search query with client-side scoring — no ML inference, runs in under 200ms. Use it as the first step in any incident investigation to quickly answer "was there a deploy before this alert?"

## When to use

- Immediately after an anomaly alert fires, before reading logs
- When you need to answer "was there a deploy before this incident?"
- As a fast pre-screen to decide whether to rollback before escalating
- Any time a latency or error-rate anomaly fires on a service that was recently deployed

## How it works

Searches `deploy-events` for all deploys in the lookback window, then scores each one:

```
suspicion = service_match(0–0.40)
          + time_proximity(0–0.40)   # linear decay; 0 at Δt > 2h; 0 if deploy AFTER anomaly
          + change_risk(0–0.20)      # keyword scan on the changes field
```

**Change-risk keywords and weights:**
`pool(0.20)`, `connection(0.20)`, `timeout(0.15)`, `migration(0.15)`, `refactor(0.10)`, `upgrade(0.07)`, `new(0.08)`, `dependency(0.08)`

| Score | Verdict |
|---|---|
| ≥ 0.75 | HIGH — likely root cause |
| ≥ 0.50 | MEDIUM — investigate further |
| ≥ 0.25 | LOW — possible contributor |
| < 0.25 | UNLIKELY |

## Prerequisites

- OpenSearch cluster accessible at `$OPENSEARCH_URL` (default: `http://localhost:9200`)
- `deploy-events` index populated (written by `solution/seed_data.py`)

## Inputs

| Parameter | Type | Required | Description |
|---|---|---|---|
| `service` | string | no | Service to match against deploy records (default: `payment-service`) |
| `anomaly_time` | ISO8601 string | no | Anomaly timestamp to score proximity against (default: now) |
| `lookback_minutes` | int | no | How far back to search for deploys (default: `120`) |
| `opensearch_url` | string | no | Cluster endpoint (default: `$OPENSEARCH_URL` env var) |

## Usage

```bash
# Run standalone — reads OPENSEARCH_URL from environment
OPENSEARCH_URL=https://user:pass@host:9200 python solution/custom_tools/deploy_correlator_tool.py
```

```python
import os
os.environ["OPENSEARCH_URL"] = "https://user:pass@host:9200"
from solution.custom_tools.deploy_correlator_tool import DeployCorrelatorTool

result = DeployCorrelatorTool().execute({
    "service": "payment-service",
    "anomaly_time": "2026-09-18T14:30:00Z",
    "lookback_minutes": 120,
})
print(result["top_suspect"]["verdict"])           # "HIGH — likely root cause"
print(result["top_suspect"]["deploy"]["version_to"])  # "v2.3.1"
print(result["top_suspect"]["reasons"])
```

## Output

```json
{
  "anomaly_time": "2026-09-18T14:30:00Z",
  "service": "payment-service",
  "total_deploys": 3,
  "top_suspect": {
    "deploy": {
      "service": "payment-service",
      "version_from": "v2.3.0",
      "version_to": "v2.3.1",
      "deployed_by": "alice",
      "changes": "refactored connection pool config"
    },
    "suspicion": 0.89,
    "delta": "28m before anomaly",
    "reasons": [
      "same service (payment-service)",
      "deployed 28m before anomaly",
      "changes mention 'connection', 'pool'"
    ],
    "verdict": "HIGH — likely root cause"
  },
  "all_suspects": [
    { "deploy": { "service": "payment-service", "version_to": "v2.3.1" }, "suspicion": 0.89, "verdict": "HIGH — likely root cause" },
    { "deploy": { "service": "order-service",   "version_to": "v1.7.2" }, "suspicion": 0.12, "verdict": "UNLIKELY" }
  ]
}
```

## Implementation

`solution/custom_tools/deploy_correlator_tool.py`

Executes one `match_all` + `range` query against `deploy-events`, then scores results in Python. No second query needed — all data comes back in the first response.
