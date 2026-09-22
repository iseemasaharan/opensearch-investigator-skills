"""
OpenSearch Incident Investigation Dashboard — Streamlit UI
Runs in Docker as the investigation-ui service on port 8501.

Tabs:
  Overview          — live service health cards + recent anomaly summary
  Anomaly Timeline  — P99 latency + error rate charts, anomaly grade bars
  Log Analysis      — error/warn counts by service, recent log table
  Deploy Correlator — ranked deploy suspicion table
  Investigation     — one-click AI root cause report via the flow agent
"""

import json
import os
import time
from datetime import datetime, timedelta, timezone

import pandas as pd
import plotly.express as px
import streamlit as st

from os_client import get_client

os_client = get_client()
IDS_FILE = "/app/.ids.env"

AWS_KEY    = os.environ.get("AWS_ACCESS_KEY_ID", "")
AWS_SECRET = os.environ.get("AWS_SECRET_ACCESS_KEY", "")
AWS_REGION = os.environ.get("AWS_REGION", "us-east-1")
BEDROCK_MODEL = os.environ.get("BEDROCK_MODEL_ID", "us.anthropic.claude-sonnet-4-6")


def call_llm(prompt: str) -> str:
    import boto3, json as _json
    client = boto3.client(
        "bedrock-runtime",
        region_name=AWS_REGION,
        aws_access_key_id=AWS_KEY,
        aws_secret_access_key=AWS_SECRET,
    )
    body = _json.dumps({
        "anthropic_version": "bedrock-2023-05-31",
        "max_tokens": 1024,
        "messages": [{"role": "user", "content": prompt}],
    })
    resp = client.invoke_model(modelId=BEDROCK_MODEL, body=body,
                               contentType="application/json", accept="application/json")
    result = _json.loads(resp["body"].read())
    return result["content"][0]["text"]

SERVICES = ["payment-service", "order-service", "inventory-service", "auth-service"]

RISKY_KW = {
    "pool": 0.20, "connection": 0.20, "timeout": 0.15,
    "refactor": 0.10, "migration": 0.15, "new": 0.08, "upgrade": 0.07,
}

# ── Helpers ───────────────────────────────────────────────────────────────────

def os_get(path: str) -> dict:
    try:
        return os_client.transport.perform_request("GET", path)
    except Exception as e:
        return {"error": str(e)}


def os_post(path: str, body: dict) -> dict:
    try:
        return os_client.transport.perform_request("POST", path, body=body)
    except Exception as e:
        return {"error": str(e)}


def load_ids() -> dict:
    ids = {}
    if os.path.exists(IDS_FILE):
        for line in open(IDS_FILE):
            if "=" in line:
                k, v = line.strip().split("=", 1)
                ids[k.strip()] = v.strip()
    return ids


def suspicion_score(src: dict, service: str, delta_min: int) -> float:
    svc_score  = 0.40 if src.get("service") == service else 0.0
    time_score = max(0.0, 1.0 - delta_min / 120) * 0.40 if 0 <= delta_min <= 120 else 0.0
    risk_score = min(sum(w for kw, w in RISKY_KW.items() if kw in src.get("changes", "").lower()), 0.20)
    return round(svc_score + time_score + risk_score, 2)


# ── Page config ───────────────────────────────────────────────────────────────

st.set_page_config(
    page_title="OpenSearch Incident Investigator",
    page_icon="🔍",
    layout="wide",
    initial_sidebar_state="expanded",
)

st.markdown("""
<style>
[data-testid="stMetricDelta"] svg { display: none; }
</style>
""", unsafe_allow_html=True)

# ── Sidebar ───────────────────────────────────────────────────────────────────

