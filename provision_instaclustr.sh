#!/usr/bin/env bash
# Provisions a managed OpenSearch cluster on NetApp Instaclustr for the
# opensearch-skills demo, then updates docker-compose.yml to point at it.
#
# Uses the Instaclustr Provisioning API v1 — the correct API for provisioning
# API keys. The v2 Cluster Management API requires a different auth type.
#
# Prerequisites (run on your local machine, not inside Docker):
#   brew install jq   # or: apt install jq
#
# Usage:
#   export INSTACLUSTR_USER=you@example.com
#   export INSTACLUSTR_API_KEY=<your-provisioning-api-key>
#   bash provision_instaclustr.sh
#
#   # For verbose API responses (debugging):
#   bash provision_instaclustr.sh --debug
#
# Store secrets in GitHub Actions as:
#   INSTACLUSTR_API_KEY, INSTACLUSTR_USER
# Never hardcode credentials in this file.

set -euo pipefail

DEBUG=false
[[ "${1:-}" == "--debug" ]] && DEBUG=true

# ── Credentials (all from environment — never hardcode here) ──────────────────

API_KEY="${INSTACLUSTR_API_KEY:-}"
BASE="https://api.instaclustr.com/provisioning/v1"
CLUSTER_NAME="opensearch-skills"

[[ -z "$API_KEY" ]] && { read -rsp "Instaclustr provisioning API key: " API_KEY; echo; }

if [[ -z "${INSTACLUSTR_USER:-}" ]]; then
  read -rp "Instaclustr username (email): " INSTACLUSTR_USER
fi
AUTH="$INSTACLUSTR_USER:$API_KEY"

BLUE='\033[0;34m'; GREEN='\033[0;32m'; YELLOW='\033[1;33m'; RED='\033[0;31m'; NC='\033[0m'
ok()   { echo -e "${GREEN}  ✓${NC} $*"; }
info() { echo -e "${BLUE}  ▸${NC} $*"; }
warn() { echo -e "${YELLOW}  !${NC} $*"; }
die()  { echo -e "${RED}  ✗${NC} $*"; exit 1; }

api() {
  local method=$1 path=$2 body=${3:-}
  local resp http_code
  local tmpfile; tmpfile=$(mktemp)

  if [[ -n "$body" ]]; then
    http_code=$(curl -s -o "$tmpfile" -w "%{http_code}" -X "$method" "$BASE$path" \
      -u "$AUTH" \
      -H "Content-Type: application/json" \
      -d "$body")
  else
    http_code=$(curl -s -o "$tmpfile" -w "%{http_code}" -X "$method" "$BASE$path" \
      -u "$AUTH")
  fi

  resp=$(cat "$tmpfile"); rm -f "$tmpfile"

  if $DEBUG; then
    echo -e "  [DEBUG] $method $path → HTTP $http_code" >&2
    echo -e "  [DEBUG] Response: ${resp:0:500}" >&2
  fi

  if [[ "$http_code" -ge 400 ]]; then
    echo -e "${RED}  ✗ API error HTTP $http_code on $method $path${NC}" >&2
    echo "    Response: $resp" >&2
    return 1
  fi

  echo "$resp"
}

api_code() {
  # Returns only the HTTP status code (no body output)
  curl -s -o /dev/null -w "%{http_code}" -X GET "$BASE$1" -u "$AUTH"
}

# ── 1. Test auth ──────────────────────────────────────────────────────────────

info "Testing credentials against provisioning API v1 …"
HTTP=$(api_code "/clusters" || echo "000")

case "$HTTP" in
  200) ok "Authenticated successfully" ;;
  401) die "401 Unauthorized — check INSTACLUSTR_USER and API key" ;;
  403) die "403 Forbidden — API key does not have provisioning access" ;;
  000) die "Connection failed — check network / VPN access to api.instaclustr.com" ;;
  *)   warn "HTTP $HTTP on GET /clusters — proceeding (may still work)" ;;
esac

# ── 2. Fetch available OpenSearch versions ────────────────────────────────────

info "Fetching available OpenSearch bundle versions …"
BUNDLES=$(api GET "/bundles?providerName=AWS_VPC&datacentre=US_EAST_1&type=OPENSEARCH" || echo "[]")
OS_VERSION=$(echo "$BUNDLES" | jq -r '[.[] | select(.bundle == "OPENSEARCH")] | sort_by(.version) | last | .version // empty' 2>/dev/null || true)

if [[ -z "$OS_VERSION" ]]; then
  OS_VERSION="2.11.1"
  warn "Could not fetch bundle list — defaulting to OpenSearch $OS_VERSION"
else
  ok "Latest OpenSearch version available: $OS_VERSION"
fi

# ── 3. Pick a node size ───────────────────────────────────────────────────────

