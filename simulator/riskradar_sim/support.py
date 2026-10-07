"""The simulator plays the bank's support team (D109a).

The fraud desk is not customer-facing; the support team is. For the demo the
simulator stands in for it, through the same public API a real support system
would use:

* ``report`` — a defrauded customer calls support, and support forwards the
  report with the customer record (``POST /v1/support/reports``). The victims
  come from ``stream``, which notes each fraud payment it sends, as a real
  victim would know which payments were not theirs. Nothing here reads the
  detector, so the generator/detector wall (D10a) holds: support knows what
  the *customer* knows.
* ``work`` — support carries out what the desk asked of it and says so
  (``GET /v1/restrictions`` then ``POST /v1/restrictions/{ref}/ack``). Most
  actions are done; a few are not, with a reason, because a real desk has
  customers it cannot reach.
* ``contact`` — support reached a watch-flagged customer
  (``POST /v1/support/watchlist/{case_id}/contact``).
"""

from __future__ import annotations

import json
import random
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import httpx

LOGS = Path(__file__).resolve().parents[2] / "logs"
VICTIMS = LOGS / "sim_fraud_victims.jsonl"
STATE = LOGS / "sim_support_state.json"

NOT_DONE_REASONS = [
    "customer could not be reached on any safe channel",
    "account already closed by the customer",
    "receiving bank did not respond",
]


def note_victim(event: Any) -> None:
    """Called by ``stream`` for each fraud payment it sends."""
    p = event.payload
    if event.kind != "PAYMENT" or not event.is_fraud or p.get("direction", "OUTBOUND") != "OUTBOUND":
        return
    LOGS.mkdir(exist_ok=True)
    with VICTIMS.open("a", encoding="utf-8") as f:
        f.write(json.dumps({"transaction_ref": p["transaction_ref"], "customer_id": p["customer_id"],
                            "typology": event.typology, "occurred_at": p.get("occurred_at"),
                            "sent_at": datetime.now(timezone.utc).isoformat()}) + "\n")


def _state() -> dict[str, Any]:
    try:
        return json.loads(STATE.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {"reported_refs": [], "after_id": 0}


def _save(state: dict[str, Any]) -> None:
    LOGS.mkdir(exist_ok=True)
    STATE.write_text(json.dumps(state), encoding="utf-8")


def forward_reports(client: httpx.Client, *, count: int, min_age_minutes: float, rng: random.Random) -> int:
    """Forward ``count`` customers' reports, oldest victims first."""
    if not VICTIMS.exists():
        print("no victims yet: run `stream` first so some fraud happens")
        return 0
    state = _state()
    done = set(state["reported_refs"])
    cutoff = datetime.now(timezone.utc) - timedelta(minutes=min_age_minutes)
    by_customer: dict[str, list[dict[str, Any]]] = {}
    for line in VICTIMS.read_text(encoding="utf-8").splitlines():
        v = json.loads(line)
        if v["transaction_ref"] in done or datetime.fromisoformat(v["sent_at"]) > cutoff:
            continue
        by_customer.setdefault(v["customer_id"], []).append(v)

    sent = 0
    for customer_id, payments in list(by_customer.items())[:count]:
        refs = [p["transaction_ref"] for p in payments][:10]
        ticket = f"SUP-{rng.randint(100000, 999999)}"
        r = client.post("/v1/support/reports", json={
            "support_ticket_ref": ticket, "customer_id": customer_id, "transaction_refs": refs,
            "reported_at": datetime.now(timezone.utc).isoformat(),
            "channel": rng.choice(["CONTACT_CENTRE", "BRANCH", "MOBILE_APP", "EMAIL"]),
            "customer_statement": f"Customer says they did not make {len(refs)} payment(s) "
                                  f"and noticed when their balance dropped.",
        })
        if r.status_code == 409:
            print(f"  {ticket}: payments not scored yet, will retry next run")
            continue
        if r.status_code >= 400:
            print(f"  {ticket}: refused {r.status_code} {r.text[:200]}")
            continue
        out = r.json()
        print(f"  {ticket}: case(s) {out['cases']}, {len(out['missed_by_detector'])} payment(s) the detector missed")
        done.update(refs)
        sent += 1
    state["reported_refs"] = sorted(done)
    _save(state)
    return sent


def work_actions(client: httpx.Client, *, rng: random.Random, done_rate: float) -> int:
    """Carry out what the desk asked and acknowledge each action once."""
    state = _state()
    r = client.get("/v1/restrictions", params={"after_id": state["after_id"], "limit": 200})
    r.raise_for_status()
    body = r.json()
    for item in body["items"]:
        done = rng.random() < done_rate
        ack = client.post(f"/v1/restrictions/{item['restriction_ref']}/ack", json={
            "outcome": "APPLIED" if done else "NOT_APPLIED",
            "reason": None if done else rng.choice(NOT_DONE_REASONS),
        })
        word = "done" if done else "not done"
        print(f"  case {item.get('case_id')}: {item['action'].replace('_', ' ').lower()} - {word}"
              + ("" if ack.status_code < 400 else f" (ack refused {ack.status_code})"))
    state["after_id"] = body["next_after_id"]
    _save(state)
    return len(body["items"])


def record_contact(client: httpx.Client, *, case_id: int, outcome: str, note: str | None) -> None:
    r = client.post(f"/v1/support/watchlist/{case_id}/contact", json={"outcome": outcome, "note": note})
    print(f"  case {case_id}: {r.status_code} {r.text[:200]}")


def run(client: httpx.Client, args: Any) -> None:
    rng = random.Random(args.seed)
    if args.action == "report":
        forward_reports(client, count=args.count, min_age_minutes=args.min_age, rng=rng)
    elif args.action == "contact":
        record_contact(client, case_id=args.case_id, outcome=args.outcome, note=args.note)
    else:
        started = time.monotonic()
        while True:
            work_actions(client, rng=rng, done_rate=args.done_rate)
            if args.minutes is not None and time.monotonic() - started >= args.minutes * 60:
                break
            if args.once:
                break
            time.sleep(args.every)
