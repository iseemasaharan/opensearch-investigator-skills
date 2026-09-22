---
name: opensearch-anomaly-investigate
version: 1.1.0
description: Investigates an OpenSearch anomaly detection alert end-to-end. Fetches the anomaly result, correlates it with application logs and deploy events, then synthesizes a structured root cause report using an LLM flow agent. Use this skill when a production anomaly alert fires or when asked to investigate a latency or error-rate incident in OpenSearch.
license: Apache-2.0
compatibility: OpenSearch 2.12+, ml-commons plugin, anomaly-detection plugin
allowed-tools: Bash, mcp__opensearch, WebFetch
tags: [observability, incident-response, anomaly-detection, rca]
---

# opensearch-anomaly-investigate

Runs a full incident investigation pipeline against a live OpenSearch cluster when an anomaly alert fires. Calls five OpenSearch APIs in sequence — detector state, anomaly results, error logs, deploy events, log patterns — then posts all evidence to an LLM flow agent that returns a structured root cause report.

## When to use

- An alerting monitor has fired on a high anomaly grade (> 0.7)
- A user reports a production latency spike or error-rate increase
- You need a root cause report before rolling back a deployment
- The on-call engineer asks "what's causing this alert?"

## How it works

1. **Fetch detector state** — `GET /_plugins/_anomaly_detection/detectors/{detector_id}` confirms the detector name, feature, and thresholds
2. **Fetch anomaly results** — searches `.opendistro-anomaly-results*` for high-grade results (`anomaly_grade > 0.7`) in the last 10 minutes
3. **Correlate error logs** — searches `prod-logs` for ERROR/WARN entries in the anomaly window
4. **Correlate deploys** — searches `deploy-events` for any deploy in the 2 hours before the anomaly, flags same-service matches
5. **Find log patterns** — calls `LogPatternTool` to cluster recurring error messages
6. **Synthesize RCA** — posts all evidence to `/_plugins/_ml/agents/{agent_id}/_execute`; the LLM returns a markdown report with root cause verdict, ranked evidence, impact, recommended action, and confidence score

## Prerequisites

- OpenSearch cluster accessible at `$OPENSEARCH_URL` (default: `http://localhost:9200`)
- `DETECTOR_ID` and `AGENT_ID` available (written to `solution/.ids.env` by `setup_agent.sh`)
- Indices: `prod-metrics`, `prod-logs`, `deploy-events`
- ML Commons flow agent registered (see `solution/setup_agent.sh`)

## Inputs

| Parameter | Type | Required | Description |
|---|---|---|---|
| `detector_id` | string | yes | Anomaly detector ID |
| `agent_id` | string | yes | ML flow agent ID for the LLM synthesizer |
| `service` | string | no | Service name to filter logs (default: `payment-service`) |
| `window_minutes` | int | no | Lookback window for logs and anomaly results (default: `10`) |
| `opensearch_url` | string | no | Cluster endpoint (default: `$OPENSEARCH_URL` env var) |

## Usage

```bash
# Load IDs written by setup_agent.sh
source solution/.ids.env

# Run the full investigation pipeline (colored terminal output)
OPENSEARCH_URL=https://user:pass@host:9200 python solution/investigate.py

# Trigger via webhook (fires automatically when alerting monitor detects anomaly_grade > 0.7)
curl -X POST http://localhost:8765/trigger-investigation \
  -H 'Content-Type: application/json' \
  -d "{\"anomaly_grade\": 0.91, \"detector_id\": \"$DETECTOR_ID\", \"agent_id\": \"$AGENT_ID\"}"

# Execute the flow agent directly
curl -X POST "$OPENSEARCH_URL/_plugins/_ml/agents/$AGENT_ID/_execute" \
  -H 'Content-Type: application/json' \
  -d '{"parameters": {"question": "Investigate the current anomaly"}}'
```

## Output

A structured markdown incident report:

```markdown
## Root Cause
Deploy regression — payment-service v2.3.0 → v2.3.1 deployed 28 minutes before anomaly onset.

## Evidence (ranked by confidence)
1. Deploy by alice at T-28m changed connection pool config (suspicion: HIGH)
2. NullPointerException spike: 38 occurrences in last 10 min (0 in baseline)
3. P99 latency: 2444ms (normal: ~120ms) — 20× baseline

## Impact
- Affected service: payment-service
- P99 latency: 2444ms (normal: 120ms)
- Error rate: 12%
- Estimated blast radius: moderate

## Recommended Action
- **Immediate**: Rollback payment-service to v2.3.0
- **Verify**: Check connection pool exhaustion — `DB connection timeout` errors confirm pool max=10 exceeded
- **Confidence**: 91%

## Timeline
T-28m deploy v2.3.1 → T-5m latency creep → T-0 anomaly fires
```

## Implementation

`solution/investigate.py` — sequential 5-step Python chain, colored terminal output  
`solution/setup_agent.sh` — registers ML connector, model, anomaly detector, and flow agent  
`solution/mock_llm_server.py` — OpenAI-compatible mock LLM and webhook receiver (local dev)

Always-on production path:
```
log-generator → prod-metrics/prod-logs → anomaly detector
  → alerting monitor (grade > 0.7) → webhook → flow agent → RCA report
```