NODE_SIZE=$(echo "$BUNDLES" | jq -r '
  [.[] | select(.bundle == "OPENSEARCH") | .nodeSize // empty]
  | map(select(test("t3|t4g|r5|r6g")))
  | first // empty' 2>/dev/null || true)

if [[ -z "$NODE_SIZE" ]]; then
  NODE_SIZE="t3.medium"
  warn "Could not determine node size — defaulting to $NODE_SIZE"
else
  ok "Using node size: $NODE_SIZE"
fi

# ── 4. Check for existing cluster ─────────────────────────────────────────────

info "Checking for existing cluster named '$CLUSTER_NAME' …"
ALL_CLUSTERS=$(api GET "/clusters" || echo "[]")
EXISTING_ID=$(echo "$ALL_CLUSTERS" | jq -r --arg name "$CLUSTER_NAME" \
  '.[] | select(.clusterName == $name) | .id // empty' 2>/dev/null | head -1 || true)

if [[ -n "$EXISTING_ID" ]]; then
  warn "Cluster '$CLUSTER_NAME' already exists (ID: $EXISTING_ID) — skipping creation"
  CLUSTER_ID="$EXISTING_ID"
else

# ── 5. Provision cluster ───────────────────────────────────────────────────────

  info "Provisioning OpenSearch $OS_VERSION cluster ($NODE_SIZE × 3, US_EAST_1, NON_PRODUCTION) …"

  CREATE_RESP=$(api POST "/clusters" "{
    \"cluster\": {
      \"clusterName\": \"$CLUSTER_NAME\",
      \"nodeSize\":     \"$NODE_SIZE\",
      \"numberNodes\":  3,
      \"datacentre\":   \"US_EAST_1\",
      \"clusterNetwork\": \"10.0.0.0/16\",
      \"privateNetworkCluster\": false,
      \"slaTier\":  \"NON_PRODUCTION\",
      \"provider\": \"AWS_VPC\",
      \"bundles\": [
        {
          \"bundle\": \"OPENSEARCH\",
          \"version\": \"$OS_VERSION\",
          \"options\": { \"dedicatedMasterNodes\": false }
        },
        {
          \"bundle\": \"OPENSEARCH_DASHBOARDS\",
          \"version\": \"$OS_VERSION\",
          \"options\": {}
        }
      ]
    }
  }")

  CLUSTER_ID=$(echo "$CREATE_RESP" | jq -r '.id // .clusterId // empty' 2>/dev/null || true)
  [[ -z "$CLUSTER_ID" ]] && die "Cluster creation failed — run with --debug to see the full response"
  ok "Cluster provisioning started: $CLUSTER_ID"
fi

# ── 6. Poll until RUNNING ─────────────────────────────────────────────────────

info "Polling until cluster is RUNNING (usually 10–15 min) …"
while true; do
  CLUSTER_RESP=$(api GET "/clusters/$CLUSTER_ID" 2>/dev/null || echo '{}')
  STATUS=$(echo "$CLUSTER_RESP" | jq -r '.clusterStatus // .status // "UNKNOWN"')

  printf "\r  Status: %-25s [%s]" "$STATUS" "$(date +%H:%M:%S)"

  if [[ "$STATUS" == "RUNNING" ]]; then
    echo ""; ok "Cluster is RUNNING"
    break
  elif [[ "$STATUS" == "FAILED" || "$STATUS" == "DELETED" || "$STATUS" == "ERROR" ]]; then
    echo ""; die "Cluster entered $STATUS — check console.instaclustr.com"
  fi
  sleep 30
done

# ── 7. Extract connection info ─────────────────────────────────────────────────

info "Fetching connection details …"
CLUSTER_RESP=$(api GET "/clusters/$CLUSTER_ID" 2>/dev/null || echo '{}')

