#!/usr/bin/env bash
# Registers all OpenSearch ML resources for the investigation demo:
#   1. Remote model connector  → Bedrock Claude Sonnet (or mock-llm fallback)
#   2. Remote model            → wraps the connector
#   3. Anomaly detector        → watches prod-metrics p99 for payment-service
#   4. Flow agent              → investigation pipeline (6 tools + enhanced RCA prompt)
#
# Environment variables:
#   OPENSEARCH_URL        - e.g. https://user:pass@host:9200
#   MOCK_LLM_URL          - fallback if AWS creds not set
#   AWS_ACCESS_KEY_ID     - use Bedrock when set
#   AWS_SECRET_ACCESS_KEY
#   AWS_REGION            - default: us-east-1
#   BEDROCK_MODEL_ID      - default: us.anthropic.claude-sonnet-4-5-20251001-v1:0
set -euo pipefail

BASE="${OPENSEARCH_URL:-http://localhost:9200}"
MOCK_LLM_URL="${MOCK_LLM_URL:-http://mock-llm:8765}"
AWS_REGION="${AWS_REGION:-us-east-1}"
BEDROCK_MODEL_ID="${BEDROCK_MODEL_ID:-us.anthropic.claude-sonnet-4-5-20251001-v1:0}"
BLUE='\033[0;34m'; GREEN='\033[0;32m'; YELLOW='\033[1;33m'; RED='\033[0;31m'; NC='\033[0m'

ok()   { echo -e "${GREEN}  ✓${NC} $*"; }
info() { echo -e "${BLUE}  ▸${NC} $*"; }
warn() { echo -e "${YELLOW}  !${NC} $*"; }
die()  { echo -e "${RED}  ✗${NC} $*"; exit 1; }

call() {
    local method=$1 path=$2 body=${3:-}
    if [[ -n "$body" ]]; then
        curl -sf -X "$method" "$BASE$path" -H 'Content-Type: application/json' -d "$body"
    else
        curl -sf -X "$method" "$BASE$path"
    fi
}

info "OpenSearch:  $BASE"

# ── Detect LLM backend ────────────────────────────────────────────────────────
USE_BEDROCK=false
if [[ -n "${AWS_ACCESS_KEY_ID:-}" && -n "${AWS_SECRET_ACCESS_KEY:-}" ]]; then
    USE_BEDROCK=true
    info "LLM backend: Bedrock ($BEDROCK_MODEL_ID, $AWS_REGION)"
else
    info "LLM backend: mock-llm ($MOCK_LLM_URL)"
fi

# ── Allow trusted connector endpoint ─────────────────────────────────────────
info "Configuring trusted ML endpoints …"
if [[ "$USE_BEDROCK" == "true" ]]; then
    BEDROCK_ENDPOINT="https://bedrock-runtime.${AWS_REGION}.amazonaws.com"
    call PUT /_cluster/settings "{
      \"persistent\": {
        \"plugins.ml_commons.trusted_connector_endpoints_regex\": [
          \"^https://bedrock-runtime\\\\..*\\\\.amazonaws\\\\.com/.*\"
        ]
      }
    }" > /dev/null
    ok "Trusted endpoint: $BEDROCK_ENDPOINT"
else
    call PUT /_cluster/settings "{
      \"persistent\": {
        \"plugins.ml_commons.trusted_connector_endpoints_regex\": [
          \"^${MOCK_LLM_URL}.*\"
        ]
      }
    }" > /dev/null
    ok "Trusted endpoint: $MOCK_LLM_URL"
fi

# ── 1. Connector ─────────────────────────────────────────────────────────────
info "Registering connector …"
if [[ "$USE_BEDROCK" == "true" ]]; then
    BEDROCK_URL="https://bedrock-runtime.${AWS_REGION}.amazonaws.com/model/${BEDROCK_MODEL_ID}/invoke"
    CONNECTOR_RESP=$(call POST /_plugins/_ml/connectors/_create "{
      \"name\": \"bedrock-claude-connector\",
      \"description\": \"Bedrock Claude Sonnet for incident investigation\",
      \"version\": 1,
      \"protocol\": \"aws_sigv4\",
      \"parameters\": {
        \"region\": \"${AWS_REGION}\",
        \"service_name\": \"bedrock\",
        \"model\": \"${BEDROCK_MODEL_ID}\",
        \"max_tokens\": 2048,
        \"anthropic_version\": \"bedrock-2023-05-31\"
      },
      \"credential\": {
        \"access_key\": \"${AWS_ACCESS_KEY_ID}\",
        \"secret_key\": \"${AWS_SECRET_ACCESS_KEY}\"
      },
      \"actions\": [
        {
          \"action_type\": \"predict\",
          \"method\": \"POST\",
          \"url\": \"${BEDROCK_URL}\",
          \"headers\": { \"Content-Type\": \"application/json\" },
          \"request_body\": \"{\\\"anthropic_version\\\":\\\"bedrock-2023-05-31\\\",\\\"max_tokens\\\":2048,\\\"messages\\\":[{\\\"role\\\":\\\"user\\\",\\\"content\\\":\\\"\${parameters.prompt}\\\"}]}\",
          \"post_process_function\": \"connector.post_process.bedrock.chat\"
        }
      ]
    }")
