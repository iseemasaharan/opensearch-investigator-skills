# opensearch-skills

A production-grade OpenSearch agent skills project demonstrating real-time anomaly detection, automated incident investigation, and custom detection tools — built for the talk *"From Alert to Answer: Accelerating Anomaly Investigation with OpenSearch Agent Skills"*.

## What's in this repo

```
opensearch-skills/
├── docker-compose.yml          # Full cluster: OpenSearch, Dashboards, mock-LLM, log generator,
│                               #   seed-data (auto), investigation UI (Streamlit)
├── validate_abstract.sh        # 24-check live validation of every agent skill claim
├── skills/                     # Agent Skills (agentskills.io format)
│   ├── opensearch-anomaly-investigate/
│   ├── opensearch-deploy-correlate/
│   ├── opensearch-baseline-compare/
│   └── opensearch-multi-signal-correlate/
└── solution/
    ├── seed_data.py            # Seeds prod-metrics, prod-logs, deploy-events with a planted incident
    ├── investigate.py          # 5-step colored terminal investigation pipeline
    ├── investigation_ui.py     # Streamlit investigation dashboard (port 8501)
    ├── log_generator.py        # Continuous traffic simulator — auto-injects incidents on a schedule
    ├── mock_llm_server.py      # OpenAI-compatible mock LLM + alerting webhook receiver
    ├── setup_agent.sh          # Registers ML connector, model, and flow agent in OpenSearch
    ├── setup_realtime.sh       # Wires real-time detector → alerting monitor → webhook (run once)
    └── custom_tools/
        ├── deploy_correlator_tool.py     # Deploy suspicion scoring
        ├── baseline_comparator_tool.py   # Slow regression detection vs N-day baseline
        └── multi_signal_tool.py          # 4-signal parallel correlation
```

## Deploy to NetApp Instaclustr (managed OpenSearch)

The OpenSearch cluster runs on Instaclustr; the demo app services (mock-llm, Streamlit UI, log generator) run on a machine you control (EC2 in the same region, or your laptop with ngrok).

### 1. Get your connection details

