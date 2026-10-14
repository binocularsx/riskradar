"""Feed a real transaction file into the running system, so the desk can watch it arrive (D81).

    python scripts/replay_dataset.py DATA.csv --mapping DATA.mapping.yaml \
        --customers 3000 --history-days 30 --rate 3

Plain English
-------------
``ml/evaluate_dataset.py`` measures a dataset offline. This does the other half:
it pushes the same file through the public API a bank would use, into the live
database, where the workers score it, the rules fire, cases open and the
dashboard shows them.

It happens in two phases, like the demo builder:

1. **History, quietly.** A sample of whole customers (never rows) is posted in
   replay mode with alerting off, so behavioural baselines exist. Times are
   shifted so the file's history ends just before now: a transaction from
   14 March 2023 becomes one from a few weeks ago, with every gap between
   transactions preserved.
2. **Live.** The file's next transactions for the same customers are then posted
   one at a time, in their original order. These score, alert and open cases
   while you watch.

How the live part keeps time (D87)
----------------------------------
``--pace timelapse`` (the default) plays the file faster than it happened but
never lies about when anything happened. Each transaction keeps its own time
from the file, shifted so the file's clock meets the wall clock at the moment
the stream ends; it is sent after the same gap as in the file, divided by
``--speed``. Velocity features therefore see the file's real gaps: an hour of
card use is still an hour, it just arrives in a minute at ``--speed 60``.

``--pace now`` is the old behaviour: a fixed ``--rate`` a second, each stamped
with the moment it is sent. It squeezes a day of a customer's payments into
minutes, so every per-hour count inflates; use it only to load-test.

Either way the client is paced and listens to the API: it slows down when told
the scoring queue is under pressure and waits out ``429``/``503``.

The mapping is the same reviewed YAML the offline evaluator uses, so what the
system reads is exactly what was measured. Labels are never sent: the API has
no field for them. They are written beside the run (``--labels-out``) so the
alerts can be checked afterwards.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import numpy as np
import pandas as pd

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "backend"))
sys.path.insert(0, str(REPO_ROOT / "ml"))
sys.path.insert(0, str(REPO_ROOT / "simulator"))

from riskradar_sim.client import PacedClient  # noqa: E402

from byod import canonical  # noqa: E402
from byod.mapping import Mapping  # noqa: E402

API_KEY = "rr_dev_simulator_key_do_not_use_in_production"


def _raw_id(token: str | None) -> str | None:
    """Canonical ids carry a kind prefix ("a:", "b:"); the API wants the bank's own id."""
    if token is None or token is pd.NA or (isinstance(token, float) and np.isnan(token)):
        return None
    return str(token)[2:][:128]


CURRENCY = "NGN"
DATASET = "dataset"


def display_name(row: pd.Series) -> str:
    """Something a person can say aloud: the file's name and a short stable handle."""
    handle = str(row["subject_token"])[2:]
    return f"{DATASET} customer {handle[-6:]}"[:128]


def payload(row: pd.Series, when: datetime, ref: str) -> dict:
    inbound = row["direction"] == "INBOUND"
    body = {
        "transaction_ref": ref[:128],
        "occurred_at": when.isoformat(),
        "amount_minor": int(row["amount_minor"]),
        "currency": CURRENCY,
        "channel": row["channel"],
        "instrument": row["instrument"],
        "rail": "CARD_SCHEME" if row["instrument"] == "CARD" else "NIP",
        "customer_id": _raw_id(row["subject_token"]),
        "account_id": _raw_id(row["account_token"]),
        "beneficiary_account_id": None if inbound else _raw_id(row["beneficiary_token"]),
        "device_fingerprint": _raw_id(row["device_token"]),
        "ip_region": (str(row["ip_region"])[:32] if isinstance(row["ip_region"], str) else None),
        "merchant_category": (str(row["merchant_category"])[:16] if isinstance(row["merchant_category"], str) else None),
        "auth_result": row["auth_result"],
        "direction": row["direction"],
        "display_name": display_name(row),
    }
    if inbound:
        body["remitter_account_id"] = _raw_id(row["remitter_token"]) or "unknown-remitter"
    return body