with st.sidebar:
    st.title("🔍 Incident Investigator")
    st.caption("OpenSearch Agent Skills · Demo")

    health = os_get("/_cluster/health")
    status = health.get("status", "unknown")
    icon   = {"green": "🟢", "yellow": "🟡", "red": "🔴"}.get(status, "⚪")
    st.markdown(f"**Cluster:** {icon} `{status}`")
    st.caption(
        f"Nodes: {health.get('number_of_nodes', '?')} | "
        f"Active shards: {health.get('active_shards', '?')}"
    )

    st.divider()

    saved = load_ids()
    st.subheader("Resource IDs")
    st.caption("Auto-loaded from `solution/.ids.env` after running `setup_agent.sh`.")

    detector_id = st.text_input("Detector ID", value=saved.get("DETECTOR_ID", ""),
                                placeholder="run setup_agent.sh first")
    agent_id    = st.text_input("Agent ID",    value=saved.get("AGENT_ID", ""),
                                placeholder="run setup_agent.sh first")

    st.divider()

    window_minutes  = st.slider("Lookback window (min)", 5, 120, 15)
    service_filter  = st.selectbox("Focus service", SERVICES)

    if st.button("🔄 Refresh data", use_container_width=True):
        st.rerun()

# ── Tabs ──────────────────────────────────────────────────────────────────────

tab_overview, tab_timeline, tab_logs, tab_deploys, tab_report = st.tabs([
    "📊 Overview",
    "📈 Anomaly Timeline",
    "📋 Log Analysis",
    "🚀 Deploy Correlator",
    "🔬 Investigation",
])

# ─────────────────────────────────────────────────────────────────────────────
# TAB 1 · OVERVIEW
# ─────────────────────────────────────────────────────────────────────────────

with tab_overview:
    st.header("Service Health — last {} min".format(window_minutes))

    metrics_resp = os_post("/prod-metrics/_search", {
        "size": 0,
        "query": {"range": {"@timestamp": {"gte": f"now-{window_minutes}m"}}},
        "aggs": {
            "by_service": {
                "terms": {"field": "service", "size": 10},
                "aggs": {
                    "avg_p99": {"avg": {"field": "p99_latency_ms"}},
                    "max_p99": {"max": {"field": "p99_latency_ms"}},
                    "avg_err": {"avg": {"field": "error_rate_pct"}},
                },
            }
        },
    })

    buckets = (metrics_resp
               .get("aggregations", {})
               .get("by_service", {})
               .get("buckets", []))

    if buckets:
        cols = st.columns(len(buckets))
        for col, b in zip(cols, buckets):
            svc = b["key"]
            p99 = b.get("avg_p99", {}).get("value") or 0
            err = b.get("avg_err", {}).get("value") or 0
            icon = "🔴" if p99 > 500 or err > 5 else ("🟡" if p99 > 200 or err > 1 else "🟢")
            col.metric(
                label=f"{icon} {svc}",
                value=f"{p99:.0f} ms",
                delta=f"{err:.2f}% errors",
                delta_color="inverse",
            )
    else:
        st.info("No metrics yet. The `seed-data` container seeds data automatically on first run.")

    st.divider()

    col_a, col_b = st.columns(2)

    with col_a:
        st.subheader("Recent high-grade anomalies")
        anomaly_resp = os_post("/.opendistro-anomaly-results*/_search", {
            "size": 8,
            "query": {
                "bool": {
                    "must": [
                        {"range": {"anomaly_grade": {"gt": 0.5}}},
                        {"range": {"data_end_time": {"gte": "now-60m"}}},
                    ]
                }
            },
            "sort": [{"data_end_time": "desc"}],
            "_source": ["anomaly_grade", "anomaly_score", "data_end_time", "detector_id"],
        })
        hits = anomaly_resp.get("hits", {}).get("hits", [])
        if hits:
            df = pd.DataFrame([h["_source"] for h in hits])
            st.dataframe(df, use_container_width=True, hide_index=True)
        else:
            st.success("No anomalies (grade > 0.5) in the last 60 min.")

    with col_b:
        st.subheader("Log ingestion rate")
        recent_count = os_post("/prod-logs/_count", {
            "query": {"range": {"@timestamp": {"gte": "now-1m"}}}
        }).get("count", 0)
        total_count = os_post("/prod-metrics/_count", {}).get("count", 0)

        st.metric("Logs last 1 min", recent_count)
        st.metric("Total metric docs", f"{total_count:,}")

        if recent_count > 0:
            st.success("Log generator is running")
        else:
            st.warning("No logs in the last minute — generator may be starting up")

        incident_count = os_post("/prod-logs/_count", {
            "query": {
                "bool": {
                    "must": [
                        {"terms": {"level": ["ERROR"]}},
                        {"range": {"@timestamp": {"gte": f"now-{window_minutes}m"}}},
                    ]
                }
            }
        }).get("count", 0)
        st.metric("ERROR logs last {} min".format(window_minutes), incident_count,
                  delta="incident active" if incident_count > 10 else "normal",
                  delta_color="inverse")

