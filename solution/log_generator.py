"""
Continuous production traffic simulator.
Runs as a Docker service (log-generator), ingesting real-time metrics and
logs into OpenSearch every TICK_SECONDS — exactly what Data Prepper or
Fluent Bit would do in production.

Incident mode is toggled by writing "incident" to /tmp/incident_mode.
The alerting monitor + investigation agent take it from there automatically.

Usage (standalone):  python log_generator.py
Usage (Docker):      set in docker-compose, OPENSEARCH_URL env var
"""

import json
import os
import random
import time
from datetime import datetime, timezone

from os_client import get_client

os_client = get_client()
TICK_SECONDS = int(os.environ.get("TICK_SECONDS", "30"))
INCIDENT_FLAG = "/tmp/incident_mode"
AUTO_INCIDENT_FLAG = "/tmp/auto_incident_mode"

# Auto-incident schedule: after INITIAL_DELAY_S, inject a INCIDENT_DURATION_S-long
# incident every INCIDENT_INTERVAL_S seconds. Fully automatic — no manual file touching.
INITIAL_DELAY_S    = int(os.environ.get("INITIAL_DELAY_S",    str(2 * 60)))   # 2 min warmup
INCIDENT_DURATION_S = int(os.environ.get("INCIDENT_DURATION_S", str(5 * 60))) # 5 min incident
INCIDENT_INTERVAL_S = int(os.environ.get("INCIDENT_INTERVAL_S", str(20 * 60))) # every 20 min

SERVICES = ["payment-service", "order-service", "inventory-service", "auth-service"]
ANOMALY_SERVICE = "payment-service"

NORMAL_MESSAGES = {
    "payment-service":   ["Payment processed", "Card validated", "Fraud check passed", "Token issued"],
    "order-service":     ["Order created", "Order updated", "Inventory reserved"],
    "inventory-service": ["Stock checked", "Reservation confirmed", "Cache refreshed"],
    "auth-service":      ["Token issued", "Session validated", "OAuth completed"],
}

INCIDENT_MESSAGES = [
    "NullPointerException in PaymentProcessor.validate() at line 247",
    "NullPointerException in CardTokenizer.process() at line 112",
    "DB connection timeout after 30000ms — pool exhausted (max=10, active=10)",
    "DB connection timeout — all connections in use",
    "WARN: connection pool utilisation 100% — queuing requests",
    "ERROR: upstream validation service returned 503",
    "Payment request failed: downstream timeout",
]


def now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def is_incident() -> bool:
    return os.path.exists(INCIDENT_FLAG) or os.path.exists(AUTO_INCIDENT_FLAG)


def _auto_incident_wanted(elapsed: float) -> bool:
    """Return True if the auto-scheduler wants incident mode active right now."""
    if elapsed < INITIAL_DELAY_S:
        return False
    adjusted = elapsed - INITIAL_DELAY_S
    cycle = INCIDENT_DURATION_S + INCIDENT_INTERVAL_S
    return (adjusted % cycle) < INCIDENT_DURATION_S


def manage_auto_incident(elapsed: float, prev_auto: bool) -> bool:
    """Create/remove AUTO_INCIDENT_FLAG based on schedule. Returns new prev_auto."""
    wanted = _auto_incident_wanted(elapsed)
    if wanted and not prev_auto:
        open(AUTO_INCIDENT_FLAG, "w").close()
        incident_num = int((elapsed - INITIAL_DELAY_S) // (INCIDENT_DURATION_S + INCIDENT_INTERVAL_S)) + 1
        print(f"[generator] AUTO-INCIDENT #{incident_num} started ({INCIDENT_DURATION_S}s duration)")
    elif not wanted and prev_auto:
        if os.path.exists(AUTO_INCIDENT_FLAG):
            os.remove(AUTO_INCIDENT_FLAG)
        print("[generator] AUTO-INCIDENT ended — next in ~{:.0f}m".format(INCIDENT_INTERVAL_S / 60))
    return wanted


def generate_metric(service: str, incident: bool) -> dict:
    if service == ANOMALY_SERVICE and incident:
        return {
            "@timestamp":     now_iso(),
            "service":        service,
            "p50_latency_ms": round(random.uniform(350, 550), 1),
            "p99_latency_ms": round(random.uniform(1800, 2800), 1),
            "error_rate_pct": round(random.uniform(8, 15), 3),
            "throughput_rps": random.randint(580, 720),
        }
    return {
        "@timestamp":     now_iso(),
        "service":        service,
        "p50_latency_ms": round(random.uniform(38, 68), 1),
        "p99_latency_ms": round(random.uniform(95, 145), 1),
        "error_rate_pct": round(random.uniform(0.04, 0.28), 3),
        "throughput_rps": random.randint(790, 910),
    }


def generate_logs(service: str, incident: bool, count: int) -> list:
    logs = []
    rng = random.Random()
    for _ in range(count):
        if service == ANOMALY_SERVICE and incident:
            msg = rng.choice(INCIDENT_MESSAGES)
            level = "ERROR" if "Exception" in msg or "timeout" in msg else "WARN"
            status = rng.choice([500, 503, 504]) if level == "ERROR" else 200
            duration = rng.randint(1800, 3500)
        else:
            msg = rng.choice(NORMAL_MESSAGES.get(service, ["Request handled"]))
            level = "INFO"
            status = 200
            duration = rng.randint(20, 100)

        logs.append({
            "@timestamp":  now_iso(),
            "service":     service,
            "level":       level,
            "message":     msg,
            "http_status": status,
            "duration_ms": duration,
            "host":        f"{service[:3]}-{rng.randint(1, 3)}",
            "trace_id":    f"{rng.randint(0, 0xFFFFFFFF):08x}",
        })
    return logs


def bulk_index(index: str, docs: list):
    if not docs:
        return
    actions = []
    for doc in docs:
        actions.append({"index": {"_index": index}})
        actions.append(doc)
    try:
        os_client.bulk(body=actions)
    except Exception as e:
        print(f"[generator] bulk error: {e}")


def tick():
    incident = is_incident()
    mode_label = "INCIDENT" if incident else "normal"

    metrics, logs = [], []
    for svc in SERVICES:
        metrics.append(generate_metric(svc, incident))
        log_count = random.randint(10, 18) if (svc == ANOMALY_SERVICE and incident) else random.randint(2, 5)
        logs.extend(generate_logs(svc, incident, log_count))

    bulk_index("prod-metrics", metrics)
    bulk_index("prod-logs", logs)

    ts = datetime.now(timezone.utc).strftime("%H:%M:%S")
    print(f"[{ts}] [{mode_label}] +{len(metrics)} metrics  +{len(logs)} logs")


def main():
    print(f"[generator] Starting — tick: {TICK_SECONDS}s")
    print(f"[generator] Manual incident: touch {INCIDENT_FLAG}")
    print(f"[generator] Auto-incident: in {INITIAL_DELAY_S}s, then every {INCIDENT_INTERVAL_S}s for {INCIDENT_DURATION_S}s")

    for _ in range(30):
        try:
            health = os_client.cluster.health()
            if health.get("status") in ("green", "yellow"):
                print("[generator] OpenSearch ready")
                break
        except Exception:
            pass
        time.sleep(5)

    start_time = time.time()
    prev_auto = False

    while True:
        try:
            elapsed = time.time() - start_time
            prev_auto = manage_auto_incident(elapsed, prev_auto)
            tick()
        except Exception as e:
            print(f"[generator] tick error: {e}")
        time.sleep(TICK_SECONDS)


if __name__ == "__main__":
    main()