def main() -> None:
    parser = argparse.ArgumentParser(description="Replay a dataset into the live system")
    parser.add_argument("data")
    parser.add_argument("--mapping", required=True)
    parser.add_argument("--customers", type=int, default=3000, help="whole customers to sample")
    parser.add_argument("--history-days", type=float, default=30)
    parser.add_argument("--live", type=int, default=5000, help="transactions to stream live")
    parser.add_argument("--pace", choices=["timelapse", "now"], default="timelapse",
                        help="timelapse keeps the file's gaps and times; now stamps each at send time")
    parser.add_argument("--speed", type=float, default=60.0,
                        help="timelapse: how many times faster than the file (60 = an hour a minute)")
    parser.add_argument("--rate", type=float, default=3.0,
                        help="now: transactions a second; timelapse: the most a second, whatever is due")
    parser.add_argument("--base-url", default="http://127.0.0.1:8000")
    parser.add_argument("--labels-out", default=None)
    parser.add_argument("--seed", type=int, default=20260917)
    parser.add_argument("--skip-history", action="store_true")
    parser.add_argument("--currency", default="NGN", help="ISO 4217 code of the file's amounts (IEEE-CIS: USD)")
    parser.add_argument("--history-only", action="store_true",
                        help="post the history and stop, so thresholds can be derived before the live part")
    args = parser.parse_args()

    global CURRENCY, DATASET
    CURRENCY = args.currency.upper()
    path = Path(args.data)
    mapping = Mapping.load(Path(args.mapping))
    DATASET = mapping.dataset.split("(")[0].strip() or path.stem
    if not mapping.reviewed:
        raise SystemExit("the mapping is not reviewed; see ml/evaluate_dataset.py suggest")

    # Sample whole customers: pick keys first, then read only their rows.
    key = canonical._key_column(mapping)
    print(f"choosing {args.customers:,} customers by `{key}` ...", flush=True)
    counts: dict = {}
    for chunk in canonical.read_table(path, usecols=[key], chunksize=500_000):
        for k, n in chunk[key].value_counts().items():
            counts[k] = counts.get(k, 0) + int(n)
    rng = np.random.default_rng(args.seed)
    keys = list(counts)
    chosen = set(rng.choice(np.array(keys, dtype=object), size=min(args.customers, len(keys)), replace=False))
    usecols = sorted(mapping.columns_used())
    raw = pd.concat([c[c[key].isin(chosen)] for c in canonical.read_table(path, usecols=usecols, chunksize=500_000)],
                    ignore_index=True)
    df = canonical.canonicalise(raw, mapping)
    print(f"  {len(df):,} transactions for {len(chosen):,} customers", flush=True)

    times = df["occurred_at"]
    live = df.tail(args.live).reset_index(drop=True)
    boundary = live["occurred_at"].iloc[0]
    history = df[(times < boundary) & (times >= boundary - pd.Timedelta(days=args.history_days))]
    now = pd.Timestamp.now(tz="UTC")
    span = live["occurred_at"].iloc[-1] - boundary
    if args.pace == "timelapse":
        # The file's clock meets the wall clock when the stream ends, so no
        # transaction is ever dated after the moment it is sent.
        shift = (now + span / args.speed) - live["occurred_at"].iloc[-1]
        print(f"  timelapse x{args.speed:g}: {span} of the file plays in {span / args.speed}", flush=True)
    else:
        shift = now - boundary
    print(f"  history {len(history):,} rows ({args.history_days:g} days before the live part), "
          f"live {len(live):,} rows; times shifted by {shift.days} days", flush=True)

    labels = Path(args.labels_out) if args.labels_out else REPO_ROOT / "ml" / "artifacts" / "datasets" / f"{path.stem}.replay-labels.jsonl"
    labels.parent.mkdir(parents=True, exist_ok=True)
    with labels.open("w", encoding="utf-8") as fh:
        for part, frame in (("history", history), ("live", live)):
            for _, r in frame.iterrows():
                fh.write(json.dumps({"transaction_ref": f"{path.stem[:20]}-{int(r['row_id'])}", "part": part,
                                     "is_fraud": int(r["is_fraud"]), "fraud_type": r["fraud_type"]}) + "\n")

    ref = lambda r: f"{path.stem[:20]}-{int(r['row_id'])}"  # noqa: E731
    if not args.skip_history:
        started = time.perf_counter()
        with PacedClient(args.base_url, API_KEY, rate=2000, max_retries=720) as client:
            batch: list[dict] = []
            for _, r in history.iterrows():
                batch.append(payload(r, (r["occurred_at"] + shift).to_pydatetime(), ref(r)))
                if len(batch) == 500:
                    _post_batch(client, batch)
                    batch = []
                    print(f"\r  history {client.stats.line()}", end="", flush=True)
            if batch:
                _post_batch(client, batch)
        print(f"\n  history posted in {time.perf_counter() - started:.0f}s: {client.stats.line()}", flush=True)

    if args.history_only:
        print("  history only: derive thresholds, then run again with --skip-history", flush=True)
        return

    fraud_sent = 0
    wall0 = time.monotonic()
    with PacedClient(args.base_url, API_KEY, rate=args.rate if args.pace == "now" else max(args.rate, 10.0)) as client:
        print(f"  streaming {len(live):,} live transactions ({args.pace}); open the dashboard", flush=True)
        try:
            for i, r in live.iterrows():
                if args.pace == "timelapse":
                    due = wall0 + (r["occurred_at"] - boundary).total_seconds() / args.speed
                    wait = due - time.monotonic()
                    if wait > 0:
                        time.sleep(wait)
                    when = (r["occurred_at"] + shift).to_pydatetime()
                    # Never ahead of the sender's own clock, whatever rounding did.
                    when = min(when, datetime.now(timezone.utc))
                else:
                    when = datetime.now(timezone.utc)
                client.post("/v1/transactions", json=payload(r, when, ref(r)))
                fraud_sent += int(r["is_fraud"] == 1)
                if i % 25 == 0:
                    print(f"\r  live {i + 1:,}/{len(live):,}: {client.stats.line()}, labelled fraud so far "
                          f"{fraud_sent}", end="", flush=True)
        except KeyboardInterrupt:
            print("\n  stopped by hand", flush=True)
        print(f"\n  done: {client.stats.line()}; labels in {labels}", flush=True)
        for err in client.stats.errors[:5]:
            print(f"  refused: {err}", flush=True)


def _post_batch(client: PacedClient, batch: list[dict]) -> None:
    response = client.post("/v1/transactions/batch", json={"transactions": batch, "is_replay": True,
                                                          "raise_alerts": False}, cost=len(batch))
    if response is None:
        raise SystemExit(f"history: the API never accepted a batch ({client.stats.errors[-1:]}); "
                         "are the scoring workers running?")
    response.raise_for_status()


if __name__ == "__main__":
    main()
