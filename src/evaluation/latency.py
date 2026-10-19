"""
Latency reporter: reads decisions.jsonl and produces latency_report.json.
"""

import json
import logging
import math
import os
from typing import List

log = logging.getLogger(__name__)


def compute_latency_report(decisions_path: str, out_path: str) -> dict:
    """Read decisions.jsonl and compute latency statistics."""
    times: List[int] = []
    status_counts = {"OK": 0, "WARN": 0, "FAILED": 0}
    over_60s = 0
    over_5min = 0

    if os.path.exists(decisions_path):
        with open(decisions_path, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                d = json.loads(line)
                ms = d.get("decided_in_ms", 0)
                times.append(ms)
                ls = d.get("latency_status", "OK")
                status_counts[ls] = status_counts.get(ls, 0) + 1
                if ms > 60_000:
                    over_60s += 1
                if ms > 300_000:
                    over_5min += 1

    n = len(times)
    if n == 0:
        report = {"error": "no decisions found"}
    else:
        times_sorted = sorted(times)
        report = {
            "total_decisions": n,
            "latency_ms": {
                "p50": times_sorted[n // 2],
                "p90": times_sorted[int(n * 0.90)],
                "p95": times_sorted[int(n * 0.95)],
                "p99": times_sorted[int(n * 0.99)],
                "max": times_sorted[-1],
                "mean": round(sum(times) / n),
            },
            "status_counts": status_counts,
            "over_60s_count": over_60s,
            "over_5min_count": over_5min,
            "within_budget_pct": round((n - over_60s) / n * 100, 1),
        }

    os.makedirs(os.path.dirname(out_path) if os.path.dirname(out_path) else ".", exist_ok=True)
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(report, f, indent=2)
    log.info("Wrote latency report to %s", out_path)
    return report
