"""
Seeds OpenSearch with 3 hours of realistic observability data.

Timeline (all times relative to NOW = script run time):
  T-180m .. T-31m  normal operation, payment-service healthy
  T-30m            deploy: payment-service v2.3.0 → v2.3.1 by alice
  T-25m .. T-6m    gradual latency creep (deploy starts warming up)
  T-5m  .. T+0     anomaly onset: p99 spikes, errors spike
  T+0              (now) — this is when the detector fires

Indices created:
  prod-metrics     time-series latency/throughput/error-rate per service
  prod-logs        structured application logs
  deploy-events    deployment records
"""

import json
import random
import sys
from datetime import datetime, timedelta, timezone
import os

from os_client import get_client

os_client = get_client()
NOW = datetime.now(timezone.utc).replace(microsecond=0)

SERVICES = ["payment-service", "order-service", "inventory-service", "auth-service"]
ANOMALY_SERVICE = "payment-service"
DEPLOY_OFFSET = timedelta(minutes=30)   # deploy happened 30m ago
ONSET_OFFSET = timedelta(minutes=5)    # anomaly onset 5m ago

# ── Index definitions ─────────────────────────────────────────────────────────

INDICES = {
    "prod-metrics": {
        "mappings": {
            "properties": {
                "@timestamp":      {"type": "date"},
                "service":         {"type": "keyword"},
                "p50_latency_ms":  {"type": "float"},
                "p99_latency_ms":  {"type": "float"},
                "error_rate_pct":  {"type": "float"},
                "throughput_rps":  {"type": "integer"},
            }
        }
    },
    "prod-logs": {
        "mappings": {
            "properties": {
                "@timestamp":  {"type": "date"},
                "service":     {"type": "keyword"},
                "level":       {"type": "keyword"},
                "message":     {"type": "text"},
                "http_status": {"type": "integer"},
                "duration_ms": {"type": "integer"},
                "host":        {"type": "keyword"},
                "trace_id":    {"type": "keyword"},
            }
        }
    },
    "deploy-events": {
        "mappings": {
            "properties": {
                "@timestamp":  {"type": "date"},
                "service":     {"type": "keyword"},
                "version_from": {"type": "keyword"},
                "version_to":   {"type": "keyword"},
                "deployed_by":  {"type": "keyword"},
                "environment":  {"type": "keyword"},
                "changes":      {"type": "text"},
                "status":       {"type": "keyword"},
            }
        }
    },
}

# ── Normal log messages per service ──────────────────────────────────────────

NORMAL_MESSAGES = {
    "payment-service": [
        "Payment processed successfully",
        "Card validation passed",
        "Fraud check completed: clean",
        "Tokenisation successful",
    ],
    "order-service": [
        "Order created", "Order status updated", "Inventory reserved",
    ],
    "inventory-service": [
        "Stock level checked", "Reservation confirmed", "Cache refreshed",
    ],
    "auth-service": [
        "Token issued", "Session validated", "OAuth flow completed",
    ],
}

ANOMALY_MESSAGES = [
    "NullPointerException in PaymentProcessor.validate() at line 247",
    "NullPointerException in CardTokenizer.process() at line 112",
    "DB connection timeout after 30000ms — pool exhausted (max=10, active=10)",
    "DB connection timeout — all connections in use",
    "WARN: connection pool utilisation 100% — queuing requests",
    "ERROR: upstream validation service returned 503",
    "Payment request failed: downstream timeout",
]


def ts(offset: timedelta) -> str:
    return (NOW - offset).strftime("%Y-%m-%dT%H:%M:%SZ")


def bulk_index(index: str, docs: list):
    actions = []
    for doc in docs:
        actions.append({"index": {"_index": index}})
        actions.append(doc)
    result = os_client.bulk(body=actions)
    if result.get("errors"):
        print(f"  [WARN] bulk errors in {index}", file=sys.stderr)


def create_indices():
    print("Creating indices …")
    for name, body in INDICES.items():
        try:
            os_client.indices.delete(index=name, ignore_unavailable=True)
        except Exception:
            pass
        try:
            os_client.indices.create(index=name, body=body)
            print(f"  {name}: ok")
        except Exception as e:
            print(f"  {name}: {e}")


def seed_metrics():
    print("Seeding prod-metrics (1-min resolution, 3 hours) …")
    docs = []
    for minute in range(180, -1, -1):
        offset = timedelta(minutes=minute)
        t = ts(offset)
        for svc in SERVICES:
            is_anomaly_svc = svc == ANOMALY_SERVICE
            is_anomaly_window = minute <= 5
            is_creep_window = 6 <= minute <= 25

            if is_anomaly_svc and is_anomaly_window:
                # Full anomaly
                p50 = random.uniform(400, 600)
                p99 = random.uniform(1800, 2800)
                err = random.uniform(8, 15)
                rps = random.randint(600, 750)
            elif is_anomaly_svc and is_creep_window:
                # Gradual creep after deploy
                creep_factor = (26 - minute) / 20   # 0..1
                p50 = random.uniform(50, 80) + creep_factor * 200
                p99 = random.uniform(110, 140) + creep_factor * 800
                err = random.uniform(0.1, 0.3) + creep_factor * 3
                rps = random.randint(820, 870)
            else:
                # Normal
                p50 = random.uniform(40, 65)
                p99 = random.uniform(100, 140)
                err = random.uniform(0.05, 0.25)
                rps = random.randint(800, 900)

            docs.append({
                "@timestamp": t,
                "service": svc,
                "p50_latency_ms": round(p50, 1),
                "p99_latency_ms": round(p99, 1),
                "error_rate_pct": round(err, 3),
                "throughput_rps": rps,
            })

    bulk_index("prod-metrics", docs)
    print(f"  {len(docs)} metric data points")


