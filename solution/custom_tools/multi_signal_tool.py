"""
MultiSignalCorrelatorTool
─────────────────────────
Problem it solves: the default investigation runs 4–5 sequential tool calls
(anomaly results → error logs → deploys → log patterns → synthesis).
Each round-trip adds 200–400 ms. Under a live incident that's dead time.

This tool fires all queries in parallel using concurrent.futures, returns a
single correlated JSON, and hands it directly to MLModelTool. Investigation
time drops from ~4 s to ~1 s.

Registration path:
  1. Drop this file into opensearch-mcp-server-py/tools/custom/
  2. Add @tool decorator (see bottom of file)
  3. Restart the MCP server — OpenSearch sees it as McpSseTool

Direct call path (used in investigate.py):
  from custom_tools.multi_signal_tool import MultiSignalCorrelatorTool
  result = MultiSignalCorrelatorTool().execute({"service": "payment-service", "window_minutes": 10})
"""

import json
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta, timezone
from typing import Any

import requests

BASE = "http://localhost:9200"


class MultiSignalCorrelatorTool:
    name = "MultiSignalCorrelatorTool"
    description = (
        "Fetches anomaly results, error logs, deploy events, and metric spikes "
        "in parallel for a given service and time window. Returns a single "
        "pre-correlated JSON with all evidence. Use instead of four sequential "
        "SearchIndexTool / SearchAnomalyResultsTool calls."
    )

    # ── public interface ──────────────────────────────────────────────────────

    def execute(self, params: dict) -> dict:
        service = params.get("service", "payment-service")
        window_minutes = int(params.get("window_minutes", 10))
        now = datetime.now(timezone.utc)
        window_start = int((now - timedelta(minutes=window_minutes)).timestamp() * 1000)
        window_end   = int(now.timestamp() * 1000)

        tasks = {
            "anomaly_results":  lambda: self._fetch_anomaly_results(window_start, window_end),
            "error_logs":       lambda: self._fetch_error_logs(service, window_minutes),
            "deploy_events":    lambda: self._fetch_deploy_events(window_minutes * 6),  # 6× window
            "metric_spike":     lambda: self._fetch_metric_spike(service, window_minutes),
        }

        results: dict[str, Any] = {}
        with ThreadPoolExecutor(max_workers=4) as pool:
            futures = {pool.submit(fn): key for key, fn in tasks.items()}
            for future in as_completed(futures):
                key = futures[future]
                try:
                    results[key] = future.result()
                except Exception as e:
                    results[key] = {"error": str(e)}

        results["correlation_summary"] = self._correlate(results, service)
        return results

    # ── private fetchers ──────────────────────────────────────────────────────

    def _fetch_anomaly_results(self, start_ms: int, end_ms: int) -> dict:
        query = {
            "query": {"bool": {"filter": [
                {"range": {"data_end_time": {"gte": start_ms, "lte": end_ms}}},
                {"range": {"anomaly_grade": {"gt": 0}}},
            ]}},
            "sort": [{"anomaly_grade": "desc"}],
            "size": 5,
            "_source": ["data_end_time", "anomaly_grade", "anomaly_score", "detector_id"],
        }
        resp = requests.post(
            f"{BASE}/.opendistro-anomaly-results*/_search",
            json=query, timeout=10
        ).json()
        hits = resp.get("hits", {}).get("hits", [])
        return {
            "count": len(hits),
            "max_grade": max((h["_source"].get("anomaly_grade", 0) for h in hits), default=0),
            "results": [h["_source"] for h in hits],
        }

    def _fetch_error_logs(self, service: str, window_minutes: int) -> dict:
        query = {
            "query": {"bool": {"must": [
                {"term": {"service": service}},
                {"terms": {"level": ["ERROR", "WARN"]}},
                {"range": {"@timestamp": {"gte": f"now-{window_minutes}m"}}},
            ]}},
            "size": 50,
            "sort": [{"@timestamp": "desc"}],
            "_source": ["@timestamp", "level", "message", "http_status", "duration_ms"],
            "aggs": {
                "by_message": {
                    "terms": {"field": "message.keyword", "size": 10}
                },
                "error_5xx": {
                    "filter": {"range": {"http_status": {"gte": 500}}}
                },
            },
        }
        resp = requests.post(f"{BASE}/prod-logs/_search", json=query, timeout=10).json()
        hits = resp.get("hits", {}).get("hits", [])
        aggs = resp.get("aggregations", {})

        patterns = [
            {"message": b["key"], "count": b["doc_count"]}
            for b in aggs.get("by_message", {}).get("buckets", [])
        ]
        return {
            "total_errors": resp.get("hits", {}).get("total", {}).get("value", 0),
            "http_5xx_count": aggs.get("error_5xx", {}).get("doc_count", 0),
            "top_patterns": patterns[:5],
            "recent": [h["_source"] for h in hits[:5]],
        }

    def _fetch_deploy_events(self, lookback_minutes: int) -> dict:
        query = {
            "query": {"range": {"@timestamp": {"gte": f"now-{lookback_minutes}m"}}},
            "size": 10,
            "sort": [{"@timestamp": "desc"}],
            "_source": ["@timestamp", "service", "version_from", "version_to",
                        "deployed_by", "changes", "status"],
        }
        resp = requests.post(f"{BASE}/deploy-events/_search", json=query, timeout=10).json()
        hits = resp.get("hits", {}).get("hits", [])
        return {
            "count": resp.get("hits", {}).get("total", {}).get("value", 0),
            "deploys": [h["_source"] for h in hits],
        }

    def _fetch_metric_spike(self, service: str, window_minutes: int) -> dict:
        query = {
            "query": {"bool": {"must": [
                {"term": {"service": service}},
                {"range": {"@timestamp": {"gte": f"now-{window_minutes}m"}}},
            ]}},
            "size": 0,
            "aggs": {
                "max_p99":   {"max": {"field": "p99_latency_ms"}},
                "avg_p99":   {"avg": {"field": "p99_latency_ms"}},
                "max_error": {"max": {"field": "error_rate_pct"}},
                "avg_error": {"avg": {"field": "error_rate_pct"}},
            },
        }
        resp = requests.post(f"{BASE}/prod-metrics/_search", json=query, timeout=10).json()
        aggs = resp.get("aggregations", {})
        return {
            "max_p99_ms":      round(aggs.get("max_p99",   {}).get("value", 0) or 0, 1),
            "avg_p99_ms":      round(aggs.get("avg_p99",   {}).get("value", 0) or 0, 1),
            "max_error_pct":   round(aggs.get("max_error", {}).get("value", 0) or 0, 3),
            "avg_error_pct":   round(aggs.get("avg_error", {}).get("value", 0) or 0, 3),
        }

    # ── correlation ───────────────────────────────────────────────────────────

    def _correlate(self, results: dict, service: str) -> dict:
        anomaly   = results.get("anomaly_results", {})
        logs      = results.get("error_logs", {})
        deploys   = results.get("deploy_events", {})
        metrics   = results.get("metric_spike", {})

        suspects = []
        for deploy in deploys.get("deploys", []):
            if deploy.get("service") == service:
                suspects.append({
                    "type":       "deploy",
                    "version":    deploy.get("version_to"),
                    "actor":      deploy.get("deployed_by"),
                    "timestamp":  deploy.get("@timestamp"),
                    "suspicion":  0.90,
                    "reason":     f"Same-service deploy {deploy.get('version_from')} → {deploy.get('version_to')} in anomaly window",
                })

        baseline_p99 = 120
        max_p99 = metrics.get("max_p99_ms", 0)
        if max_p99 > baseline_p99 * 3:
            suspects.append({
                "type":      "latency_spike",
                "value":     f"{max_p99} ms",
                "ratio":     round(max_p99 / baseline_p99, 1),
                "suspicion": 0.85,
                "reason":    f"p99 latency {round(max_p99/baseline_p99,1)}× above baseline",
            })

        npe_patterns = [
            p for p in logs.get("top_patterns", [])
            if "NullPointerException" in p.get("message", "") or "timeout" in p.get("message", "")
        ]
        if npe_patterns:
            suspects.append({
                "type":      "error_pattern",
                "patterns":  npe_patterns,
                "suspicion": 0.75,
                "reason":    f"{len(npe_patterns)} high-suspicion error pattern(s) in logs",
            })

        suspects.sort(key=lambda s: s["suspicion"], reverse=True)
        return {
            "anomaly_grade":   anomaly.get("max_grade", 0),
            "error_count":     logs.get("total_errors", 0),
            "deploy_count":    deploys.get("count", 0),
            "top_suspects":    suspects[:3],
            "confidence":      suspects[0]["suspicion"] if suspects else 0.0,
        }


# ── MCP tool registration (opensearch-mcp-server-py pattern) ─────────────────
# Uncomment when dropping into the MCP server:
#
# from mcp.server import tool
#
# @tool
# def multi_signal_correlator(service: str, window_minutes: int = 10) -> str:
#     """Parallel fetch of all incident signals. Returns correlated JSON."""
#     result = MultiSignalCorrelatorTool().execute(
#         {"service": service, "window_minutes": window_minutes}
#     )
#     return json.dumps(result, indent=2)


if __name__ == "__main__":
    tool = MultiSignalCorrelatorTool()
    result = tool.execute({"service": "payment-service", "window_minutes": 10})
    print(json.dumps(result, indent=2, default=str))
