---
name: opensearch-bedrock-investigate
version: 1.0.0
description: Runs a full AI-powered incident investigation using OpenSearch ML Commons flow agents connected to Amazon Bedrock Claude Sonnet. Fetches anomaly results, error logs, deploy events, and log patterns, then synthesizes a structured RCA report with root cause verdict, ranked evidence, impact assessment, recommended action, and confidence score. Use this skill for production incidents on clusters backed by Bedrock for real LLM inference.
license: Apache-2.0
compatibility: OpenSearch 2.12+, ml-commons plugin, anomaly-detection plugin, AWS Bedrock Claude Sonnet
allowed-tools: Bash
tags: [incident-response, bedrock, claude, rca, ai-investigation, aws]
---

# opensearch-bedrock-investigate

Wires OpenSearch ML Commons flow agents to Amazon Bedrock Claude Sonnet for AI-powered root cause analysis. The flow agent runs 5 tools sequentially — detector lookup, anomaly results, error logs, deploy events, log patterns — then calls Bedrock Claude to synthesize a structured incident report in under 5 seconds.

## When to use

- You have an OpenSearch cluster on AWS or NetApp Instaclustr and want real LLM inference (not a mock)
- An anomaly alert fired and you need an actionable RCA in seconds
- You want the investigation to be fully automated: alert → tools → Bedrock → structured report
- You are demoing AI-powered incident response with a real language model

## How it works

1. **Connector** — OpenSearch ML Commons connector using `aws_sigv4` protocol calls `bedrock-runtime.{region}.amazonaws.com/model/{model-id}/invoke`
2. **Flow agent** — 6 tools run sequentially:
   - `SearchAnomalyDetectorsTool` — confirms detector config
   - `SearchAnomalyResultsTool` — fetches recent high-grade anomalies
   - `SearchIndexTool` (prod-logs) — fetches ERROR/WARN logs in the anomaly window
   - `SearchIndexTool` (deploy-events) — fetches recent deploys
   - `LogPatternTool` — clusters recurring error messages
   - `MLModelTool` — calls Bedrock Claude Sonnet with all evidence
3. **Structured prompt** — Claude responds with a fixed-format report: root cause, ranked evidence, impact, recommended action, timeline, and confidence score

## Prerequisites

- OpenSearch cluster accessible at `$OPENSEARCH_URL`
- AWS IAM credentials with `bedrock:InvokeModel` permission on the Claude Sonnet model
- Bedrock Claude Sonnet model access enabled in your AWS region (us-east-1 recommended)
- `DETECTOR_ID` and `AGENT_ID` from `solution/.ids.env` (written by `setup_agent.sh`)

**Required IAM policy:**
```json
{
  "Effect": "Allow",
  "Action": "bedrock:InvokeModel",
  "Resource": "arn:aws:bedrock:us-east-1::foundation-model/anthropic.claude-*"
}
```

## Inputs

| Parameter | Type | Required | Description |
|---|---|---|---|
| `opensearch_url` | string | yes | Cluster endpoint with credentials embedded |
| `aws_access_key_id` | string | yes | IAM access key with `bedrock:InvokeModel` |
| `aws_secret_access_key` | string | yes | IAM secret key |
| `aws_region` | string | no | AWS region for Bedrock (default: `us-east-1`) |
| `bedrock_model_id` | string | no | Bedrock model ID (default: `us.anthropic.claude-sonnet-4-5-20251001-v1:0`) |
| `detector_id` | string | yes | Anomaly detector ID |
| `agent_id` | string | yes | Registered flow agent ID |

## Setup

```bash
# Configure credentials in instaclustr.env
cat >> instaclustr.env << 'EOF'
AWS_ACCESS_KEY_ID=<your-access-key>
AWS_SECRET_ACCESS_KEY=<your-secret-key>
AWS_REGION=us-east-1
EOF

source instaclustr.env

# Register Bedrock connector + flow agent (auto-detects AWS creds)
OPENSEARCH_URL="$OPENSEARCH_URL" \
AWS_ACCESS_KEY_ID="$AWS_ACCESS_KEY_ID" \
AWS_SECRET_ACCESS_KEY="$AWS_SECRET_ACCESS_KEY" \
AWS_REGION="$AWS_REGION" \
  bash solution/setup_agent.sh
```

## Usage

```bash
# Load agent IDs
source solution/.ids.env

# Trigger investigation manually
curl -X POST "$OPENSEARCH_URL/_plugins/_ml/agents/$AGENT_ID/_execute" \
  -H 'Content-Type: application/json' \
  -d '{"parameters": {"question": "Investigate the current anomaly for payment-service"}}'

# Trigger via webhook (fires automatically from alerting monitor)
curl -X POST http://localhost:8765/trigger-investigation \
  -H 'Content-Type: application/json' \
  -d "{\"anomaly_grade\": 0.91, \"agent_id\": \"$AGENT_ID\"}"
```

## Output

Bedrock Claude returns a structured markdown report:

```markdown
## Root Cause
Deploy regression — payment-service v2.3.0 → v2.3.1 by alice, deployed 28 minutes before anomaly onset. The change description mentions connection pool refactoring, consistent with DB connection timeout errors.

## Evidence (ranked by confidence)
1. Deploy by alice at T-28m modified connection pool config — HIGH correlation with anomaly onset timing
2. 38 NullPointerExceptions in PaymentProcessor.validate() within 10-minute window (0 in baseline)
3. P99 latency spiked from ~120ms to 2444ms — 20× baseline
4. DB connection pool exhaustion: "pool exhausted (max=10, active=10)" in 12 log entries

## Impact
- Affected service: payment-service
- P99 latency: 2444ms (normal: ~120ms)
- Error rate: 12%
- Estimated blast radius: moderate (downstream order-service may be affected)

## Recommended Action
- **Immediate**: Rollback payment-service to v2.3.0
- **Verify**: `GET prod-logs/_search` filtered for "pool exhausted" — if count drops after rollback, root cause confirmed
- **Confidence**: 91%

## Timeline
T-28m: alice deployed v2.3.1 → T-5m: latency creep begins → T-0: anomaly detector fires (grade: 0.91)
```

## Implementation

`solution/setup_agent.sh` — detects `AWS_ACCESS_KEY_ID` and registers `aws_sigv4` Bedrock connector instead of mock-llm  
`solution/os_client.py` — shared OpenSearch client with SSL + auth support for managed clusters  

**Connector protocol:** `aws_sigv4` with SigV4 signing via OpenSearch ML Commons built-in support  
**Model invocation URL:** `https://bedrock-runtime.{region}.amazonaws.com/model/{model-id}/invoke`  
**Post-process function:** `connector.post_process.bedrock.chat` extracts `content[0].text` from the Bedrock response

## Differences from opensearch-anomaly-investigate

| | opensearch-anomaly-investigate | opensearch-bedrock-investigate |
|---|---|---|
| LLM backend | mock-llm (local) or any OpenAI-compatible | Amazon Bedrock Claude Sonnet |
| Auth | none (local mock) | AWS SigV4 |
| Report quality | scripted/template | real Claude reasoning |
| Latency | ~200ms | ~2–5s (Bedrock inference) |
| Cost | free | Bedrock token pricing |
| Best for | local dev, demos without AWS | production, live demos with real AI |