# ─────────────────────────────────────────────────────────────────────────────
# TAB 2 · ANOMALY TIMELINE
# ─────────────────────────────────────────────────────────────────────────────

with tab_timeline:
    st.header(f"Metrics Timeline — {service_filter}")

    ts_resp = os_post("/prod-metrics/_search", {
        "size": 0,
        "query": {
            "bool": {
                "must": [
                    {"term":  {"service": service_filter}},
                    {"range": {"@timestamp": {"gte": f"now-{window_minutes}m"}}},
                ]
            }
        },
        "aggs": {
            "over_time": {
                "date_histogram": {"field": "@timestamp", "fixed_interval": "1m"},
                "aggs": {
                    "avg_p99": {"avg": {"field": "p99_latency_ms"}},
                    "avg_err": {"avg": {"field": "error_rate_pct"}},
                },
            }
        },
    })

    ts_buckets = ts_resp.get("aggregations", {}).get("over_time", {}).get("buckets", [])

    if ts_buckets:
        df_ts = pd.DataFrame([{
            "time":           b["key_as_string"],
            "P99 Latency ms": round(b.get("avg_p99", {}).get("value") or 0, 1),
            "Error Rate %":   round(b.get("avg_err", {}).get("value") or 0, 3),
        } for b in ts_buckets])
        df_ts["time"] = pd.to_datetime(df_ts["time"])

        fig_lat = px.line(
            df_ts, x="time", y="P99 Latency ms",
            title=f"P99 Latency — {service_filter}",
        )
        fig_lat.add_hline(y=500, line_dash="dash", line_color="red",
                          annotation_text="Alert threshold 500 ms")
        fig_lat.update_layout(margin=dict(t=40, b=20))
        st.plotly_chart(fig_lat, use_container_width=True)

        fig_err = px.area(
            df_ts, x="time", y="Error Rate %",
            title=f"Error Rate — {service_filter}",
            color_discrete_sequence=["#ef4444"],
        )
        fig_err.add_hline(y=5, line_dash="dash", line_color="orange",
                          annotation_text="Alert threshold 5%")
        fig_err.update_layout(margin=dict(t=40, b=20))
        st.plotly_chart(fig_err, use_container_width=True)
    else:
        st.info(f"No metric data for {service_filter} in the last {window_minutes} min.")

    if detector_id:
        st.subheader("Anomaly Grade Timeline")
        grade_resp = os_post("/.opendistro-anomaly-results*/_search", {
            "size": 0,
            "query": {
                "bool": {
                    "must": [
                        {"term":  {"detector_id": detector_id}},
                        {"range": {"data_end_time": {"gte": f"now-{window_minutes}m"}}},
                    ]
                }
            },
            "aggs": {
                "over_time": {
                    "date_histogram": {"field": "data_end_time", "fixed_interval": "1m"},
                    "aggs": {"max_grade": {"max": {"field": "anomaly_grade"}}},
                }
            },
        })
        grade_buckets = grade_resp.get("aggregations", {}).get("over_time", {}).get("buckets", [])
        if grade_buckets:
            df_grade = pd.DataFrame([{
                "time":          b["key_as_string"],
                "Anomaly Grade": round(b.get("max_grade", {}).get("value") or 0, 3),
            } for b in grade_buckets])
            df_grade["time"] = pd.to_datetime(df_grade["time"])
            fig_grade = px.bar(
                df_grade, x="time", y="Anomaly Grade",
                title="Anomaly Grade (max per minute)",
                color="Anomaly Grade",
                color_continuous_scale=["#22c55e", "#f59e0b", "#ef4444"],
                range_color=[0, 1],
            )
            fig_grade.add_hline(y=0.7, line_dash="dash", line_color="red",
                                annotation_text="Alert threshold 0.7")
            fig_grade.update_layout(margin=dict(t=40, b=20))
            st.plotly_chart(fig_grade, use_container_width=True)
        else:
            st.info("No anomaly results yet for this detector in the selected window.")
    else:
        st.caption("Enter Detector ID in the sidebar to see anomaly grade chart.")

