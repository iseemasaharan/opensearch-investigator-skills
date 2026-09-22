"""
Investigation demo — "From Alert to Answer"

Runs each tool in the pipeline individually (for talk clarity),
then executes the full flow agent in one call, and prints a side-by-side
comparison showing what the agent resolved automatically.

Usage:
    python investigate.py [--agent-only] [--step-by-step]
"""

import argparse
import json
import os
import sys
import time
from datetime import datetime, timedelta, timezone

import requests

BASE = os.environ.get("OPENSEARCH_URL", "http://localhost:9200").rstrip("/")
NOW = datetime.now(timezone.utc)

# ── ANSI colours ──────────────────────────────────────────────────────────────

R = "\033[0;31m"
G = "\033[0;32m"
Y = "\033[1;33m"
B = "\033[0;34m"
C = "\033[0;36m"
M = "\033[0;35m"
DIM = "\033[2m"
BOLD = "\033[1m"
NC = "\033[0m"

BANNER = f"""{B}
┌─────────────────────────────────────────────────────────────────┐
│  {R}{BOLD}⬤  ANOMALY DETECTED{NC}{B}  ·  detector: prod-latency-p99              │
│  {Y}severity: HIGH{B}  ·  service: payment-service                    │
│  {DIM}Handing off to investigation agent …{NC}{B}                        │
└─────────────────────────────────────────────────────────────────┘{NC}"""


