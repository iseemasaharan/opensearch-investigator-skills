#!/usr/bin/env bash
# Validates abstract claims against a live OpenSearch 3.x cluster.
# Usage: ./validate_abstract.sh [OPENSEARCH_URL]
set -euo pipefail

BASE="${1:-http://localhost:9200}"
PASS=0; FAIL=0; WARN=0

ok()   { echo "  [PASS] $*"; PASS=$((PASS+1)); }
fail() { echo "  [FAIL] $*"; FAIL=$((FAIL+1)); }
warn() { echo "  [WARN] $*"; WARN=$((WARN+1)); }
section() { echo; echo "=== $* ==="; }

# ── 1. Cluster health ────────────────────────────────────────────────────────
section "Cluster Health"
STATUS=$(curl -sf "$BASE/_cluster/health" | python3 -c "import sys,json; print(json.load(sys.stdin)['status'])")
VER=$(curl -sf "$BASE/" | python3 -c "import sys,json; print(json.load(sys.stdin)['version']['number'])")
echo "  OpenSearch $VER  cluster=$STATUS"
[[ "$STATUS" != "red" ]] && ok "Cluster healthy" || fail "Cluster is RED"
[[ "$VER" == 3.* ]]      && ok "Running 3.x GA (abstract targets 3.x)" || warn "Not 3.x: $VER"

# ── 2. Plugin inventory ──────────────────────────────────────────────────────
section "Plugin Inventory"
PLUGINS=$(curl -sf "$BASE/_cat/plugins?h=component")
for p in opensearch-skills opensearch-ml opensearch-knn opensearch-neural-search \
          opensearch-sql opensearch-flow-framework; do
  echo "$PLUGINS" | grep -q "$p" \
    && ok "Plugin present: $p" \
    || fail "Plugin MISSING: $p"
done

# ── 3. Built-in skill taxonomy (abstract Table §3) ───────────────────────────
section "Built-in Skill Taxonomy"
ALL_TOOLS=$(curl -sf "$BASE/_plugins/_ml/tools")

check_tool() {
  local name="$1" label="${2:-$1}"
  echo "$ALL_TOOLS" | python3 -c "
import sys, json
tools = json.load(sys.stdin)
names = [t['name'] for t in tools]
print('found' if '$name' in names else 'missing')
" | grep -q found \
    && ok "Tool registered: $label" \
    || fail "Tool NOT registered: $label (abstract claims it exists)"
}

check_tool PPLTool
check_tool VectorDBTool
check_tool RAGTool
check_tool NeuralSparseSearchTool
check_tool MLModelTool
check_tool WebSearchTool        "WebSearchTool (abstract: new in 3.0)"
check_tool SearchIndexTool
check_tool ListIndexTool        "ListIndexTool (REAL name for abstract's 'CATIndexTool')"

# CATIndexTool is the abstract's claimed name — verify it does NOT exist
echo "$ALL_TOOLS" | python3 -c "
import sys, json
tools = json.load(sys.stdin)
names = [t['name'] for t in tools]
print('found' if 'CATIndexTool' in names else 'missing')
" | grep -q missing \
  && warn "CATIndexTool absent — abstract §3 uses wrong name; real tool is ListIndexTool" \
  || ok "CATIndexTool exists (name check)"

check_tool LogPatternTool       "LogPatternTool (abstract claims mcp-server-py only — actually built-in)"
check_tool IndexMappingTool     "IndexMappingTool (not in abstract taxonomy; ships in skills plugin)"
check_tool AgentTool            "AgentTool (not in abstract; enables agent-as-tool)"
check_tool McpSseTool           "McpSseTool (not in abstract; native MCP SSE bridge in 3.0)"

# ── 4. PPL engine ────────────────────────────────────────────────────────────
section "PPL Engine (abstract: NL→PPL demo)"
# Create and populate a minimal log index
curl -sf -X PUT "$BASE/val-logs" -H 'Content-Type: application/json' -d '{
  "mappings":{"properties":{"status":{"type":"integer"},"msg":{"type":"text"}}}
}' > /dev/null
curl -sf -X POST "$BASE/val-logs/_bulk" -H 'Content-Type: application/json' -d '
{"index":{}}
{"status":200,"msg":"ok"}
{"index":{}}
{"status":500,"msg":"error"}
' > /dev/null
curl -sf -X POST "$BASE/val-logs/_refresh" > /dev/null

