"""Flow control at the front door: rate limits and backpressure (D87).

Plain English
-------------
Two things can overwhelm the service, and they need different answers.

* **A caller sending too fast.** Each API key has a token bucket: a steady
  rate a second plus a burst it may spend at once, and a separate, larger
  one for replayed history. A caller over its rate is
  told ``429`` with ``Retry-After``, the standard signal every HTTP client and
  gateway understands, and nothing it sent is half-written.
* **The workers falling behind.** Transactions are accepted into a queue and
  scored a moment later. If the queue grows because workers are slow or dead,
  accepting more only makes the backlog longer. Above a soft mark every
  response carries ``X-RiskRadar-Backpressure: slow-down``; above a hard mark
  new work is refused with ``503`` and ``Retry-After`` until the backlog
  drains. Replayed history is refused at its own, earlier mark: it can wait,
  a payment happening now should not wait behind it.

Every ingestion response carries the queue depth and the caller's remaining
allowance, so a well-behaved client (the simulator is one) paces itself
before it is ever refused.

The buckets live in this process. Behind several API processes each enforces
its share; a gateway in front is the place for a global limit.
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass
from typing import Any

from fastapi import HTTPException, Response, status

from ..config import settings


@dataclass
class _Bucket:
    tokens: float
    stamp: float


class TokenBuckets:
    def __init__(self) -> None:
        self._buckets: dict[Any, _Bucket] = {}
        self._lock = threading.Lock()

    def take(self, key: Any, cost: float, *, rate: float, burst: float) -> tuple[bool, float, float]:
        """Spend ``cost`` tokens. Returns (allowed, seconds until allowed, tokens left)."""
        cost = min(cost, burst)  # a batch larger than the burst waits for a full bucket, not forever
        now = time.monotonic()
        with self._lock:
            b = self._buckets.get(key)
            if b is None:
                b = self._buckets[key] = _Bucket(burst, now)
            b.tokens = min(burst, b.tokens + (now - b.stamp) * rate)
            b.stamp = now
            if b.tokens >= cost:
                b.tokens -= cost
                return True, 0.0, b.tokens
            return False, (cost - b.tokens) / rate if rate > 0 else 60.0, b.tokens

    def reset(self) -> None:
        with self._lock:
            self._buckets.clear()


buckets = TokenBuckets()


class QueueGauge:
    """The scoring queue's depth, read at most once a second per process."""

    TTL = 1.0

    def __init__(self) -> None:
        self._value: tuple[int, int] = (0, 0)
        self._stamp = 0.0
        self._lock = threading.Lock()

    def read(self, conn: Any) -> tuple[int, int]:
        """(live, total) waiting to be scored."""
        with self._lock:
            if time.monotonic() - self._stamp < self.TTL:
                return self._value
        with conn.cursor() as cur:
            cur.execute("SELECT count(*) FILTER (WHERE priority = 0) AS live, count(*) AS total FROM scoring_queue")
            row = cur.fetchone()
        value = (int(row["live"]), int(row["total"]))
        with self._lock:
            self._value, self._stamp = value, time.monotonic()
        return value

    def reset(self) -> None:
        with self._lock:
            self._stamp = 0.0


gauge = QueueGauge()


def guard(conn: Any, response: Response, api_key: dict[str, Any], *, cost: int, replay: bool) -> None:
    """Admit one ingestion request or refuse it cleanly, before anything is written."""
    s = settings()
    rate, burst = (s.replay_rate, s.replay_burst) if replay else (s.ingest_rate, s.ingest_burst)
    allowed, wait, left = buckets.take((api_key["id"], replay), cost, rate=rate, burst=burst)
    limit_headers = {
        "X-RateLimit-Limit": f"{rate:g}",
        "X-RateLimit-Burst": f"{burst:g}",
        "X-RateLimit-Remaining": str(int(left)),
    }
    if not allowed:
        kind = "replayed transactions" if replay else "transactions"
        raise HTTPException(
            status.HTTP_429_TOO_MANY_REQUESTS,
            detail=f"over {rate:g} {kind} a second for this API key; retry after {wait:.1f}s",
            headers={**limit_headers, "Retry-After": str(max(1, int(wait + 0.999)))},
        )

    live, total = gauge.read(conn)
    queue_headers = {"X-RiskRadar-Queue-Depth": str(live), "X-RiskRadar-Queue-Total": str(total)}
    if replay and total >= s.replay_queue_limit:
        raise HTTPException(
            status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=f"{total} transactions waiting to be scored; replayed history is paused until the backlog drains",
            headers={**limit_headers, **queue_headers, "Retry-After": "10"},
        )
    if not replay and live >= s.queue_hard_limit:
        raise HTTPException(
            status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=f"{live} live payments waiting to be scored; the scoring workers are behind. Retry shortly.",
            headers={**limit_headers, **queue_headers, "Retry-After": "5"},
        )
    for k, v in {**limit_headers, **queue_headers}.items():
        response.headers[k] = v
    busy = total >= s.replay_queue_limit // 2 if replay else live >= s.queue_soft_limit
    if busy:
        response.headers["X-RiskRadar-Backpressure"] = "slow-down"
