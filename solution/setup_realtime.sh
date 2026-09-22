#!/usr/bin/env bash
# Sets up the always-on production loop:
#
#   Log generator (Docker) ──► prod-metrics / prod-logs
#                                      │
#                           Real-time anomaly detector
#                                      │ grade > 0.7
#                           Alerting monitor (1-min schedule)
#                                      │ fires
#                           Webhook → mock-llm /trigger-investigation
#                                      │
#                           Investigation agent ──► Slack / PagerDuty
#
# Run once after docker compose up. The loop then runs forever without
# any manual intervention.
set -euo pipefail

BASE="${OPENSEARCH_URL:-http://localhost:9200}"
MOCK_LLM="${MOCK_LLM_URL:-http://localhost:8765}"
BLUE='\033[0;34m'; GREEN='\033[0;32m'; YELLOW='\033[1;33m'; RED='\033[0;31m'; NC='\033[0m'

ok()      { echo -e "${GREEN}  ✓${NC} $*"; }
info()    { echo -e "${BLUE}  ▸${NC} $*"; }
warn()    { echo -e "${YELLOW}  !${NC} $*"; }
die()     { echo -e "${RED}  ✗${NC} $*"; exit 1; }

call() {
    local method=$1 path=$2 body=${3:-}
    if [[ -n "$body" ]]; then
        curl -sf -X "$method" "$BASE$path" -H 'Content-Type: application/json' -d "$body"
    else
        curl -sf -X "$method" "$BASE$path"
    fi
}

source /tmp/opensearch_demo_ids.env 2>/dev/null || true
[[ -z "${DETECTOR_ID:-}" ]] && die "Run setup_agent.sh first to get DETECTOR_ID"
[[ -z "${AGENT_ID:-}"    ]] && die "Run setup_agent.sh first to get AGENT_ID"

# ── 1. Start real-time anomaly detector ──────────────────────────────────────
info "Starting anomaly detector in real-time mode …"
# Stop any running historical job first
call POST "/_plugins/_anomaly_detection/detectors/$DETECTOR_ID/_stop" > /dev/null 2>&1 || true
sleep 2
# Start without time bounds = real-time
START_RESP=$(call POST "/_plugins/_anomaly_detection/detectors/$DETECTOR_ID/_start" '{}' 2>/dev/null || echo '{}')
ok "Detector $DETECTOR_ID running in real-time (results every ~1 min)"

# ── 2. Create alerting notification channel ───────────────────────────────────
info "Creating notification channel (webhook → mock-llm) …"

# Get mock-llm container IP for the webhook
MOCK_IP=$(docker inspect mock-llm --format '{{range .NetworkSettings.Networks}}{{.IPAddress}}{{end}}' 2>/dev/null || echo "localhost")
WEBHOOK_URL="http://$MOCK_IP:8765/trigger-investigation"

CHANNEL_RESP=$(call POST "/_plugins/_notifications/configs" "{
  \"config_id\": \"investigation-webhook\",
  \"config\": {
    \"name\":        \"Investigation Webhook\",
    \"description\": \"Triggers investigation agent when anomaly fires\",
    \"config_type\": \"webhook\",
    \"is_enabled\":  true,
    \"webhook\": {
      \"url\":    \"$WEBHOOK_URL\",
      \"method\": \"POST\",
      \"header_params\": {
        \"Content-Type\": \"application/json\"
      }
    }
  }
}" 2>/dev/null || echo '{}')

CHANNEL_ID=$(echo "$CHANNEL_RESP" | python3 -c "import sys,json; print(json.load(sys.stdin).get('config_id','investigation-webhook'))" 2>/dev/null || echo "investigation-webhook")
ok "Notification channel: $CHANNEL_ID → $WEBHOOK_URL"

# ── 3. Create alerting monitor ────────────────────────────────────────────────
info "Creating alerting monitor (polls anomaly results every 1 min) …"

MONITOR_RESP=$(call POST "/_plugins/_alerting/monitors" "{
  \"name\":    \"anomaly-investigation-trigger\",
  \"type\":    \"monitor\",
  \"enabled\": true,
  \"schedule\": {
    \"period\": {\"interval\": 1, \"unit\": \"MINUTES\"}
  },
  \"inputs\": [{
    \"search\": {
      \"indices\": [\".opendistro-anomaly-results*\"],
      \"query\": {
        \"size\": 1,
        \"query\": {
          \"bool\": {
            \"must\": [
              {\"range\": {\"anomaly_grade\": {\"gt\": 0.7}}},
              {\"range\": {\"data_end_time\": {\"gte\": \"now-2m\"}}}
            ]
          }
        },
        \"sort\": [{\"anomaly_grade\": \"desc\"}],
        \"_source\": [\"anomaly_grade\", \"anomaly_score\", \"data_end_time\", \"detector_id\"]
      }
    }
  }],
  \"triggers\": [{
    \"name\":     \"high-grade-anomaly\",
    \"severity\": \"1\",
    \"condition\": {
      \"script\": {
        \"source\": \"ctx.results[0].hits.total.value > 0\",
        \"lang\":   \"painless\"
      }
    },
    \"actions\": [{
      \"name\":            \"trigger-investigation-agent\",
      \"destination_id\":  \"$CHANNEL_ID\",
      \"message_template\": {
        \"source\": \"{\\\"agent_id\\\": \\\"$AGENT_ID\\\", \\\"anomaly_grade\\\": {{ctx.results.0.hits.hits.0._source.anomaly_grade}}, \\\"data_end_time\\\": {{ctx.results.0.hits.hits.0._source.data_end_time}}, \\\"detector_id\\\": \\\"{{ctx.results.0.hits.hits.0._source.detector_id}}\\\"}\"
      }
    }]
  }]
}")

MONITOR_ID=$(echo "$MONITOR_RESP" | python3 -c "import sys,json; print(json.load(sys.stdin).get('_id',''))" 2>/dev/null || echo "")
[[ -n "$MONITOR_ID" ]] && ok "Monitor: $MONITOR_ID (runs every 1 min)" || warn "Monitor creation partial — check Dashboards Alerting"

# Save monitor ID
echo "MONITOR_ID=${MONITOR_ID:-unknown}" >> /tmp/opensearch_demo_ids.env

# ── 4. Start log generator ────────────────────────────────────────────────────
info "Starting log generator service …"
if docker ps --format '{{.Names}}' 2>/dev/null | grep -q log-generator; then
    ok "log-generator already running"
else
    docker compose up -d log-generator 2>/dev/null && ok "log-generator started" || \
        warn "log-generator not in compose — run: python solution/log_generator.py &"
fi

echo
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
echo "  Production loop is live. No more manual scripts needed."
echo ""
echo "  Flow:"
echo "    log-generator → prod-metrics/prod-logs (every 30s)"
echo "    Detector       → checks for anomalies   (every 1 min)"
echo "    Monitor        → polls anomaly results   (every 1 min)"
echo "    Webhook        → triggers investigation  (on grade > 0.7)"
echo ""
echo "  Trigger a test incident:"
echo "    touch /tmp/incident_mode            # in log-generator container"
echo "    docker exec log-generator touch /tmp/incident_mode"
echo ""
echo "  Watch investigation logs:"
echo "    docker logs -f mock-llm"
echo ""
echo "  Dashboards:"
echo "    http://localhost:5601/app/anomaly-detection-dashboards#/detectors/$DETECTOR_ID/results"
echo "    http://localhost:5601/app/alerting#/monitors"
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