def ms_to_iso(ms: int) -> str:
    return datetime.fromtimestamp(ms / 1000, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def call(method: str, path: str, body=None):
    kwargs = {"timeout": 30}
    if body:
        kwargs["json"] = body
    r = requests.request(method, f"{BASE}{path}", **kwargs)
    try:
        return r.json()
    except Exception:
        return {"_raw": r.text}


def step_header(n: int, title: str, tool: str):
    print(f"\n{C}{'─'*65}{NC}")
    print(f"{BOLD}  Step {n}: {title}{NC}  {DIM}[{tool}]{NC}")
    print(f"{C}{'─'*65}{NC}")


def print_hits(hits: list, fields: list, max_rows: int = 8):
    if not hits:
        print(f"  {DIM}(no results){NC}")
        return
    widths = [max(len(f), max(len(str(h.get("_source", {}).get(f, ""))) for h in hits[:max_rows]))
              for f in fields]
    header = "  " + "  ".join(f.ljust(w) for f, w in zip(fields, widths))
    print(f"{DIM}{header}{NC}")
    print(f"  {DIM}{'  '.join('─'*w for w in widths)}{NC}")
    for hit in hits[:max_rows]:
        src = hit.get("_source", {})
        row = "  " + "  ".join(str(src.get(f, "—"))[:w].ljust(w) for f, w in zip(fields, widths))
        level = src.get("level", "")
        colour = R if level == "ERROR" else (Y if level == "WARN" else NC)
        print(f"{colour}{row}{NC}")
    if len(hits) > max_rows:
        print(f"  {DIM}… {len(hits) - max_rows} more rows{NC}")


def load_ids() -> dict:
    ids = {}
    env_file = "/tmp/opensearch_demo_ids.env"
    if os.path.exists(env_file):
        for line in open(env_file):
            line = line.strip()
            if "=" in line:
                k, v = line.split("=", 1)
                ids[k] = v
    # also check environment variables
    for k in ("DETECTOR_ID", "MODEL_ID", "AGENT_ID", "WINDOW_START", "WINDOW_END"):
        if k in os.environ:
            ids[k] = os.environ[k]
    return ids


# ── Investigation steps ───────────────────────────────────────────────────────

def step1_get_detector(ids: dict) -> dict:
    step_header(1, "Find Anomaly Detector", "SearchAnomalyDetectorsTool")
    resp = call("GET", f"/_plugins/_anomaly_detection/detectors/{ids['DETECTOR_ID']}")
    detector = resp.get("anomaly_detector", {})
    print(f"  {G}Detector:{NC} {detector.get('name', ids['DETECTOR_ID'])}")
    print(f"  {G}Indices: {NC} {detector.get('indices', ['prod-metrics'])}")
    feats = detector.get("feature_attributes", [])
    for f in feats:
        print(f"  {G}Feature: {NC} {f.get('feature_name')}")
    return detector


def step2_get_anomaly_results(ids: dict) -> list:
    step_header(2, "Fetch Anomaly Results", "SearchAnomalyResultsTool")

    # Search the anomaly results index directly (tool equivalent)
    query = {
        "query": {
            "bool": {
                "must": [{"term": {"detector_id": ids["DETECTOR_ID"]}}],
                "filter": [{"range": {"data_start_time": {
                    "gte": int(ids["WINDOW_START"]),
                    "lte": int(ids["WINDOW_END"])
                }}}]
            }
        },
        "sort": [{"anomaly_grade": "desc"}],
        "size": 5,
    }
    resp = call("POST", "/.opendistro-anomaly-results*/_search", query)
    hits = resp.get("hits", {}).get("hits", [])

    if hits:
        print(f"  {R}Found {len(hits)} anomaly result(s):{NC}")
        for h in hits[:3]:
            src = h["_source"]
            grade = src.get("anomaly_grade", 0)
            score = src.get("anomaly_score", 0)
            ts = ms_to_iso(src.get("data_end_time", int(ids["WINDOW_END"])))
            print(f"  {R}●{NC} grade={grade:.3f}  score={score:.3f}  at {ts}")
    else:
        # Detector may still be running historical analysis — show live metric state instead
        print(f"  {Y}Anomaly results still processing (historical analysis running).{NC}")
        print(f"  {Y}Showing latest metric spike instead:{NC}")
        metric_q = {
            "query": {"bool": {"must": [{"term": {"service": "payment-service"}}],
                                "filter": [{"range": {"@timestamp": {"gte": "now-10m"}}}]}},
            "sort": [{"p99_latency_ms": "desc"}],
            "size": 3,
            "_source": ["@timestamp", "p99_latency_ms", "error_rate_pct"],
        }
        mresp = call("POST", "/prod-metrics/_search", metric_q)
        mhits = mresp.get("hits", {}).get("hits", [])
        print_hits(mhits, ["@timestamp", "p99_latency_ms", "error_rate_pct"])

    return hits


def step3_query_error_logs() -> list:
    step_header(3, "Correlate Error Logs", "SearchIndexTool → prod-logs")
    query = {
        "query": {
            "bool": {
                "must": [
                    {"term": {"service": "payment-service"}},
                    {"terms": {"level": ["ERROR", "WARN"]}},
                ],
                "filter": [{"range": {"@timestamp": {"gte": "now-10m"}}}],
            }
        },
        "size": 20,
        "sort": [{"@timestamp": "desc"}],
        "_source": ["@timestamp", "level", "message", "http_status", "duration_ms"],
    }
    resp = call("POST", "/prod-logs/_search", query)
    hits = resp.get("hits", {}).get("hits", [])
    total = resp.get("hits", {}).get("total", {}).get("value", 0)
    print(f"  {R}{total} ERROR/WARN entries in last 10 minutes{NC}")
    print_hits(hits, ["@timestamp", "level", "http_status", "duration_ms", "message"])

    # Count by message pattern
    msg_counts: dict = {}
    for h in resp.get("hits", {}).get("hits", []):
        msg = h["_source"].get("message", "")
        # Normalise: collapse specifics
        key = msg.split(" at line")[0].split(" after ")[0]
        msg_counts[key] = msg_counts.get(key, 0) + 1

    if msg_counts:
        print(f"\n  {BOLD}Pattern frequencies:{NC}")
        for msg, cnt in sorted(msg_counts.items(), key=lambda x: -x[1])[:5]:
            bar = "█" * min(cnt, 20)
            print(f"  {Y}{cnt:3d}{NC}  {bar}  {DIM}{msg[:70]}{NC}")

    return hits


def step4_query_deploys() -> list:
    step_header(4, "Check Recent Deploys", "SearchIndexTool → deploy-events")
    query = {
        "query": {"range": {"@timestamp": {"gte": "now-60m"}}},
        "size": 10,
        "sort": [{"@timestamp": "desc"}],
        "_source": ["@timestamp", "service", "version_from", "version_to", "deployed_by", "changes", "status"],
    }
    resp = call("POST", "/deploy-events/_search", query)
    hits = resp.get("hits", {}).get("hits", [])
    total = resp.get("hits", {}).get("total", {}).get("value", 0)
    print(f"  {total} deploy(s) in the past 60 minutes:")
    print_hits(hits, ["@timestamp", "service", "version_to", "deployed_by", "status"])

    if hits:
        culprit = hits[0]["_source"]
        if culprit["service"] == "payment-service":
            print(f"\n  {R}{BOLD}⚠  Suspicious:{NC} payment-service was deployed "
                  f"{culprit['version_from']} → {culprit['version_to']} by {culprit['deployed_by']}")
            print(f"  {DIM}Changes: {culprit.get('changes','')[:100]}{NC}")

    return hits


def step5_synthesize(anomaly_hits, log_hits, deploy_hits, ids: dict) -> str:
    step_header(5, "Synthesise Investigation Report", "MLModelTool → mock-llm")

    # Build context string for the LLM
    def hits_to_text(hits: list, key_fields: list) -> str:
        lines = []
        for h in hits[:10]:
            src = h.get("_source", {})
            lines.append(", ".join(f"{f}={src.get(f,'')}" for f in key_fields))
        return "\n".join(lines) if lines else "(none)"

    anomaly_text = hits_to_text(anomaly_hits,
        ["data_end_time", "anomaly_grade", "anomaly_score"]) or \
        "anomaly_grade=0.91, anomaly_score=4.2, detector_name=prod-latency-p99"
    log_text = hits_to_text(log_hits,
        ["@timestamp", "level", "message", "http_status", "duration_ms"])
    deploy_text = hits_to_text(deploy_hits,
        ["@timestamp", "service", "version_from", "version_to", "deployed_by", "changes"])

    prompt = (
        "You are an SRE on-call assistant. Anomaly detector prod-latency-p99 just fired with HIGH severity.\n\n"
        f"[ANOMALY RESULTS]\n{anomaly_text}\n\n"
        f"[ERROR LOGS]\n{log_text}\n\n"
        f"[RECENT DEPLOYS]\n{deploy_text}\n\n"
        "Provide: root cause assessment, ranked evidence, and recommended action."
    )

    # Call mock LLM via model
    resp = call("POST", f"/_plugins/_ml/models/{ids['MODEL_ID']}/_predict", {
        "parameters": {"prompt": prompt}
    })

    report = ""
    # Try multiple response shapes
    for path in [
        lambda r: r["inference_results"][0]["output"][0]["dataAsMap"]["choices"][0]["message"]["content"],
        lambda r: r["inference_results"][0]["output"][0]["result"],
        lambda r: json.dumps(r, indent=2),
    ]:
        try:
            report = path(resp)
            break
        except (KeyError, IndexError, TypeError):
            continue

    # Pretty-print the report
    print()
    for line in report.split("\n"):
        if line.startswith("##"):
            print(f"\n{BOLD}{B}{line}{NC}")
        elif line.startswith("###"):
            print(f"\n{BOLD}{C}{line}{NC}")
        elif line.startswith("**") and line.endswith("**"):
            print(f"{BOLD}{line}{NC}")
        elif "Root Cause" in line or "CRITICAL" in line:
            print(f"{R}{line}{NC}")
        elif line.startswith("- P99") or line.startswith("- **"):
            print(f"{Y}{line}{NC}")
        elif line.startswith("1.") or line.startswith("2.") or line.startswith("3."):
            print(f"{G}{line}{NC}")
        else:
            print(f"{DIM if line.startswith('*Generated') else ''}{line}{NC}")

    return report


def step_full_agent(ids: dict):
    """Shows the one-liner: execute the registered flow agent end-to-end."""
    print(f"\n{C}{'─'*65}{NC}")
    print(f"{BOLD}  Bonus: Full pipeline as ONE agent call{NC}  {DIM}[flow agent]{NC}")
    print(f"{C}{'─'*65}{NC}")
    print(f"  {DIM}POST /_plugins/_ml/agents/{ids['AGENT_ID']}/_execute{NC}")
    print(f"  {DIM}  body: {{\"parameters\": {{\"question\": \"investigate prod-latency-p99 alert\"}}}}{NC}\n")

    resp = call("POST", f"/_plugins/_ml/agents/{ids['AGENT_ID']}/_execute", {
        "parameters": {"question": "investigate prod-latency-p99 alert"}
    })

    outputs = resp.get("inference_results", [{}])[0].get("output", [])
    if outputs:
        print(f"  {G}Agent returned {len(outputs)} tool output(s):{NC}")
        for out in outputs:
            name = out.get("name", "?")
            # synthesize_report comes back as raw dataAsMap (no post_process_function)
            dam = out.get("dataAsMap", {})
            if dam:
                content = ""
                try:
                    content = dam["choices"][0]["message"]["content"]
                except (KeyError, IndexError, TypeError):
                    content = json.dumps(dam)[:300]
                print(f"\n  {Y}[{name}]{NC}")
                for line in content.split("\n")[:12]:
                    print(f"    {DIM}{line}{NC}")
            else:
                result = str(out.get("result", ""))[:300]
                print(f"  {Y}{name}:{NC} {DIM}{result}{NC}")
    else:
        print(f"  {DIM}(agent response: {str(resp)[:300]}){NC}")


# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--agent-only", action="store_true",
                        help="Skip step-by-step and only run the flow agent")
    parser.add_argument("--step-by-step", action="store_true", default=False,
                        help="Pause between steps (for live demos)")
    args = parser.parse_args()

    ids = load_ids()
    missing = [k for k in ("DETECTOR_ID", "MODEL_ID", "AGENT_ID", "WINDOW_START", "WINDOW_END")
               if k not in ids]
    if missing:
        print(f"{R}Missing IDs: {missing}. Run setup_agent.sh first.{NC}")
        sys.exit(1)

    print(BANNER)
    print(f"  {DIM}Investigation window: {ms_to_iso(int(ids['WINDOW_START']))} → {ms_to_iso(int(ids['WINDOW_END']))}{NC}")

    if args.step_by_step:
        input(f"\n  {DIM}Press ENTER to start investigation …{NC}")

    if not args.agent_only:
        detector = step1_get_detector(ids)
        if args.step_by_step:
            input(f"\n  {DIM}Press ENTER for next step …{NC}")

        anomaly_hits = step2_get_anomaly_results(ids)
        if args.step_by_step:
            input(f"\n  {DIM}Press ENTER for next step …{NC}")

        log_hits = step3_query_error_logs()
        if args.step_by_step:
            input(f"\n  {DIM}Press ENTER for next step …{NC}")

        deploy_hits = step4_query_deploys()
        if args.step_by_step:
            input(f"\n  {DIM}Press ENTER for next step …{NC}")

        step5_synthesize(anomaly_hits, log_hits, deploy_hits, ids)

    step_full_agent(ids)

    print(f"\n{C}{'─'*65}{NC}")
    print(f"{G}{BOLD}  Investigation complete.{NC}  Time from alert to answer: {DIM}~4s{NC}")
    print(f"  {DIM}(vs. ~20 min manual triage){NC}")
    print(f"{C}{'─'*65}{NC}\n")


if __name__ == "__main__":
    main()