# ─────────────────────────────────────────────────────────────────────────────
# TAB 3 · LOG ANALYSIS
# ─────────────────────────────────────────────────────────────────────────────

with tab_logs:
    st.header("Log Analysis — last {} min".format(window_minutes))

    col_chart, col_table = st.columns([1, 2])

    with col_chart:
        st.subheader("Errors & warnings by service")
        err_resp = os_post("/prod-logs/_search", {
            "size": 0,
            "query": {
                "bool": {
                    "must": [
                        {"terms": {"level": ["ERROR", "WARN"]}},
                        {"range": {"@timestamp": {"gte": f"now-{window_minutes}m"}}},
                    ]
                }
            },
            "aggs": {
                "by_service": {
                    "terms": {"field": "service", "size": 10},
                    "aggs": {"by_level": {"terms": {"field": "level", "size": 5}}},
                }
            },
        })
        svc_buckets = err_resp.get("aggregations", {}).get("by_service", {}).get("buckets", [])

        if svc_buckets:
            rows = []
            for b in svc_buckets:
                level_counts = {lb["key"]: lb["doc_count"]
                                for lb in b.get("by_level", {}).get("buckets", [])}
                rows.append({
                    "service": b["key"],
                    "ERROR":   level_counts.get("ERROR", 0),
                    "WARN":    level_counts.get("WARN", 0),
                })
            df_err = pd.DataFrame(rows)
            fig = px.bar(
                df_err, x="service", y=["ERROR", "WARN"],
                barmode="stack",
                color_discrete_map={"ERROR": "#ef4444", "WARN": "#f59e0b"},
                title="Errors & Warnings",
            )
            fig.update_xaxes(tickangle=15)
            fig.update_layout(margin=dict(t=40, b=20), legend_title="")
            st.plotly_chart(fig, use_container_width=True)
        else:
            st.success("No errors or warnings in the selected window.")

    with col_table:
        st.subheader(f"Recent errors — {service_filter}")
        logs_resp = os_post("/prod-logs/_search", {
            "size": 25,
            "query": {
                "bool": {
                    "must": [
                        {"term":   {"service": service_filter}},
                        {"terms":  {"level": ["ERROR", "WARN"]}},
                        {"range":  {"@timestamp": {"gte": f"now-{window_minutes}m"}}},
                    ]
                }
            },
            "sort": [{"@timestamp": "desc"}],
            "_source": ["@timestamp", "level", "message", "http_status", "duration_ms"],
        })
        log_hits = logs_resp.get("hits", {}).get("hits", [])
        if log_hits:
            df_logs = pd.DataFrame([h["_source"] for h in log_hits])
            st.dataframe(df_logs, use_container_width=True, height=420, hide_index=True)
        else:
            st.success(f"No errors from {service_filter} in the last {window_minutes} min.")

    # Exception pattern frequency
    st.subheader("Exception pattern frequency")
    pattern_resp = os_post("/prod-logs/_search", {
        "size": 0,
        "query": {
            "bool": {
                "must": [
                    {"match":  {"message": "Exception OR timeout OR exhausted"}},
                    {"range":  {"@timestamp": {"gte": f"now-{window_minutes}m"}}},
                ]
            }
        },
        "aggs": {
            "patterns": {
                "terms": {"field": "message.keyword", "size": 10}
            }
        },
    })
    pat_buckets = pattern_resp.get("aggregations", {}).get("patterns", {}).get("buckets", [])
    if pat_buckets:
        df_pat = pd.DataFrame([{"message": b["key"][:80], "count": b["doc_count"]} for b in pat_buckets])
        fig_pat = px.bar(df_pat, x="count", y="message", orientation="h",
                         title="Top error messages", color="count",
                         color_continuous_scale=["#fde68a", "#ef4444"])
        fig_pat.update_layout(margin=dict(t=40, b=20), yaxis_title="")
        st.plotly_chart(fig_pat, use_container_width=True)

