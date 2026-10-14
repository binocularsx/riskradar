"""D87: the simulator and replay behave like a well-mannered bank integration.

Pinned against a fake server: a 429 or 503 is waited out for as long as
Retry-After says and the transaction is sent again (safe, because a repeat is
a no-op); a slow-down header halves the pace; a 422 is counted as refused,
never retried and never dropped silently; and the API refuses a payment dated
in the future.
"""

from __future__ import annotations

import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "simulator"))

from riskradar_sim import client as paced  # noqa: E402


def _client(handler, **kw) -> paced.PacedClient:
    return paced.PacedClient("http://test", "k", rate=1000, transport=httpx.MockTransport(handler), **kw)


def test_refusals_are_waited_out_and_resent(monkeypatch):
    slept: list[float] = []
    monkeypatch.setattr(paced.time, "sleep", lambda s: slept.append(s))
    answers = iter([
        httpx.Response(429, headers={"Retry-After": "2"}),
        httpx.Response(503, headers={"Retry-After": "1"}),
        httpx.Response(202, json={"status": "accepted"}),
    ])
    c = _client(lambda request: next(answers))
    r = c.post("/v1/transactions", json={"x": 1})
    assert r.status_code == 202
    assert c.stats.accepted == 1 and c.stats.retries == 2 and c.stats.throttled == 1
    assert any(s >= 2 for s in slept) and any(1 <= s < 2 for s in slept)
    assert c.slowdown > 1.0  # a refusal also slows the pace


def test_backpressure_halves_the_pace_and_recovers():
    pressure = {"on": True}

    def handler(request):
        headers = {"X-RiskRadar-Queue-Depth": "2500"}
        if pressure["on"]:
            headers["X-RiskRadar-Backpressure"] = "slow-down"
        return httpx.Response(202, json={"status": "accepted"}, headers=headers)

    c = _client(handler)
    c.post("/v1/transactions", json={})
    assert c.slowdown == 2.0 and c.stats.last_queue_depth == 2500
    pressure["on"] = False
    for _ in range(30):
        c.post("/v1/transactions", json={})
    assert c.slowdown < 1.1


def test_a_rejected_payload_is_counted_not_retried():
    calls = []

    def handler(request):
        calls.append(1)
        return httpx.Response(422, json={"detail": "bad"})

    c = _client(handler)
    assert c.post("/v1/transactions", json={}).status_code == 422
    assert len(calls) == 1 and c.stats.rejected == 1 and c.stats.errors


def test_batches_count_what_the_server_says():
    c = _client(lambda r: httpx.Response(202, json={"accepted": 7, "duplicates": 2, "rejected": 1}))
    c.post("/v1/transactions/batch", json={}, cost=10)
    assert (c.stats.sent, c.stats.accepted, c.stats.duplicates, c.stats.rejected) == (10, 7, 2, 1)


def test_the_api_refuses_a_payment_from_the_future(client, api_headers, sample_transaction):
    ahead = (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat()
    r = client.post("/v1/transactions", json=sample_transaction(occurred_at=ahead), headers=api_headers)
    assert r.status_code == 422 and "future" in r.text
    slightly = (datetime.now(timezone.utc) + timedelta(seconds=30)).isoformat()
    assert client.post("/v1/transactions", json=sample_transaction(occurred_at=slightly),
                       headers=api_headers).status_code == 202
