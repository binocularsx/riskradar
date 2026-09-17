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
   one at a time, in their original order, at ``--rate`` a second, stamped with
   the moment they are sent. These score, alert and open cases while you watch.

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

import httpx
import numpy as np
import pandas as pd

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "backend"))
sys.path.insert(0, str(REPO_ROOT / "ml"))

from byod import canonical  # noqa: E402
from byod.mapping import Mapping  # noqa: E402

API_KEY = "rr_dev_simulator_key_do_not_use_in_production"


def _raw_id(token: str | None) -> str | None:
    """Canonical ids carry a kind prefix ("a:", "b:"); the API wants the bank's own id."""
    if token is None or token is pd.NA or (isinstance(token, float) and np.isnan(token)):
        return None
    return str(token)[2:][:128]


CURRENCY = "NGN"


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
    parser.add_argument("--rate", type=float, default=3.0, help="live transactions a second")
    parser.add_argument("--base-url", default="http://127.0.0.1:8000")
    parser.add_argument("--labels-out", default=None)
    parser.add_argument("--seed", type=int, default=20260917)
    parser.add_argument("--skip-history", action="store_true")
    parser.add_argument("--currency", default="NGN", help="ISO 4217 code of the file's amounts (IEEE-CIS: USD)")
    parser.add_argument("--history-only", action="store_true",
                        help="post the history and stop, so thresholds can be derived before the live part")
    args = parser.parse_args()

    global CURRENCY
    CURRENCY = args.currency.upper()
    path = Path(args.data)
    mapping = Mapping.load(Path(args.mapping))
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

    with httpx.Client(base_url=args.base_url, headers={"X-API-Key": API_KEY}, timeout=60.0) as client:
        if not args.skip_history:
            started = time.perf_counter()
            batch: list[dict] = []
            sent = 0
            for _, r in history.iterrows():
                batch.append(payload(r, (r["occurred_at"] + shift).to_pydatetime(), f"{path.stem[:20]}-{int(r['row_id'])}"))
                if len(batch) == 500:
                    resp = client.post("/v1/transactions/batch", json={"transactions": batch, "is_replay": True,
                                                                      "raise_alerts": False})
                    resp.raise_for_status()
                    sent += len(batch)
                    batch = []
                    print(f"\r  history {sent:,}/{len(history):,}", end="", flush=True)
            if batch:
                client.post("/v1/transactions/batch", json={"transactions": batch, "is_replay": True,
                                                            "raise_alerts": False}).raise_for_status()
            print(f"\n  history posted in {time.perf_counter() - started:.0f}s", flush=True)

        if args.history_only:
            print("  history only: derive thresholds, then run again with --skip-history", flush=True)
            return
        print(f"  streaming {len(live):,} live transactions at {args.rate:g} a second; open the dashboard", flush=True)
        interval = 1.0 / max(args.rate, 0.01)
        fraud_sent = 0
        for i, r in live.iterrows():
            when = datetime.now(timezone.utc)
            resp = client.post("/v1/transactions", json=payload(r, when, f"{path.stem[:20]}-{int(r['row_id'])}"))
            if resp.status_code >= 400:
                print(f"\n  rejected row {int(r['row_id'])}: {resp.status_code} {resp.text[:200]}", flush=True)
            fraud_sent += int(r["is_fraud"] == 1)
            if i % 25 == 0:
                print(f"\r  live {i + 1:,}/{len(live):,} (labelled fraud so far {fraud_sent})", end="", flush=True)
            time.sleep(interval)
        print(f"\n  done; labels in {labels}", flush=True)


if __name__ == "__main__":
    main()
