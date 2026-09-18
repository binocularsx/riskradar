"""Simulator command line.

    python -m riskradar_sim corpus  --profile training --out ml/data/corpus.jsonl
    python -m riskradar_sim history --profile demo        # seed the demo database
    python -m riskradar_sim stream  --rate 3              # live feed for the demo
    python -m riskradar_sim burst   --tps 250 --seconds 60  # NFR-002
    python -m riskradar_sim industry-flag --count 3       # another bank flags BVNs (D75)

``history`` posts through the batch endpoint with replay semantics (D8d), so
seeding a month of behaviour does not raise a month of alerts. ``stream`` posts
one at a time through the same public endpoint a bank would use.
"""

from __future__ import annotations

import argparse
import heapq
import itertools
import json
import random
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import httpx

from .client import PacedClient
from .engine import legitimate_event
from .generate import SimulationConfig, generate, write_corpus
from .identity import DEFAULT_CORE_FILE, CoreFile
from .signals import EventLayer
from .population import build_population

DEFAULT_BASE_URL = "http://127.0.0.1:8000"
DEFAULT_API_KEY = "rr_dev_simulator_key_do_not_use_in_production"


def _client(base_url: str, api_key: str) -> httpx.Client:
    return httpx.Client(
        base_url=base_url, headers={"X-API-Key": api_key}, timeout=60.0
    )


def cmd_corpus(args: argparse.Namespace) -> None:
    config = SimulationConfig.profile(args.profile)
    if args.seed is not None:
        config.seed = args.seed
    if args.days is not None:
        # D58: the binding constraint on every comparison in the evaluation is
        # the number of fraud incidents — 153 gives Wilson intervals about 0.17
        # wide, which is wider than the differences we were drawing conclusions
        # from. Lengthening the window raises incidents and legitimate traffic
        # together, so the fraud rate stays at the ~0.3% reference point while
        # the intervals narrow with the square root of the count.
        config.days = args.days
    if args.anchor:
        config.end = datetime.fromisoformat(args.anchor)
    out = Path(args.out)
    started = time.perf_counter()
    counts = write_corpus(config, out)
    elapsed = time.perf_counter() - started
    print(f"wrote {out} in {elapsed:.1f}s")
    for key, value in counts.items():
        print(f"  {key:<28} {value}")