OS_HOST=$(echo "$CLUSTER_RESP" | jq -r '
  .dataCentres[0].nodes[0].publicAddress //
  .nodes[0].publicAddress //
  .nodes[0].privateAddress // empty' 2>/dev/null || true)

OS_PORT="9243"
OS_PROTOCOL="https"
OS_USER=$(echo "$CLUSTER_RESP" | jq -r '.username // "icadmin"' 2>/dev/null || echo "icadmin")
OS_PASS=$(echo "$CLUSTER_RESP" | jq -r '.instaclustrUserPassword // .defaultUserPassword // empty' 2>/dev/null || true)

DASH_HOST=$(echo "$CLUSTER_RESP" | jq -r '
  .openSearchDashboards.nodes[0].publicAddress //
  (.bundles[]? | select(.bundle=="OPENSEARCH_DASHBOARDS") | .nodes[0].publicAddress) //
  empty' 2>/dev/null || true)

if [[ -z "$OS_HOST" ]]; then
  warn "Host not yet in API response — copy it from Connection Info in the Instaclustr console"
  OS_HOST="<paste-host-from-console>"
fi
if [[ -z "$OS_PASS" ]]; then
  warn "Password not returned by API — copy it from the Instaclustr console"
  OS_PASS="<paste-password-from-console>"
fi

ok "Host:       $OS_HOST:$OS_PORT"
ok "User:       $OS_USER"
[[ "$OS_PASS" != "<paste"* ]] && ok "Password:   (set)" || warn "Password:   <not returned — check console>"
[[ -n "$DASH_HOST" ]] && ok "Dashboards: https://$DASH_HOST"

# ── 8. Write instaclustr.env ──────────────────────────────────────────────────

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

cat > "$SCRIPT_DIR/instaclustr.env" << ENV
# NetApp Instaclustr managed OpenSearch — generated by provision_instaclustr.sh
# This file is gitignored — do not commit it.
CLUSTER_ID=$CLUSTER_ID
OPENSEARCH_URL=${OS_PROTOCOL}://${OS_USER}:${OS_PASS}@${OS_HOST}:${OS_PORT}
OPENSEARCH_HOST=${OS_HOST}
OPENSEARCH_PORT=${OS_PORT}
OPENSEARCH_USER=${OS_USER}
OPENSEARCH_PASSWORD=${OS_PASS}
DASHBOARDS_URL=https://${DASH_HOST:-<dashboards-host>}
ENV

ok "instaclustr.env written (gitignored)"

# ── 9. Patch docker-compose.yml ───────────────────────────────────────────────

info "Patching docker-compose.yml to point at managed cluster …"
python3 - "$SCRIPT_DIR/docker-compose.yml" \
  "$OS_PROTOCOL" "$OS_HOST" "$OS_PORT" "$OS_USER" "$OS_PASS" << 'PYEOF'
import sys, re

compose_path, protocol, host, port, user, password = sys.argv[1:]
managed_url = f"{protocol}://{user}:{password}@{host}:{port}"

with open(compose_path) as f:
    content = f.read()

content = re.sub(r'OPENSEARCH_URL=http://opensearch-node1:9200',
                 f'OPENSEARCH_URL={managed_url}', content)
content = re.sub(r'OPENSEARCH_HOSTS=\["http://opensearch-node1:9200"\]',
                 f'OPENSEARCH_HOSTS=["{managed_url}"]', content)

with open(compose_path, 'w') as f:
    f.write(content)
print(f"  OPENSEARCH_URL → {protocol}://{user}:***@{host}:{port}")
PYEOF

ok "docker-compose.yml updated"

# ── 10. Apply ML Commons settings ─────────────────────────────────────────────

if [[ "$OS_HOST" != "<paste"* && "$OS_PASS" != "<paste"* ]]; then
  info "Applying ML Commons cluster settings …"
  OS_BASE="${OS_PROTOCOL}://${OS_HOST}:${OS_PORT}"
  for SETTING in \
    '{"persistent":{"plugins.ml_commons.only_run_on_ml_node":false}}' \
    '{"persistent":{"plugins.ml_commons.native_memory_threshold":99}}' \
    '{"persistent":{"plugins.ml_commons.memory_feature_enabled":true}}' \
    '{"persistent":{"plugins.ml_commons.connector.private_ip_enabled":true}}' \
    '{"persistent":{"plugins.ml_commons.trusted_connector_endpoints_regex":["^http://.*:8765.*","^https://.*:8765.*"]}}'; do
    curl -sf -X PUT "${OS_BASE}/_cluster/settings" \
      -u "$OS_USER:$OS_PASS" \
      -H "Content-Type: application/json" \
      -d "$SETTING" > /dev/null 2>&1 || true
  done
  ok "ML Commons settings applied"
else
  warn "Skipping ML Commons settings — fill in host/password in instaclustr.env first, then run:"
  warn "  source instaclustr.env"
  warn "  bash solution/setup_agent.sh"
fi

# ── Done ──────────────────────────────────────────────────────────────────────

echo
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
echo "  Next steps:"
echo ""
echo "  1. Start non-OpenSearch services:"
echo "     docker compose up -d mock-llm seed-data log-generator investigation-ui"
echo ""
echo "  2. Register ML agent (point at managed cluster):"
echo "     source instaclustr.env && bash solution/setup_agent.sh"
echo ""
echo "  3. Wire the always-on alerting loop:"
echo "     bash solution/setup_realtime.sh"
echo ""
echo "  4. Open the UI:  http://localhost:8501"
echo ""
echo "  Cluster ID: $CLUSTER_ID"
echo "  Console:    https://console.instaclustr.com/clusters/$CLUSTER_ID"
[[ -n "$DASH_HOST" && "$DASH_HOST" != "<"* ]] && \
  echo "  Dashboards: https://$DASH_HOST"
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
