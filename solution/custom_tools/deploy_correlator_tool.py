"""
DeployCorrelatorTool
─────────────────────
Problem it solves: deploys cause ~70% of production incidents. Checking
deploy timing relative to an anomaly should be the FIRST thing an agent
does — but with vanilla SearchIndexTool it's just another query with no
temporal reasoning.

This tool:
  1. Finds all deploys in a configurable lookback window
  2. Scores each by proximity to the anomaly + service match + change risk
  3. Returns a ranked suspicion list with time-delta and a human-readable reason

Suspicion scoring formula:
  base_score = service_match(0.4) + time_proximity(0-0.4) + change_risk(0-0.2)

  time_proximity: 1.0 at Δt=0, 0.0 at Δt > 2h (linear decay)
  change_risk:    +0.2 if "pool" or "connection" in changes (infra risk)
                  +0.1 if "refactor" or "new" in changes (code risk)
"""

import json
import re
from datetime import datetime, timedelta, timezone

import requests

BASE = "http://localhost:9200"

RISKY_KEYWORDS = {
    "pool":        0.20, "connection":   0.20, "timeout":    0.15,
    "refactor":    0.10, "new":          0.08, "rewrite":    0.12,
    "dependency":  0.08, "upgrade":      0.07, "migration":  0.15,
    "rollback":    0.05, "hotfix":       0.05,
}


def _change_risk(changes: str) -> float:
    text = changes.lower()
    score = 0.0
    for kw, weight in RISKY_KEYWORDS.items():
        if kw in text:
            score += weight
    return min(score, 0.20)


def _time_proximity(deploy_ts: str, anomaly_ts: str) -> float:
    fmt = "%Y-%m-%dT%H:%M:%SZ"
    try:
        d = datetime.strptime(deploy_ts, fmt).replace(tzinfo=timezone.utc)
        a = datetime.strptime(anomaly_ts, fmt).replace(tzinfo=timezone.utc)
        delta_minutes = (a - d).total_seconds() / 60
        if delta_minutes < 0:
            return 0.0                          # deploy AFTER anomaly — not causal
        max_window = 120                        # 2-hour blame window
        return max(0.0, 1.0 - delta_minutes / max_window) * 0.40
    except Exception:
        return 0.10


class DeployCorrelatorTool:
    name = "DeployCorrelatorTool"
    description = (
        "Finds recent deploy events and scores each by its proximity to an anomaly "
        "timestamp, service match, and change-risk keywords. Returns a ranked "
        "suspicion list so the agent can immediately identify the most likely "
        "deploy-caused regression without building a DSL query. "
        "Params: anomaly_time (ISO8601), service (string), lookback_minutes (int, default 120)."
    )

    def execute(self, params: dict) -> dict:
        service         = params.get("service", "payment-service")
        anomaly_time    = params.get("anomaly_time") or datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        lookback_minutes = int(params.get("lookback_minutes", 120))

        resp = requests.post(
            f"{BASE}/deploy-events/_search",
            json={
                "query": {"range": {"@timestamp": {"gte": f"now-{lookback_minutes}m"}}},
                "size":  20,
                "sort":  [{"@timestamp": "desc"}],
                "_source": ["@timestamp", "service", "version_from", "version_to",
                            "deployed_by", "changes", "status"],
            },
            timeout=10,
        ).json()

        hits = resp.get("hits", {}).get("hits", [])
        scored = []

        for h in hits:
            src = h["_source"]
            svc_score      = 0.40 if src.get("service") == service else 0.0
            time_score     = _time_proximity(src.get("@timestamp", ""), anomaly_time)
            risk_score     = _change_risk(src.get("changes", ""))
            total          = round(svc_score + time_score + risk_score, 3)

            try:
                d = datetime.strptime(src["@timestamp"], "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
                a = datetime.strptime(anomaly_time,      "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
                delta_min = int((a - d).total_seconds() / 60)
                delta_label = f"{delta_min}m before anomaly" if delta_min >= 0 else f"{abs(delta_min)}m after anomaly"
            except Exception:
                delta_label = "unknown"

            reasons = []
            if svc_score:
                reasons.append(f"same service ({service})")
            if time_score > 0.25:
                reasons.append(f"deployed {delta_label}")
            elif time_score > 0:
                reasons.append(f"deployed {delta_label} (wider window)")
            for kw in RISKY_KEYWORDS:
                if kw in src.get("changes", "").lower():
                    reasons.append(f"changes mention '{kw}'")
                    break

            scored.append({
                "deploy":       src,
                "suspicion":    total,
                "delta":        delta_label,
                "reasons":      reasons,
                "verdict":      _verdict(total),
            })

        scored.sort(key=lambda x: x["suspicion"], reverse=True)
        top = scored[0] if scored else None

        return {
            "anomaly_time":   anomaly_time,
            "service":        service,
            "lookback_min":   lookback_minutes,
            "total_deploys":  len(scored),
            "top_suspect":    top,
            "all_suspects":   scored[:5],
        }


def _verdict(score: float) -> str:
    if score >= 0.75:  return "HIGH — likely root cause"
    if score >= 0.50:  return "MEDIUM — investigate further"
    if score >= 0.25:  return "LOW — possible contributor"
    return "UNLIKELY"


# ── MCP tool registration stub ────────────────────────────────────────────────
# @tool
# def deploy_correlator(service: str, anomaly_time: str, lookback_minutes: int = 120) -> str:
#     """Score recent deploys by suspicion relative to an anomaly timestamp."""
#     return json.dumps(
#         DeployCorrelatorTool().execute(
#             {"service": service, "anomaly_time": anomaly_time, "lookback_minutes": lookback_minutes}
#         ), indent=2, default=str
#     )


if __name__ == "__main__":
    from datetime import timezone
    result = DeployCorrelatorTool().execute({
        "service":        "payment-service",
        "anomaly_time":   datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "lookback_minutes": 120,
    })
    print(json.dumps(result, indent=2, default=str))