def cmd_history(args: argparse.Namespace) -> None:
    """Seed the database with prior behaviour, then a recent alerting window.

    Two phases, and the split is the point:

    * Everything older than ``--alerting-tail-hours`` is posted in **replay mode**
      (``is_replay=true``, ``raise_alerts=false``, D8d). Behavioural baselines
      need history to exist, and seeding a month of behaviour must not
      manufacture a month of alerts.
    * The recent tail is posted normally, so it scores, alerts and correlates
      into the case queue the demo actually shows.
    """
    config = SimulationConfig.profile(args.profile)
    if args.seed is not None:
        config.seed = args.seed
    # D68: a shared anchor lets a later run rebuild exactly the same timeline.
    anchor = datetime.fromisoformat(args.anchor) if args.anchor else datetime.now(timezone.utc)
    config.end = anchor

    cutoff = anchor - timedelta(hours=args.alerting_tail_hours)
    counters = {"history": 0, "live": 0, "duplicates": 0, "rejected": 0, "events": 0}
    # The truth about the alerting window, kept on this machine only. It never
    # travels to the API (labels are not part of the ingestion contract); the
    # demo reset reads it so seeded outcomes follow what really happened.
    labels: list[dict] = []
    batches: dict[bool, list[dict]] = {True: [], False: []}
    # D77: non-payment events, posted before the payments they precede.
    event_batches: dict[bool, list[dict]] = {True: [], False: []}
    started = time.perf_counter()

    core = CoreFile(Path(args.core_file))

    # D87: paced, and patient. When the scoring queue is full the API answers
    # 503 with Retry-After, and history waits for the workers instead of failing.
    with PacedClient(args.base_url, args.api_key, rate=args.max_rate, max_retries=720) as client:
        def _sent(response: httpx.Response | None, what: str) -> dict:
            if response is None:
                raise SystemExit(f"{what}: the API never accepted the batch ({client.stats.errors[-1:]}); "
                                 "are the scoring workers running?")
            response.raise_for_status()
            return response.json()

        def flush_events(is_history: bool) -> None:
            batch = event_batches[is_history]
            if not batch:
                return
            core.ensure([e["customer_id"] for e in batch])
            response = client.post("/v1/events/batch", json={"events": batch, "is_replay": is_history,
                                                             "raise_alerts": not is_history}, cost=len(batch))
            body = _sent(response, "events")
            counters["events"] += body["accepted"]
            if body["errors"]:
                print("  event errors:", body["errors"][:3], file=sys.stderr)
            event_batches[is_history] = []

        def flush(is_history: bool) -> None:
            # Events first: a payment's features read the events before it.
            flush_events(is_history)
            batch = batches[is_history]
            if not batch:
                return
            # D75: the core knows these customers before their payments arrive.
            core.ensure([p["customer_id"] for p in batch])
            response = client.post(
                "/v1/transactions/batch",
                json={
                    "transactions": batch,
                    "is_replay": is_history,
                    "raise_alerts": not is_history,
                },
                cost=len(batch),
            )
            body = _sent(response, "transactions")
            counters["history" if is_history else "live"] += body["accepted"]
            counters["duplicates"] += body["duplicates"]
            counters["rejected"] += body["rejected"]
            if body["errors"]:
                print("  errors:", body["errors"][:3], file=sys.stderr)
            batches[is_history] = []

        for event in generate(config, with_events=True, with_credits=True):
            is_history = datetime.fromisoformat(event.payload["occurred_at"]) < cutoff
            if not is_history and args.skip_tail:
                # History only: the alerting window is posted by a later run
                # with the same seed and anchor, after thresholds are derived.
                continue
            if is_history and args.only_tail:
                # The caller already loaded history in an earlier pass and just
                # wants the recent alerting window. Generating the earlier days
                # and throwing them away costs minutes for nothing.
                continue
            if event.kind not in ("PAYMENT", "CREDIT"):
                # Credits are transactions (D78) and travel with the payments.
                event_batches[is_history].append(event.payload)
                if len(event_batches[is_history]) >= args.batch_size:
                    flush_events(is_history)
                continue
            batches[is_history].append(event.payload)
            if not is_history and args.labels_out:
                labels.append({
                    "transaction_ref": event.payload["transaction_ref"],
                    "is_fraud": event.is_fraud,
                    "typology": event.typology,
                    "incident_id": event.incident_id,
                })
            if len(batches[is_history]) >= args.batch_size:
                flush(is_history)
                print(f"\r  history {counters['history']}  live {counters['live']}",
                      end="", flush=True)
        flush(True)
        flush(False)
        flush_events(True)
        flush_events(False)

    if args.labels_out:
        out = Path(args.labels_out)
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text("".join(json.dumps(r) + "\n" for r in labels), encoding="utf-8")
        print(f"\n  truth for {len(labels)} alerting-window transactions -> {out}")

    elapsed = time.perf_counter() - started
    total = counters["history"] + counters["live"]
    print(
        f"\nseeded {total} transactions in {elapsed:.1f}s "
        f"({total / max(elapsed, 0.01):.0f}/s)\n"
        f"  {counters['history']} as replayed history (no alerts)\n"
        f"  {counters['live']} in the last {args.alerting_tail_hours}h (alerting)\n"
        f"  {counters['duplicates']} duplicate, {counters['rejected']} rejected"
    )


def _at(event) -> datetime:
    return datetime.fromisoformat(event.payload["occurred_at"])


def _retime(events: list, start: datetime, speed: float) -> list:
    """Compress an incident's timeline around its start (``--incident-speed``)."""
    if speed == 1.0:
        return events
    for e in events:
        at = _at(e)
        if at > start:
            e.payload["occurred_at"] = (start + (at - start) / speed).isoformat()
    return events