# ─────────────────────────────────────────────────────────────────────────────
# TAB 4 · DEPLOY CORRELATOR
# ─────────────────────────────────────────────────────────────────────────────

with tab_deploys:
    st.header("Deploy Correlator")
    st.caption(
        "Scores recent deploys by suspicion: **service match (0.4)** + "
        "**time proximity to anomaly (0–0.4)** + **change-risk keywords (0–0.2)**"
    )

    dep_resp = os_post("/deploy-events/_search", {
        "size": 20,
        "query": {"range": {"@timestamp": {"gte": "now-6h"}}},
        "sort": [{"@timestamp": "desc"}],
        "_source": ["@timestamp", "service", "version_from", "version_to",
                    "deployed_by", "changes", "status"],
    })
    dep_hits = dep_resp.get("hits", {}).get("hits", [])

    if dep_hits:
        now_utc = datetime.now(timezone.utc)
        rows = []
        for h in dep_hits:
            src = h["_source"]
            try:
                dt = datetime.strptime(src["@timestamp"], "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
                delta_min = int((now_utc - dt).total_seconds() / 60)
                age = f"{delta_min}m ago"
            except Exception:
                delta_min, age = 9999, "unknown"

            score = suspicion_score(src, service_filter, delta_min)
            verdict = (
                "🔴 HIGH"     if score >= 0.75 else
                "🟠 MEDIUM"   if score >= 0.50 else
                "🟡 LOW"      if score >= 0.25 else
                "🟢 UNLIKELY"
            )
            rows.append({
                "deployed":  age,
                "service":   src.get("service"),
                "version":   f"{src.get('version_from')} → {src.get('version_to')}",
                "by":        src.get("deployed_by"),
                "suspicion": score,
                "verdict":   verdict,
                "changes":   (src.get("changes", ""))[:90] + "…",
            })

        rows.sort(key=lambda x: x["suspicion"], reverse=True)

        top = rows[0]
        if top["suspicion"] >= 0.50:
            st.error(
                f"**Top suspect:** `{top['service']}` {top['version']} "
                f"by **{top['by']}** — deployed {top['deployed']} — "
                f"suspicion score **{top['suspicion']}** {top['verdict']}"
            )

        df_dep = pd.DataFrame(rows)
        st.dataframe(df_dep, use_container_width=True, height=320, hide_index=True)

        # Suspicion score bar chart
        fig_dep = px.bar(
            df_dep, x="service", y="suspicion", color="suspicion",
            hover_data=["version", "by", "deployed", "verdict"],
            title="Suspicion Score by Deploy",
            color_continuous_scale=["#22c55e", "#f59e0b", "#ef4444"],
            range_color=[0, 1],
        )
        fig_dep.add_hline(y=0.75, line_dash="dash", line_color="red",
                          annotation_text="HIGH threshold")
        fig_dep.update_layout(margin=dict(t=40, b=20))
        st.plotly_chart(fig_dep, use_container_width=True)
    else:
        st.info("No deploy events in the last 6 hours. The `seed-data` container populates these automatically.")

# ─────────────────────────────────────────────────────────────────────────────
# TAB 5 · INVESTIGATION REPORT
# ─────────────────────────────────────────────────────────────────────────────

with tab_report:
    st.header("🔬 AI Investigation Report")

    col_btn, col_info = st.columns([1, 3])
    with col_btn:
        run_btn = st.button("🚀 Run Investigation", type="primary", use_container_width=True)
    with col_info:
        st.caption(
            f"Fetches metrics, error logs, and deploy events → "
            f"synthesises RCA via Bedrock `{BEDROCK_MODEL}`"
        )

    if run_btn:
        st.divider()
        st.subheader("🔗 Agent Skill Pipeline")

        SKILLS = [
            ("opensearch-multi-signal-correlate", "Fetch metrics, logs & deploys"),
            ("opensearch-deploy-correlate",        "Score deploy suspects"),
            ("opensearch-baseline-compare",        "Compare vs 7-day baseline"),
            ("opensearch-bedrock-investigate",     "Synthesise RCA with Claude"),
        ]
        cols = st.columns(len(SKILLS))
        placeholders = []
        for i, (name, desc) in enumerate(SKILLS):
            with cols[i]:
                p = st.empty()
                p.markdown(f"⏳ **{name}**  \n<small style='color:grey'>{desc}</small>", unsafe_allow_html=True)
                placeholders.append(p)

        def skill_running(i):
            name, desc = SKILLS[i]
            placeholders[i].markdown(f"🔄 **{name}**  \n<small style='color:#f59e0b'>{desc}</small>", unsafe_allow_html=True)

        def skill_done(i, detail=""):
            name, desc = SKILLS[i]
            note = f" — {detail}" if detail else ""
            placeholders[i].markdown(f"✅ **{name}**  \n<small style='color:#22c55e'>{desc}{note}</small>", unsafe_allow_html=True)

        st.divider()

        now_ms   = int(datetime.now(timezone.utc).timestamp() * 1000)
        start_ms = int((datetime.now(timezone.utc) - timedelta(minutes=window_minutes)).timestamp() * 1000)
        report_text = ""

        # ── Skill 0: multi-signal-correlate ──────────────────────────────────
        skill_running(0)
        log_resp = os_post("/prod-logs/_search", {
            "size": 20, "sort": [{"@timestamp": "desc"}],
            "query": {"bool": {
                "must": [{"terms": {"level": ["ERROR", "WARN"]}}],
                "filter": [{"range": {"@timestamp": {"gte": f"now-{window_minutes}m"}}}],
            }},
            "_source": ["@timestamp", "level", "service", "message", "http_status", "duration_ms"],
        })
        if "error" in log_resp:
            st.error(f"prod-logs query failed: {log_resp['error']}")
        log_hits  = log_resp.get("hits", {}).get("hits", [])
        log_total = log_resp.get("hits", {}).get("total", {}).get("value", 0)
        log_text  = json.dumps([h["_source"] for h in log_hits], indent=2) if log_hits else "No errors in window."

        metric_resp = os_post("/prod-metrics/_search", {
            "size": 0, "query": {"range": {"@timestamp": {"gte": f"now-{window_minutes}m"}}},
            "aggs": {
                "max_p99":       {"max": {"field": "p99_latency_ms"}},
                "avg_p99":       {"avg": {"field": "p99_latency_ms"}},
                "max_error_rate":{"max": {"field": "error_rate_pct"}},
            },
        })
        if "error" in metric_resp:
            st.error(f"prod-metrics query failed: {metric_resp['error']}")
        aggs    = metric_resp.get("aggregations", {})
        max_p99 = aggs.get("max_p99", {}).get("value") or 0
        avg_p99 = aggs.get("avg_p99", {}).get("value") or 0
        max_err = aggs.get("max_error_rate", {}).get("value") or 0
        metric_text = (
            f"Max P99: {max_p99:.0f}ms, Avg P99: {avg_p99:.0f}ms, Max error rate: {max_err:.1f}%"
            if max_p99 else "No metric data in window."
        )
        skill_done(0, f"{log_total} errors, P99 {max_p99:.0f}ms")

        # ── Skill 1: deploy-correlate ─────────────────────────────────────────
        skill_running(1)
        deploy_resp = os_post("/deploy-events/_search", {
            "size": 5, "sort": [{"@timestamp": "desc"}],
            "query": {"range": {"@timestamp": {"gte": "now-2h"}}},
            "_source": ["@timestamp", "service", "version_from", "version_to",
                        "deployed_by", "changes", "status"],
        })
        deploy_hits = deploy_resp.get("hits", {}).get("hits", [])
        deploy_text = json.dumps([h["_source"] for h in deploy_hits], indent=2) if deploy_hits else "No deploys in last 2 hours."
        skill_done(1, f"{len(deploy_hits)} deploy(s) found")

        # ── Skill 2: baseline-compare ─────────────────────────────────────────
        skill_running(2)
        baseline_resp = os_post("/prod-metrics/_search", {
            "size": 0,
            "query": {"range": {"@timestamp": {"gte": f"now-{window_minutes}m/m", "lte": f"now-7d/d"}}},
            "aggs": {"baseline_p99": {"avg": {"field": "p99_latency_ms"}}},
        })
        baseline_p99 = (baseline_resp.get("aggregations", {})
                        .get("baseline_p99", {}).get("value")) or 120
        deviation = ((max_p99 - baseline_p99) / baseline_p99 * 100) if baseline_p99 and max_p99 else 0
        baseline_text = f"Baseline P99: {baseline_p99:.0f}ms, Current: {max_p99:.0f}ms, Deviation: {deviation:+.0f}%"
        skill_done(2, f"{deviation:+.0f}% vs baseline")

        # ── Skill 3: bedrock-investigate ──────────────────────────────────────
        skill_running(3)
        prompt = f"""You are an expert SRE on-call assistant investigating a production incident on payment-service.

[METRIC SPIKE - last {window_minutes} min]
{metric_text}
{baseline_text}

[ERROR LOGS - last {window_minutes} min] ({log_total} total errors/warnings, showing top 20)
{log_text}

[RECENT DEPLOYS - last 2 hours]
{deploy_text}

Respond in this exact format:

## Root Cause
<1-2 sentence verdict>

## Evidence (ranked by confidence)
1. <strongest signal with specific values>
2. <second signal>
3. <third signal if relevant>

## Impact
- Affected service: <name>
- P99 latency: <value> (normal: ~120ms)
- Error rate: <value>%
- Estimated blast radius: <narrow / moderate / wide>

## Recommended Action
- **Immediate**: <rollback / scale / page team / wait>
- **Verify**: <specific check to confirm root cause>
- **Confidence**: <0-100>%

## Timeline
<earliest event> → <anomaly onset> → now"""

        try:
            report_text = call_llm(prompt)
            skill_done(3, "RCA complete")
        except Exception as e:
            skill_done(3, f"failed: {e}")
            st.error(f"LLM call failed: {e}")

        if report_text:
            st.success("Investigation complete.")
            st.divider()

            # Parse sections from the markdown report
            import re as _re
            def extract_section(text, heading):
                pattern = rf"##\s+{heading}\s*\n(.*?)(?=\n##\s|\Z)"
                m = _re.search(pattern, text, _re.DOTALL | _re.IGNORECASE)
                return m.group(1).strip() if m else ""

            sec_root    = extract_section(report_text, "Root Cause")
            sec_evidence= extract_section(report_text, "Evidence.*?")
            sec_impact  = extract_section(report_text, "Impact")
            sec_action  = extract_section(report_text, "Recommended Action")
            sec_timeline= extract_section(report_text, "Timeline")

            # Root Cause, Evidence, Impact — normal markdown
            for heading, body in [
                ("## Root Cause", sec_root),
                ("## Evidence (ranked by confidence)", sec_evidence),
                ("## Impact", sec_impact),
            ]:
                if body:
                    st.markdown(f"{heading}\n\n{body}")

            # Recommended Action
            if sec_action:
                st.markdown(f"## Recommended Action\n\n{sec_action}")

            # Timeline — parse → separated events into a table
            if sec_timeline:
                st.markdown("## Timeline")
                events = [e.strip() for e in _re.split(r"→|->", sec_timeline) if e.strip()]
                if len(events) > 1:
                    rows = []
                    for i, ev in enumerate(events):
                        label = "🔴 Now" if i == len(events) - 1 else f"T{i+1}"
                        rows.append({"#": label, "Event": ev})
                    st.table(rows)
                else:
                    st.markdown(sec_timeline)

            st.divider()
            with st.expander("Evidence gathered"):
                st.subheader("Metrics")
                st.code(metric_text + "\n" + baseline_text)
                st.subheader("Error logs")
                st.code(log_text)
                st.subheader("Recent deploys")
                st.code(deploy_text)

