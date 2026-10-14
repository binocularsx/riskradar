"""Load test for NFR-001 and NFR-002 (Week 4, not Week 5 — NFR-003).

    python scripts/load_test.py --tps 50 --seconds 60
    python scripts/load_test.py --burst --tps 250 --seconds 60

Measures what the NFRs actually claim:

* **NFR-001** — sustained ingestion at the target TPS with **p95 end-to-end
  decision latency under 2 seconds**. End-to-end means ``decided_at -
  ingested_at``: the queue wait counts. The worker's own ``latency_ms`` measures
  only the scoring work and would flatter the result by excluding exactly the
  thing a queue can get wrong.
* **NFR-002** — a burst at 5x sustained for 60 seconds absorbed with **zero
  transaction loss**. Loss is measured by counting rows, not by trusting HTTP
  status codes.

The honesty rule from D23b applies: if the machine cannot hold the target, the
figure is revised downward and republished with the bottleneck named. It is
never quietly dropped.
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "backend"))

import psycopg  # noqa: E402

from riskradar.config import settings  # noqa: E402

TARGET_P95_MS = 2000


def db():
    return psycopg.connect(settings().app_dsn, row_factory=psycopg.rows.dict_row)


def snapshot() -> dict:
    with db() as conn:
        return dict(
            conn.execute(
                """
                SELECT (SELECT count(*) FROM transactions)  AS transactions,
                       (SELECT count(*) FROM decisions)     AS decisions,
                       (SELECT count(*) FROM scoring_queue) AS queued,
                       (SELECT count(*) FROM dead_letter)   AS dead_letter
                """
            ).fetchone()
        )


def wait_for_drain(timeout_s: float = 900) -> float:
    """Block until the queue empties. Returns seconds waited."""
    started = time.perf_counter()
    while time.perf_counter() - started < timeout_s:
        with db() as conn:
            depth = conn.execute("SELECT count(*) AS n FROM scoring_queue").fetchone()["n"]
        if depth == 0:
            return time.perf_counter() - started
        print(f"\r  draining, {depth} queued …", end="", flush=True)
        time.sleep(1.0)
    raise SystemExit("queue did not drain within the timeout")


def end_to_end(since: datetime) -> dict:
    """The number NFR-001 actually claims."""
    with db() as conn:
        return dict(
            conn.execute(
                """
                SELECT count(*) AS scored,
                       round(avg (extract(epoch FROM d.decided_at - t.ingested_at) * 1000)) AS mean_ms,
                       round(percentile_disc(0.50) WITHIN GROUP (
                             ORDER BY extract(epoch FROM d.decided_at - t.ingested_at) * 1000)) AS p50_ms,
                       round(percentile_disc(0.95) WITHIN GROUP (
                             ORDER BY extract(epoch FROM d.decided_at - t.ingested_at) * 1000)) AS p95_ms,
                       round(percentile_disc(0.99) WITHIN GROUP (
                             ORDER BY extract(epoch FROM d.decided_at - t.ingested_at) * 1000)) AS p99_ms,
                       round(max (extract(epoch FROM d.decided_at - t.ingested_at) * 1000)) AS max_ms
                  FROM decisions d
                  JOIN transactions t ON t.id = d.transaction_id
                 WHERE t.ingested_at >= %s
                """,
                (since,),
            ).fetchone()
        )


def run_simulator(args_list: list[str]) -> subprocess.CompletedProcess:
    return subprocess.run(
        [str(REPO_ROOT / ".venv" / "Scripts" / "python.exe"), "-u", "-m", "riskradar_sim", *args_list],
        cwd=REPO_ROOT / "simulator",
        capture_output=True,
        text=True,
    )


def main() -> None:
    parser = argparse.ArgumentParser(description="Risk Radar load test")
    parser.add_argument("--tps", type=int, default=50)
    parser.add_argument("--seconds", type=int, default=60)
    parser.add_argument("--burst", action="store_true", help="run the NFR-002 burst instead")
    parser.add_argument("--alerts", action="store_true", help="let the load raise alerts on the shared desk")
    parser.add_argument("--out", default="ml/artifacts/load-test.json")
    args = parser.parse_args()

    print("Risk Radar load test")
    print(f"  target: {args.tps} TPS for {args.seconds}s"
          f"{' (BURST — NFR-002)' if args.burst else ' (sustained — NFR-001)'}")

    print("\ndraining any backlog first, so the measurement is of this run only")
    wait_for_drain()
    print("\r  queue empty                     ")

    before = snapshot()
    started_at = datetime.now(timezone.utc)
    wall = time.perf_counter()

    print("\ngenerating load …")
    result = run_simulator([
        "burst", "--tps", str(args.tps), "--seconds", str(args.seconds),
        "--batch-size", "200" if args.burst else "50",
        # D87a: payments are scored exactly as live ones, but raise no alerts:
        # 90,000 synthetic payments from customers with no history once put
        # 43,959 alerts behind the demo desk's budget. --alerts to include them.
        *([] if args.alerts else ["--quiet"]),
    ])
    print(result.stdout.strip()[-400:])
    if result.returncode != 0:
        print(result.stderr.strip()[-1000:], file=sys.stderr)

    ingest_seconds = time.perf_counter() - wall
    after_ingest = snapshot()
    ingested = after_ingest["transactions"] - before["transactions"]

    print(f"\ningested {ingested} transactions in {ingest_seconds:.1f}s "
          f"({ingested / ingest_seconds:.0f} TPS)")
    print(f"queue absorbed a peak of {after_ingest['queued']} rows")

    print("\nwaiting for the queue to drain …")
    drain_seconds = wait_for_drain()
    after = snapshot()
    print(f"\r  drained in {drain_seconds:.1f}s                      ")

    latency = end_to_end(started_at)
    scored = after["decisions"] - before["decisions"]
    # Loss counts against what was *meant* to arrive, not what did: a sender
    # that crashed before posting anything once scored "zero loss" here.
    target = args.tps * args.seconds
    lost = target - scored
    new_dead_letter = after["dead_letter"] - before["dead_letter"]

    verdict = {
        "run_at": started_at.isoformat(timespec="seconds"),
        "mode": "burst (NFR-002)" if args.burst else "sustained (NFR-001)",
        "target_tps": args.tps,
        "requested_seconds": args.seconds,
        "intended": target,
        "ingested": ingested,
        "achieved_ingest_tps": round(ingested / ingest_seconds, 1),
        "scored": scored,
        "transactions_lost": lost,
        "dead_lettered": new_dead_letter,
        "drain_seconds": round(drain_seconds, 1),
        "scoring_throughput_tps": round(scored / max(ingest_seconds + drain_seconds, 0.01), 1),
        "end_to_end_latency_ms": {k: (int(v) if v is not None else None)
                                  for k, v in latency.items() if k != "scored"},
        # NFR-001's latency claim applies to the **sustained** rate only.
        # NFR-002 makes no latency claim, deliberately: absorbing a 5x burst in a
        # queue *means* latency grows. The system is advisory and not in the
        # money path (D7), so the designed failure mode under overload is a
        # slower decision, never a dropped transaction (NFR-004). Judging the
        # burst against the sustained latency target would be a category error.
        "nfr_001_p95_under_2s": (
            None if args.burst
            else bool(latency["p95_ms"] is not None and latency["p95_ms"] < TARGET_P95_MS)
        ),
        "nfr_002_zero_loss": lost == 0 and result.returncode == 0,
    }

    print("\n" + "=" * 62)
    print(f"  ingested            {verdict['ingested']}")
    print(f"  scored              {verdict['scored']}")
    print(f"  lost                {verdict['transactions_lost']}")
    print(f"  ingest rate         {verdict['achieved_ingest_tps']} TPS")
    print(f"  scoring throughput  {verdict['scoring_throughput_tps']} TPS")
    print(f"  end-to-end p50      {verdict['end_to_end_latency_ms']['p50_ms']} ms")
    print(f"  end-to-end p95      {verdict['end_to_end_latency_ms']['p95_ms']} ms  "
          f"(target < {TARGET_P95_MS})")
    print(f"  end-to-end max      {verdict['end_to_end_latency_ms']['max_ms']} ms")
    print("=" * 62)
    if args.burst:
        print("  NFR-001 p95 < 2s        n/a — NFR-002 makes no latency claim.")
        print("                          The queue absorbing a 5x burst means")
        print("                          latency grows; that is the design.")
    else:
        print(f"  NFR-001 p95 < 2s        {'PASS' if verdict['nfr_001_p95_under_2s'] else 'FAIL'}")
    print(f"  NFR-002 zero loss       {'PASS' if verdict['nfr_002_zero_loss'] else 'FAIL'}")
    if args.burst:
        print()
        print(
            f"  Backlog cleared in {verdict['drain_seconds']}s at "
            f"{verdict['scoring_throughput_tps']} TPS with the workers running."
        )
        print("  Clearing it faster is a matter of running more workers — which is")
        print("  what SELECT ... FOR UPDATE SKIP LOCKED buys, and costs no new")
        print("  infrastructure (D8a).")

    out = REPO_ROOT / args.out
    out.parent.mkdir(parents=True, exist_ok=True)
    existing = json.loads(out.read_text()) if out.exists() else []
    if not isinstance(existing, list):
        existing = [existing]
    existing.append(verdict)
    out.write_text(json.dumps(existing, indent=2), encoding="utf-8")
    print(f"\n  recorded in {out.relative_to(REPO_ROOT)}")


if __name__ == "__main__":
    main()
