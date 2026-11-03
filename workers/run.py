"""Background loop: households, probabilistic matching, abandonment flows, retention.

    python -m workers.run            # loop every FLOW_WORKER_INTERVAL_SECONDS
    python -m workers.run --once     # single pass (cron-friendly)
"""
from __future__ import annotations

import sys
import time

from db import fetchall
from domain.activation import run_all
from domain.households import rebuild as rebuild_households
from domain.matching import run_matching
from domain.privacy import apply_retention
from settings import FLOW_WORKER_INTERVAL_SECONDS


def tick(n: int = 0):
    for t in fetchall("select id from tenant"):
        tid = t["id"]
        h = rebuild_households(tid, actor="worker")  # before matching: candidates get the household flag
        m = run_matching(tid, actor="worker")
        if m.get("auto_merged"):
            h = rebuild_households(tid, actor="worker")  # merged profiles are one person, not two members
        f = run_all(tid, actor="worker")
        line = f"[{tid}] households={h} matching={m} flows={[(r['flow'], r['sent'], r['skipped']) for r in f]}"
        if n % 60 == 0:  # roughly hourly at the default interval
            line += f" retention={apply_retention(tid)}"
        print(line, flush=True)


if __name__ == "__main__":
    if "--once" in sys.argv:
        tick()
        sys.exit(0)
    i = 0
    while True:
        try:
            tick(i)
        except Exception as exc:  # keep the loop alive; errors surface in logs
            print(f"worker error: {exc!r}", flush=True)
        i += 1
        time.sleep(FLOW_WORKER_INTERVAL_SECONDS)