def seed_logs():
    print("Seeding prod-logs …")
    docs = []
    rng = random.Random(42)

    for minute in range(180, -1, -1):
        offset = timedelta(minutes=minute)
        t = ts(offset)
        is_anomaly_window = minute <= 5
        is_creep_window = 6 <= minute <= 25

        for svc in SERVICES:
            # How many log lines per minute
            if svc == ANOMALY_SERVICE and is_anomaly_window:
                count = rng.randint(12, 20)
            elif svc == ANOMALY_SERVICE and is_creep_window:
                count = rng.randint(4, 8)
            else:
                count = rng.randint(2, 5)

            for _ in range(count):
                sec_offset = timedelta(seconds=rng.randint(0, 59))
                log_ts = (NOW - offset + sec_offset).strftime("%Y-%m-%dT%H:%M:%SZ")

                if svc == ANOMALY_SERVICE and is_anomaly_window:
                    msg = rng.choice(ANOMALY_MESSAGES)
                    level = "ERROR" if "Exception" in msg or "timeout" in msg else "WARN"
                    status = rng.choice([500, 503, 504]) if level == "ERROR" else 200
                    duration = rng.randint(1800, 3500)
                elif svc == ANOMALY_SERVICE and is_creep_window:
                    # Occasional slow / warn during creep
                    if rng.random() < 0.3:
                        msg = rng.choice(ANOMALY_MESSAGES[-3:])
                        level = "WARN"
                        status = 200
                        duration = rng.randint(400, 900)
                    else:
                        msg = rng.choice(NORMAL_MESSAGES[svc])
                        level = "INFO"
                        status = 200
                        duration = rng.randint(40, 120)
                else:
                    msg = rng.choice(NORMAL_MESSAGES.get(svc, ["Request handled"]))
                    level = "INFO"
                    status = 200
                    duration = rng.randint(20, 100)

                docs.append({
                    "@timestamp": log_ts,
                    "service": svc,
                    "level": level,
                    "message": msg,
                    "http_status": status,
                    "duration_ms": duration,
                    "host": f"{svc[:3]}-{rng.randint(1,3)}",
                    "trace_id": f"{rng.randint(0, 0xFFFFFFFF):08x}",
                })

    bulk_index("prod-logs", docs)
    print(f"  {len(docs)} log entries")


def seed_deploys():
    print("Seeding deploy-events …")
    deploy_time = ts(DEPLOY_OFFSET)
    docs = [
        # The culprit deploy
        {
            "@timestamp": deploy_time,
            "service": "payment-service",
            "version_from": "v2.3.0",
            "version_to": "v2.3.1",
            "deployed_by": "alice",
            "environment": "production",
            "changes": (
                "Add new payment validation flow using refactored CardTokenizer; "
                "update DB connection pool max from 20 to 10 to reduce memory footprint; "
                "bump dependencies: stripe-java 23.1 -> 23.3"
            ),
            "status": "completed",
        },
        # Innocent earlier deploys
        {
            "@timestamp": ts(timedelta(hours=2, minutes=15)),
            "service": "order-service",
            "version_from": "v1.8.4",
            "version_to": "v1.8.5",
            "deployed_by": "bob",
            "environment": "production",
            "changes": "Fix order status pagination bug",
            "status": "completed",
        },
        {
            "@timestamp": ts(timedelta(hours=4)),
            "service": "auth-service",
            "version_from": "v3.1.0",
            "version_to": "v3.1.1",
            "deployed_by": "carol",
            "environment": "production",
            "changes": "Update OAuth token TTL from 1h to 2h",
            "status": "completed",
        },
    ]
    bulk_index("deploy-events", docs)
    print(f"  {len(docs)} deploy records")


def refresh_all():
    for name in INDICES:
        try:
            os_client.indices.refresh(index=name)
        except Exception:
            pass


def main():
    print(f"Seeding incident scenario (T=now={NOW.strftime('%H:%M:%S UTC')})")
    print(f"  Deploy:         T-30m  ({ts(DEPLOY_OFFSET)})")
    print(f"  Anomaly onset:  T-5m   ({ts(ONSET_OFFSET)})")
    print()

    create_indices()
    seed_metrics()
    seed_logs()
    seed_deploys()
    refresh_all()

    print("\nDone. Run setup_agent.sh next.")


if __name__ == "__main__":
    main()