def cmd_stream(args: argparse.Namespace) -> None:
    """A live feed that behaves like a bank's switch (D87).

    Ordinary payments arrive as a Poisson process at ``--rate`` a second, each
    stamped with the moment it is sent. Now and then a fraud incident begins.
    Its events are **scheduled, not fired at once**: a takeover whose payments
    happen over the next three hours is sent over the next three hours, each
    when its time comes. The API refuses payments dated in the future, and
    velocity features are only honest if events arrive in their own time.
    ``--incident-speed 10`` compresses incidents tenfold for a short demo, at
    the cost of making them look faster than real fraud does.

    Everything goes through a paced client: never faster than ``--max-rate``,
    slower when the service says it is under pressure, and every refusal is
    counted and shown.
    """
    config = SimulationConfig.profile("demo")
    rng = random.Random(args.seed if args.seed is not None else int(time.time()))
    customers = build_population(rng, n_customers=config.n_customers,
                                 now=datetime.now(timezone.utc) - timedelta(days=30))

    from .engine import TYPOLOGIES

    # D75: the whole live population is known to the simulated core up front.
    CoreFile().ensure([c.customer_id for c in customers])
    layer = EventLayer(args.seed if args.seed is not None else int(time.time()), customers)

    pending: list = []            # (due, seq, event), a min-heap on due time
    seq = itertools.count()
    incidents = 0
    started = time.monotonic()
    next_arrival = started
    last_print = 0.0

    def schedule(events: list) -> None:
        for e in events:
            heapq.heappush(pending, (_at(e), next(seq), e))

    with PacedClient(args.base_url, args.api_key, rate=args.max_rate) as client:
        try:
            while args.minutes is None or time.monotonic() - started < args.minutes * 60:
                clock = time.monotonic()
                if clock >= next_arrival:
                    next_arrival = clock + rng.expovariate(args.rate)
                    now = datetime.now(timezone.utc)
                    if rng.random() < args.incident_probability:
                        typology = args.typology or rng.choice(list(TYPOLOGIES))
                        victim = rng.choice(customers)
                        incident = list(TYPOLOGIES[typology](rng, victim, now, customers=customers))
                        # D77: the prelude (logins, device, payees) is part of the story.
                        schedule(_retime(layer.around(incident) + incident, now, args.incident_speed))
                        incidents += 1
                        last = max(_at(e) for e in incident)
                        print(f"\n  {typology} incident begins; its last payment is due "
                              f"{last.astimezone().strftime('%H:%M')} ({incidents} so far)")
                    else:
                        customer = rng.choice(customers)
                        account = rng.choice(customer.accounts)
                        event = legitimate_event(rng, customer, account, now)
                        account.last_activity_at = now
                        schedule(layer.around([event]) + [event])

                # Send whatever is due, in time order.
                now = datetime.now(timezone.utc)
                while pending and pending[0][0] <= now:
                    _, _, event = heapq.heappop(pending)
                    path = "/v1/transactions" if event.kind in ("PAYMENT", "CREDIT") else "/v1/events"
                    client.post(path, json=event.payload)

                if clock - last_print > 2:
                    last_print = clock
                    waiting = f", {len(pending)} scheduled" if pending else ""
                    slow = f", slowed x{client.slowdown:.1f}" if client.slowdown > 1.05 else ""
                    print(f"\r  {client.stats.line()}{waiting}{slow}   ", end="", flush=True)

                wake = next_arrival - time.monotonic()
                if pending:
                    wake = min(wake, (pending[0][0] - datetime.now(timezone.utc)).total_seconds())
                time.sleep(min(max(wake, 0.0), 0.5))
        except KeyboardInterrupt:
            pass
    print(f"\nstopped: {client.stats.line()}, {incidents} incidents begun, {len(pending)} events not yet due")
    for err in client.stats.errors[:5]:
        print(f"  refused: {err}", file=sys.stderr)


def cmd_burst(args: argparse.Namespace) -> None:
    """NFR-002: a burst at 5x sustained for 60 seconds, absorbed with zero loss.

    Uses the batch endpoint because the claim under test is that **the queue**
    absorbs the burst — not that HTTP keep-alive is fast.
    """
    config = SimulationConfig.profile("demo")
    rng = random.Random(args.seed or 7)
    customers = build_population(rng, n_customers=2_000,
                                 now=datetime.now(timezone.utc) - timedelta(days=30))

    target = args.tps * args.seconds
    print(f"burst: {args.tps} TPS for {args.seconds}s = {target} transactions")

    sent = 0
    accepted = 0
    started = time.perf_counter()
    with _client(args.base_url, args.api_key) as client:
        while sent < target:
            deadline = started + (sent / args.tps)
            batch = []
            for _ in range(min(args.batch_size, target - sent)):
                customer = rng.choice(customers)
                account = rng.choice(customer.accounts)
                batch.append(
                    legitimate_event(rng, customer, account, datetime.now(timezone.utc)).payload
                )
            response = client.post(
                "/v1/transactions/batch",
                json={"transactions": batch, "is_replay": False, "raise_alerts": True},
            )
            response.raise_for_status()
            accepted += response.json()["accepted"]
            sent += len(batch)
            drift = deadline - time.perf_counter()
            if drift > 0:
                time.sleep(drift)
            print(f"\r  sent {sent}/{target}", end="", flush=True)

    elapsed = time.perf_counter() - started
    print(
        f"\nburst complete: {sent} sent, {accepted} accepted in {elapsed:.1f}s "
        f"({sent / elapsed:.0f} TPS achieved)"
    )
    if accepted != sent:
        print(f"  LOSS: {sent - accepted} transactions were not accepted", file=sys.stderr)
        raise SystemExit(1)
    print("  zero transaction loss")


