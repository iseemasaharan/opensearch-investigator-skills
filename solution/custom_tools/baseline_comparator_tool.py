"""
BaselineComparisonTool
───────────────────────
Problem it solves: OpenSearch's anomaly detector learns patterns over time
and adapts. This means a slow regression — e.g. p99 creeping from 120 ms
to 300 ms over two weeks — may never cross the anomaly threshold because
the model adapts to the new normal.

This tool fixes that blind spot by comparing the current window against
the same time-of-day window from N days ago (default: 7 days = same
weekday, same hour). If the deviation exceeds a threshold, it fires a
"baseline breach" signal independent of the ML detector.

Also useful as a FASTER pre-screen: runs in <200 ms on any metric field
because it's just two aggregation queries in parallel.

Use cases:
  - Weekly regression detection (deploy that degraded performance gradually)
  - SLO burn-rate trending (error rate 3× last week at this time)
  - Capacity planning (throughput 40% lower than last Tuesday)
"""

import json
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from typing import Optional

import requests

BASE = "http://localhost:9200"


class BaselineComparisonTool:
    name = "BaselineComparisonTool"
    description = (
        "Compares current metric values against the same time window N days ago "
        "(default 7 days) to catch slow regressions the ML anomaly detector "
        "may have adapted to. Returns deviation %, trend direction, and a "
        "breach flag if deviation exceeds the configured threshold. "
        "Params: index, metric_field, service, window_minutes (default 10), "
        "baseline_days (default 7), breach_threshold_pct (default 50)."
    )

    def execute(self, params: dict) -> dict:
        index           = params.get("index", "prod-metrics")
        metric_field    = params.get("metric_field", "p99_latency_ms")
        service         = params.get("service", "payment-service")
        window_minutes  = int(params.get("window_minutes", 10))
        baseline_days   = int(params.get("baseline_days", 7))
        threshold_pct   = float(params.get("breach_threshold_pct", 50))

        now      = datetime.now(timezone.utc)
        baseline = now - timedelta(days=baseline_days)

        def fetch_stats(reference_time: datetime, label: str) -> dict:
            gte = (reference_time - timedelta(minutes=window_minutes)).strftime("%Y-%m-%dT%H:%M:%SZ")
            lte = reference_time.strftime("%Y-%m-%dT%H:%M:%SZ")
            query = {
                "query": {"bool": {"must": [
                    {"term":  {"service": service}},
                    {"range": {"@timestamp": {"gte": gte, "lte": lte}}},
                ]}},
                "size": 0,
                "aggs": {
                    "avg_val": {"avg": {"field": metric_field}},
                    "max_val": {"max": {"field": metric_field}},
                    "p95_val": {"percentiles": {"field": metric_field, "percents": [95]}},
                },
            }
            resp = requests.post(f"{BASE}/{index}/_search", json=query, timeout=10).json()
            aggs = resp.get("aggregations", {})
            return {
                "label":   label,
                "period":  f"{gte} → {lte}",
                "avg":     round(aggs.get("avg_val", {}).get("value") or 0, 2),
                "max":     round(aggs.get("max_val", {}).get("value") or 0, 2),
                "p95":     round((aggs.get("p95_val", {}).get("values") or {}).get("95.0") or 0, 2),
                "samples": resp.get("hits", {}).get("total", {}).get("value", 0),
            }

        with ThreadPoolExecutor(max_workers=2) as pool:
            f_current  = pool.submit(fetch_stats, now,      "current")
            f_baseline = pool.submit(fetch_stats, baseline, f"baseline (T-{baseline_days}d)")
            current  = f_current.result()
            baseline_stats = f_baseline.result()

        # Compute deviations
        def deviation(curr: float, base: float) -> Optional[float]:
            if base == 0:
                return None
            return round((curr - base) / base * 100, 1)

        avg_dev = deviation(current["avg"], baseline_stats["avg"])
        max_dev = deviation(current["max"], baseline_stats["max"])

        breached = avg_dev is not None and abs(avg_dev) >= threshold_pct
        direction = "degraded" if (avg_dev or 0) > 0 else "improved"

        verdict = "OK"
        if breached:
            if abs(avg_dev) >= threshold_pct * 2:
                verdict = f"CRITICAL — {metric_field} {direction} {abs(avg_dev):.0f}% vs {baseline_days}d ago"
            else:
                verdict = f"WARNING — {metric_field} {direction} {abs(avg_dev):.0f}% vs {baseline_days}d ago"

        return {
            "metric":             metric_field,
            "service":            service,
            "index":              index,
            "current":            current,
            "baseline":           baseline_stats,
            "avg_deviation_pct":  avg_dev,
            "max_deviation_pct":  max_dev,
            "breach_threshold":   threshold_pct,
            "breached":           breached,
            "direction":          direction,
            "verdict":            verdict,
        }


# ── MCP tool registration stub ────────────────────────────────────────────────
# @tool
# def baseline_comparison(
#     index: str = "prod-metrics",
#     metric_field: str = "p99_latency_ms",
#     service: str = "payment-service",
#     window_minutes: int = 10,
#     baseline_days: int = 7,
#     breach_threshold_pct: float = 50.0,
# ) -> str:
#     """Compare current metrics to same-time-of-week baseline. Catches slow regressions."""
#     return json.dumps(
#         BaselineComparisonTool().execute(locals()), indent=2, default=str
#     )


if __name__ == "__main__":
    result = BaselineComparisonTool().execute({
        "service":       "payment-service",
        "metric_field":  "p99_latency_ms",
        "window_minutes": 10,
    })
    print(json.dumps(result, indent=2, default=str))