else
    CONNECTOR_RESP=$(call POST /_plugins/_ml/connectors/_create "{
      \"name\": \"mock-llm-connector\",
      \"description\": \"Mock LLM for investigation synthesis\",
      \"version\": 1,
      \"protocol\": \"http\",
      \"parameters\": {
        \"endpoint\": \"${MOCK_LLM_URL}\",
        \"model\": \"investigation-model\"
      },
      \"credential\": { \"mock_key\": \"none\" },
      \"actions\": [
        {
          \"action_type\": \"predict\",
          \"method\": \"POST\",
          \"url\": \"${MOCK_LLM_URL}/v1/chat/completions\",
          \"headers\": { \"Content-Type\": \"application/json\" },
          \"request_body\": \"{ \\\"model\\\": \\\"investigation-model\\\", \\\"messages\\\": [{\\\"role\\\": \\\"user\\\", \\\"content\\\": \\\"\${parameters.prompt}\\\"}] }\",
          \"post_process_function\": \"connector.post_process.openai.chat_completion\"
        }
      ]
    }")
fi
CONNECTOR_ID=$(echo "$CONNECTOR_RESP" | python3 -c "import sys,json; print(json.load(sys.stdin)['connector_id'])")
ok "Connector: $CONNECTOR_ID"

# ── 2. Remote model ───────────────────────────────────────────────────────────
info "Registering remote model …"
MODEL_RESP=$(call POST /_plugins/_ml/models/_register '{
  "name": "investigation-synthesizer",
  "version": "1.0",
  "function_name": "remote",
  "description": "Synthesises tool outputs into an investigation report",
  "connector_id": "'"$CONNECTOR_ID"'"
}')
TASK_ID=$(echo "$MODEL_RESP" | python3 -c "import sys,json; print(json.load(sys.stdin).get('task_id',''))")

# Poll for model deploy
for i in $(seq 1 20); do
    TASK=$(call GET /_plugins/_ml/tasks/"$TASK_ID")
    STATE=$(echo "$TASK" | python3 -c "import sys,json; print(json.load(sys.stdin).get('state',''))")
    if [[ "$STATE" == "COMPLETED" ]]; then
        MODEL_ID=$(echo "$TASK" | python3 -c "import sys,json; print(json.load(sys.stdin).get('model_id',''))")
        break
    fi
    sleep 2
done
[[ -z "${MODEL_ID:-}" ]] && die "Model registration timed out"
ok "Model: $MODEL_ID"

# Deploy the model
call POST /_plugins/_ml/models/"$MODEL_ID"/_deploy > /dev/null
sleep 3
ok "Model deployed"