def cmd_industry_flag(args: argparse.Namespace) -> None:
    """Another institution flags some of our customers' BVNs on the industry list.

    Posts to the inbound door a bank-side NIBSS connector would use, so the
    desk sees "flagged by another bank" on the cases those customers have.
    """
    import csv

    rows = list(csv.DictReader(Path(args.core_file).open(newline="", encoding="utf-8")))
    if not rows:
        sys.exit(f"no customers in {args.core_file}; run `history` first")
    rng = random.Random(args.seed)
    now = datetime.now(timezone.utc)
    entries = [
        {
            "external_ref": f"{args.institution}-{r['bvn']}-{int(now.timestamp())}",
            "bvn": r["bvn"],
            "institution_code": args.institution,
            "reason_code": "SUSPECTED_FRAUD_PENDING_CLARIFICATION",
            "flagged_at": now.isoformat(),
            "expires_at": (now + timedelta(hours=min(args.hours, 24.0))).isoformat(),
        }
        for r in rng.sample(rows, min(args.count, len(rows)))
    ]
    with _client(args.base_url, args.api_key) as client:
        response = client.post("/v1/industry-watchlist/inbound", json={"entries": entries})
        response.raise_for_status()
    print(f"{args.institution} flagged {len(entries)} BVN(s): {response.json()}")


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(prog="riskradar_sim", description="Risk Radar simulator")
    parser.add_argument("--base-url", default=DEFAULT_BASE_URL)
    parser.add_argument("--api-key", default=DEFAULT_API_KEY)
    parser.add_argument("--seed", type=int, default=None)
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("corpus", help="write a labelled JSONL corpus for training")
    p.add_argument("--profile", default="training", choices=["demo", "training", "reference"])
    p.add_argument("--out", default="ml/data/corpus.jsonl")
    p.add_argument(
        "--days", type=int, default=None,
        help="override the profile's window. Raises incidents and legitimate "
             "traffic together, so the fraud rate is unchanged (D58)",
    )
    p.add_argument("--anchor", default=None,
                   help="ISO time the corpus ends at, so the same seed rebuilds it byte for byte (D78)")
    p.set_defaults(func=cmd_corpus)

    p = sub.add_parser("history", help="seed the database with replayed history")
    p.add_argument("--profile", default="demo", choices=["demo", "training", "reference"])
    p.add_argument("--batch-size", type=int, default=500)
    p.add_argument(
        "--alerting-tail-hours", type=float, default=12.0,
        help="transactions newer than this are scored with alerting enabled",
    )
    p.add_argument(
        "--only-tail", action="store_true",
        help="skip the older history entirely; post only the alerting window",
    )
    p.add_argument(
        "--skip-tail", action="store_true",
        help="post only the history before the alerting window",
    )
    p.add_argument(
        "--anchor", default=None,
        help="ISO time the simulated world ends at; share it between runs",
    )
    p.add_argument(
        "--labels-out", default=None,
        help="write the true labels of the alerting window to this local file",
    )
    p.add_argument("--core-file", default=str(DEFAULT_CORE_FILE),
                   help="the simulated core's customer file (customer_id,bvn,nin), D75")
    p.add_argument("--max-rate", type=float, default=2000.0,
                   help="transactions a second at most; the API's replay allowance is 2000 (D87)")
    p.set_defaults(func=cmd_history)

    p = sub.add_parser("stream", help="live feed for the demo")
    p.add_argument("--rate", type=float, default=2.0, help="ordinary payments a second, on average")
    p.add_argument("--max-rate", type=float, default=20.0,
                   help="hard ceiling on requests a second, whatever is due (D87)")
    p.add_argument("--minutes", type=float, default=None, help="stop after this long (default: until Ctrl+C)")
    p.add_argument("--incident-speed", type=float, default=1.0,
                   help="compress each incident's timeline by this factor (1 = real time)")
    # Each "incident" emits 8-25 transactions, so this knob is not the fraud
    # rate — it is far more sensitive than it looks. At 0.03 the live feed is
    # roughly 27% fraud, which is absurd and makes the alert queue meaningless.
    # 0.002 lands near 2%: still ~6x the real operating point of 0.3% (D23), but
    # frequent enough that somebody watching a demo sees an incident every few
    # minutes. The honest figures come from the training profile, never this one.
    p.add_argument("--incident-probability", type=float, default=0.002)
    p.add_argument("--typology", default=None,
                   choices=["ACCOUNT_TAKEOVER", "MULE_FANOUT", "CARD_TESTING", "SOCIAL_ENGINEERING",
                            "SIM_SWAP", "DORMANT_ACCOUNT", "CARD_CLONING"])
    p.set_defaults(func=cmd_stream)

    p = sub.add_parser("industry-flag", help="play another bank: flag BVNs on the industry watch-list (D75)")
    p.add_argument("--count", type=int, default=3)
    p.add_argument("--institution", default="SIMBANK-044")
    p.add_argument("--hours", type=float, default=24.0)
    p.add_argument("--core-file", default=str(DEFAULT_CORE_FILE))
    p.set_defaults(func=cmd_industry_flag)

    p = sub.add_parser("burst", help="NFR-002 burst test")
    p.add_argument("--tps", type=int, default=250)
    p.add_argument("--seconds", type=int, default=60)
    p.add_argument("--batch-size", type=int, default=250)
    p.set_defaults(func=cmd_burst)

    args = parser.parse_args(argv)
    args.func(args)


if __name__ == "__main__":
    main()