PPL_RESULT=$(curl -sf -X POST "$BASE/_plugins/_ppl" \
  -H 'Content-Type: application/json' \
  -d '{"query":"source=val-logs | stats count() as total by status"}')
echo "$PPL_RESULT" | python3 -c "import sys,json; d=json.load(sys.stdin); print(d)" | grep -q datarows \
  && ok "PPL query executes and returns datarows" \
  || fail "PPL query failed"

# ── 5. k-NN / VectorDBTool backend ───────────────────────────────────────────
section "k-NN Vector Search (VectorDBTool backend)"
curl -sf -X DELETE "$BASE/val-knn" > /dev/null 2>&1 || true
curl -sf -X PUT "$BASE/val-knn" -H 'Content-Type: application/json' -d '{
  "settings":{"index":{"knn":true}},
  "mappings":{"properties":{"v":{"type":"knn_vector","dimension":3},"t":{"type":"keyword"}}}
}' > /dev/null
curl -sf -X POST "$BASE/val-knn/_bulk" -H 'Content-Type: application/json' -d '
{"index":{"_id":"a"}}
{"v":[1,2,3],"t":"apple"}
{"index":{"_id":"b"}}
{"v":[10,20,30],"t":"far"}
' > /dev/null
curl -sf -X POST "$BASE/val-knn/_refresh" > /dev/null

HITS=$(curl -sf -X GET "$BASE/val-knn/_search" -H 'Content-Type: application/json' \
  -d '{"query":{"knn":{"v":{"vector":[1,2,3],"k":1}}}}' \
  | python3 -c "import sys,json; h=json.load(sys.stdin)['hits']['hits']; print(h[0]['_source']['t'] if h else 'none')")
[[ "$HITS" == "apple" ]] \
  && ok "k-NN returns nearest neighbour correctly (apple for [1,2,3])" \
  || fail "k-NN returned: $HITS (expected apple)"

# ── 6. Agent framework execution ─────────────────────────────────────────────
section "Agent Framework (flow agent + ListIndexTool)"
AGENT_ID=$(curl -sf -X POST "$BASE/_plugins/_ml/agents/_register" \
  -H 'Content-Type: application/json' \
  -d '{"name":"val-agent","type":"flow","tools":[{"type":"ListIndexTool","name":"ListIndexTool","parameters":{}}]}' \
  | python3 -c "import sys,json; print(json.load(sys.stdin)['agent_id'])")
[[ -n "$AGENT_ID" ]] && ok "Agent registered: $AGENT_ID" || fail "Agent registration failed"

EXEC=$(curl -sf -X POST "$BASE/_plugins/_ml/agents/$AGENT_ID/_execute" \
  -H 'Content-Type: application/json' \
  -d '{"parameters":{"question":"list indices"}}')
echo "$EXEC" | python3 -c "import sys,json; r=json.load(sys.stdin); print(r['inference_results'][0]['output'][0]['result'][:80])" \
  && ok "Agent executed ListIndexTool and returned index data" \
  || fail "Agent execution failed"

# ── 7. A2A endpoint ──────────────────────────────────────────────────────────
section "Agent2Agent (A2A) — Abstract §4 claims"
A2A_CODE=$(curl -sf -o /dev/null -w "%{http_code}" "$BASE/_plugins/_ml/a2aservers" 2>/dev/null || echo "000")
if [[ "$A2A_CODE" == "200" ]]; then
  ok "A2A endpoint active in this build"
else
  warn "A2A endpoint absent in 3.0.0 GA (/_plugins/_ml/a2aservers → $A2A_CODE). Abstract §4 describes an open RFC — not yet shipped. Label this clearly in the talk."
fi

# ── 8. Memory API ────────────────────────────────────────────────────────────
section "Memory API — Abstract says requires 3.3+"
MEM_RESP=$(curl -sf "$BASE/_plugins/_ml/memory")
echo "$MEM_RESP" | python3 -c "import sys,json; json.load(sys.stdin)" > /dev/null 2>&1 \
  && warn "Memory API responds in 3.0.0 (abstract says 3.3+). May be base infra; agentic_memory MCP tools still need 3.3+." \
  || fail "Memory API not reachable"

# ── Summary ───────────────────────────────────────────────────────────────────
section "Summary"
echo "  PASS=$PASS  FAIL=$FAIL  WARN=$WARN"
[[ $FAIL -eq 0 ]] && echo "  All hard claims verified." || echo "  Some claims need correction (see FAIL lines above)."