# ── 3. Anomaly detector ───────────────────────────────────────────────────────
info "Creating anomaly detector …"
DETECTION_INTERVAL_MINUTES=1
START_TIME=$(python3 -c "
from datetime import datetime, timedelta, timezone
t = datetime.now(timezone.utc) - timedelta(hours=3)
print(int(t.timestamp() * 1000))
")
END_TIME=$(python3 -c "
from datetime import datetime, timezone
print(int(datetime.now(timezone.utc).timestamp() * 1000))
")

AD_RESP=$(call POST /_plugins/_anomaly_detection/detectors '{
  "name": "prod-latency-p99",
  "description": "Detects p99 latency anomalies for payment-service",
  "time_field": "@timestamp",
  "indices": ["prod-metrics"],
  "feature_attributes": [
    {
      "feature_name": "p99_latency",
      "feature_enabled": true,
      "aggregation_query": {
        "p99_latency": {
          "avg": { "field": "p99_latency_ms" }
        }
      }
    },
    {
      "feature_name": "error_rate",
      "feature_enabled": true,
      "aggregation_query": {
        "error_rate": {
          "avg": { "field": "error_rate_pct" }
        }
      }
    }
  ],
  "filter_query": {
    "term": { "service": "payment-service" }
  },
  "detection_interval": { "period": { "interval": '"$DETECTION_INTERVAL_MINUTES"', "unit": "Minutes" } },
  "window_delay": { "period": { "interval": 1, "unit": "Minutes" } }
}')

DETECTOR_ID=$(echo "$AD_RESP" | python3 -c "import sys,json; print(json.load(sys.stdin).get('_id',''))")
[[ -z "$DETECTOR_ID" ]] && die "Anomaly detector creation failed: $AD_RESP"
ok "Detector: $DETECTOR_ID (prod-latency-p99)"

# Start historical analysis so the detector processes our seeded data
info "Starting historical analysis …"
call POST /_plugins/_anomaly_detection/detectors/"$DETECTOR_ID"/_start \
  '{"start_time": '"$START_TIME"', "end_time": '"$END_TIME"'}' > /dev/null || \
call POST /_plugins/_anomaly_detection/detectors/"$DETECTOR_ID"/_start > /dev/null || true
ok "Historical analysis started"

# ── 4. Flow agent ─────────────────────────────────────────────────────────────
info "Registering investigation flow agent …"

# Compute anomaly window timestamps
WINDOW_START=$(python3 -c "
from datetime import datetime, timedelta, timezone
t = datetime.now(timezone.utc) - timedelta(minutes=10)
print(int(t.timestamp() * 1000))
")
WINDOW_END=$(python3 -c "
from datetime import datetime, timezone
print(int(datetime.now(timezone.utc).timestamp() * 1000))
")

AGENT_RESP=$(call POST /_plugins/_ml/agents/_register '{
  "name": "anomaly-investigation-agent",
  "type": "flow",
  "description": "Investigates anomaly alerts: confirms detector, fetches anomaly results, synthesises RCA report",
  "tools": [
    {
      "type": "SearchAnomalyDetectorsTool",
      "name": "find_detector",
      "parameters": {
        "detectorName": "prod-latency-p99"
      }
    },
    {
      "type": "SearchAnomalyResultsTool",
      "name": "get_anomaly_results",
      "parameters": {
        "startTime": '"$WINDOW_START"',
        "endTime": '"$WINDOW_END"',
        "size": 5
      }
    },
    {
      "type": "MLModelTool",
      "name": "synthesize_report",
      "parameters": {
        "model_id": "'"$MODEL_ID"'",
        "prompt": "You are an expert SRE on-call assistant. Anomaly detector prod-latency-p99 just fired.\n\nYour job is to produce a concise, actionable root cause analysis from the evidence below.\n\n[DETECTOR INFO]\n${parameters.find_detector.output}\n\n[ANOMALY RESULTS]\n${parameters.get_anomaly_results.output}\n\nRespond in this exact format:\n\n## Root Cause\n<1-2 sentence verdict: deploy regression / infrastructure / external dependency / unknown>\n\n## Evidence (ranked by confidence)\n1. <strongest signal with specific values>\n2. <second signal>\n3. <third signal if relevant>\n\n## Impact\n- Affected service: payment-service\n- P99 latency: <value from anomaly results> (normal: ~120ms)\n- Error rate: <value>%\n- Estimated blast radius: <narrow / moderate / wide>\n\n## Recommended Action\n- **Immediate**: <rollback / scale / page team / wait>\n- **Verify**: <specific query or check to confirm root cause>\n- **Confidence**: <0-100>%\n\n## Timeline\n<anomaly onset time> → <now>"
      }
    }
  ]
}')

AGENT_ID=$(echo "$AGENT_RESP" | python3 -c "import sys,json; print(json.load(sys.stdin).get('agent_id',''))")
[[ -z "$AGENT_ID" ]] && die "Agent registration failed: $AGENT_RESP"
ok "Agent: $AGENT_ID"

# Persist IDs for the demo script and the Streamlit UI
cat > /tmp/opensearch_demo_ids.env << EOF
DETECTOR_ID=$DETECTOR_ID
CONNECTOR_ID=$CONNECTOR_ID
MODEL_ID=$MODEL_ID
AGENT_ID=$AGENT_ID
WINDOW_START=$WINDOW_START
WINDOW_END=$WINDOW_END
EOF

# Write IDs to solution/.ids.env so the investigation-ui Docker service can read them
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cat > "$SCRIPT_DIR/.ids.env" << EOF
DETECTOR_ID=$DETECTOR_ID
AGENT_ID=$AGENT_ID
MODEL_ID=$MODEL_ID
EOF
ok "IDs also saved to solution/.ids.env (used by Streamlit UI)"

echo
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
echo "  Setup complete. Resource IDs saved to /tmp/opensearch_demo_ids.env"
echo "  Next:  python investigate.py"
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