In the [Instaclustr console](https://console.instaclustr.com):
- Open your cluster → **Connection Info**
- Copy the REST endpoint (host + port) and the `icadmin` username/password

### 2. Configure credentials

```bash
cp instaclustr.env.template instaclustr.env
# Edit instaclustr.env — fill in OPENSEARCH_URL and MOCK_LLM_URL
```

`OPENSEARCH_URL` format: `https://icadmin:<password>@<host>:9200`

`MOCK_LLM_URL` must be a URL that Instaclustr's OpenSearch nodes can reach over the internet:
- **EC2** (recommended): `http://<ec2-public-ip>:8765` — open port 8765 in the security group
- **ngrok** (quick demo): run `ngrok http 8765` and use the `https://xxx.ngrok.io` URL

### 3. Start the app services

```bash
# Load credentials
source instaclustr.env

# Start everything except local OpenSearch (it's on Instaclustr)
docker compose -f docker-compose.yml -f docker-compose.instaclustr.yml up -d
```

### 4. Seed data and register the agent

```bash
source instaclustr.env

# Seed 3 hours of observability data into the Instaclustr cluster
docker run --rm -e OPENSEARCH_URL="$OPENSEARCH_URL" \
  -v "$(pwd)/solution:/app" python:3.11-slim \
  sh -c "pip install opensearch-py -q && python /app/seed_data.py"

# Register ML connector, model, anomaly detector, and flow agent
OPENSEARCH_URL="$OPENSEARCH_URL" MOCK_LLM_URL="$MOCK_LLM_URL" \
  bash solution/setup_agent.sh

# Wire the real-time alerting loop (run once)
OPENSEARCH_URL="$OPENSEARCH_URL" bash solution/setup_realtime.sh
```

### 5. Open the UI

The investigation UI runs locally on **http://localhost:8501**.  
OpenSearch Dashboards is available via the Instaclustr console → **Dashboards URL**.

> `instaclustr.env` and `solution/.ids.env` are gitignored — they contain credentials.

---

## Quick start (local Docker)

```bash
# 1. Start the cluster — data is seeded and incidents fire automatically
docker compose up -d

# 2. Register the ML agent (writes IDs to solution/.ids.env for the UI)
bash solution/setup_agent.sh

# 3. Wire the always-on production loop (run once)
bash solution/setup_realtime.sh
```

Then visit **http://localhost:8501** in your browser.

> The `investigation-ui` container installs packages on first start — allow ~60 seconds before the UI is ready.

No manual Python scripts to trigger.

## Services

| Service | Port | Description |
|---|---|---|
| `opensearch-node1` | 9200 | OpenSearch 3.0.0 single-node cluster |
| `mock-llm` | 8765 | OpenAI-compatible mock LLM + webhook receiver |
| `seed-data` | — | Seeds 3h of historical data + planted incident on startup (exits when done) |
| `log-generator` | — | Continuous traffic simulator; auto-injects incidents every 20 min |
| `investigation-ui` | **8501** | Streamlit investigation dashboard |
| `opensearch-dashboards` | 5601 | OpenSearch Dashboards |

## Streamlit Investigation UI

Open **http://localhost:8501** after `docker compose up -d`.

| Tab | What you see |
|---|---|
| **Overview** | Live health cards per service (P99 latency + error rate), recent anomaly table, log ingestion rate |
| **Anomaly Timeline** | P99 latency + error rate charts with alert thresholds; anomaly grade bar chart |
| **Log Analysis** | Stacked error/warn bar chart by service, recent error log table, exception pattern frequency |
| **Deploy Correlator** | Suspicion-scored deploy table + bar chart; top suspect highlighted in red banner |
| **Investigation** | One-click AI root cause report via the flow agent; webhook fire button for async path testing |

The sidebar auto-loads `DETECTOR_ID` and `AGENT_ID` from `solution/.ids.env` (written by `setup_agent.sh`) — no copy-paste needed.

## Auto-incident schedule

Once `docker compose up -d` runs, incidents fire on their own:

```
T+0s    seed-data seeds 3h of history including a pre-planted incident → exits
T+120s  log-generator auto-starts INCIDENT #1 (payment-service spikes)
T+420s  incident ends — back to normal traffic
T+1620s INCIDENT #2 fires … and so on every 20 min
```

To trigger a manual incident at any time:
```bash
docker exec log-generator touch /tmp/incident_mode
docker exec log-generator rm   /tmp/incident_mode   # end it
```

## Always-on production loop

Once `setup_realtime.sh` runs, no manual scripts are needed:

```
log-generator (Docker, restart: unless-stopped)
    │  every 30s
    ▼
prod-metrics / prod-logs (OpenSearch indices)
    │
    ▼
Real-time anomaly detector  (checks every ~1 min)
    │  grade > 0.7
    ▼
Alerting monitor  (polls every 1 min)
    │  fires webhook
    ▼
mock-llm /trigger-investigation
    │  async
    ▼
Investigation agent  →  root cause report  →  docker logs mock-llm
```

Watch the auto-investigation fire:
```bash
docker logs -f mock-llm
```

## Agent Skills

Skills follow the [agentskills.io](https://agentskills.io) open format — each is a folder with a `SKILL.md` describing what the skill does, inputs, outputs, and how to invoke it.

| Skill | Description |
|---|---|
| [`opensearch-anomaly-investigate`](skills/opensearch-anomaly-investigate/SKILL.md) | Full end-to-end incident investigation: anomaly → logs → deploys → LLM report |
| [`opensearch-deploy-correlate`](skills/opensearch-deploy-correlate/SKILL.md) | Score recent deploys by proximity and risk to identify the likely root cause |
| [`opensearch-baseline-compare`](skills/opensearch-baseline-compare/SKILL.md) | Catch slow regressions the adaptive ML detector has learned to ignore |
| [`opensearch-multi-signal-correlate`](skills/opensearch-multi-signal-correlate/SKILL.md) | Run 4 detection signals in parallel, get one confidence score |

## Custom tools

| Tool | File | Solves |
|---|---|---|
| `DeployCorrelatorTool` | `custom_tools/deploy_correlator_tool.py` | Deploys cause ~70% of incidents — score them first |
| `BaselineComparisonTool` | `custom_tools/baseline_comparator_tool.py` | Slow regressions the adaptive ML model never fires on |
| `MultiSignalCorrelatorTool` | `custom_tools/multi_signal_tool.py` | 4 parallel signals → one confidence score in < 500 ms |

## Dashboards

| View | URL |
|---|---|
| **Streamlit UI** | http://localhost:8501 |
| Anomaly Detection | http://localhost:5601/app/anomaly-detection-dashboards |
| Alerting monitors | http://localhost:5601/app/alerting#/monitors |
| Discover (logs) | http://localhost:5601/app/discover |

## Validated claims

`validate_abstract.sh` tests every tool claim against a live OpenSearch 3.0.0 cluster:

```
24 PASS  0 FAIL  3 WARN
```

WARN items are version-specific nuances (A2A protocol RFC not shipped in 3.0.0 GA, tool names differ slightly from docs).
