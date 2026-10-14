"""A well-behaved API client for the simulator and the dataset replay (D87).

Plain English
-------------
The simulator used to post as fast as its loop ran and ignore every answer.
Pointed at a busy service it would have made the backlog worse and silently
lost whatever was refused. This client behaves the way a bank's switch
integration should:

* **Paced.** Never more than ``rate`` transactions a second, spread evenly.
* **Listens.** Every response says how deep the scoring queue is. When the
  service answers ``X-RiskRadar-Backpressure: slow-down`` the client halves
  its speed, and recovers gradually once the pressure is gone.
* **Retries safely.** ``429`` and ``503`` are waited out for as long as
  ``Retry-After`` says; timeouts and dropped connections back off
  exponentially with jitter. Retrying is safe because every transaction
  carries its own reference and the API treats a repeat as a no-op (FR-002).
* **Reports honestly.** Anything refused for good (a ``422``) is counted and
  shown, never dropped quietly.
"""

from __future__ import annotations

import random
import time
from dataclasses import dataclass, field
from typing import Any

import httpx

RETRYABLE = {429, 502, 503, 504}


@dataclass
class Stats:
    sent: int = 0
    accepted: int = 0
    duplicates: int = 0
    rejected: int = 0
    retries: int = 0
    throttled: int = 0
    slowdowns: int = 0
    last_queue_depth: int | None = None
    errors: list[str] = field(default_factory=list)

    def line(self) -> str:
        q = "" if self.last_queue_depth is None else f", queue {self.last_queue_depth}"
        return (f"sent {self.sent}, accepted {self.accepted}, duplicate {self.duplicates}, "
                f"rejected {self.rejected}, retried {self.retries}, throttled {self.throttled}{q}")


class PacedClient:
    def __init__(self, base_url: str, api_key: str, *, rate: float, max_retries: int = 8,
                 timeout: float = 30.0, max_slowdown: float = 16.0,
                 transport: httpx.BaseTransport | None = None) -> None:
        self.rate = max(rate, 0.01)
        self.max_retries = max_retries
        self.max_slowdown = max_slowdown
        self.slowdown = 1.0
        self.stats = Stats()
        self._next = time.monotonic()
        self._http = httpx.Client(base_url=base_url, headers={"X-API-Key": api_key}, timeout=timeout,
                                  transport=transport)

    # -- context -----------------------------------------------------------------
    def __enter__(self) -> PacedClient:
        return self

    def __exit__(self, *exc: Any) -> None:
        self._http.close()

    def close(self) -> None:
        self._http.close()

    # -- pacing ------------------------------------------------------------------
    def _wait_turn(self, cost: int) -> None:
        now = time.monotonic()
        if self._next > now:
            time.sleep(self._next - now)
        self._next = max(self._next, now) + cost * self.slowdown / self.rate

    def _listen(self, response: httpx.Response) -> None:
        depth = response.headers.get("X-RiskRadar-Queue-Depth")
        if depth is not None:
            self.stats.last_queue_depth = int(depth)
        if response.headers.get("X-RiskRadar-Backpressure") == "slow-down":
            if self.slowdown < self.max_slowdown:
                self.slowdown = min(self.max_slowdown, self.slowdown * 2)
                self.stats.slowdowns += 1
        elif self.slowdown > 1.0:
            self.slowdown = max(1.0, self.slowdown * 0.9)

    # -- sending -----------------------------------------------------------------
    def post(self, path: str, json: dict[str, Any], *, cost: int = 1) -> httpx.Response | None:
        """Post once, paced, retried until it is answered for good. None if it never was."""
        self._wait_turn(cost)
        delay = 0.5
        for attempt in range(self.max_retries + 1):
            try:
                response = self._http.post(path, json=json)
            except (httpx.TimeoutException, httpx.TransportError) as exc:
                if attempt == self.max_retries:
                    self.stats.errors.append(f"{path}: {exc!r}")
                    return None
                self.stats.retries += 1
                time.sleep(delay + random.uniform(0, delay))
                delay = min(delay * 2, 30.0)
                continue
            self._listen(response)
            if response.status_code in RETRYABLE and attempt < self.max_retries:
                self.stats.retries += 1
                if response.status_code == 429:
                    self.stats.throttled += 1
                wait = float(response.headers.get("Retry-After") or delay)
                # A refusal means the service is loaded: slow down as well as wait.
                self.slowdown = min(self.max_slowdown, self.slowdown * 1.5)
                time.sleep(wait + random.uniform(0, 0.5))
                delay = min(delay * 2, 30.0)
                continue
            self._count(response, cost)
            return response
        return None

    def _count(self, response: httpx.Response, cost: int) -> None:
        self.stats.sent += cost
        if response.status_code >= 400:
            self.stats.rejected += cost
            if len(self.stats.errors) < 50:
                self.stats.errors.append(f"{response.status_code}: {response.text[:200]}")
            return
        body = response.json()
        if "accepted" in body:  # a batch
            self.stats.accepted += int(body.get("accepted", 0))
            self.stats.duplicates += int(body.get("duplicates", 0))
            self.stats.rejected += int(body.get("rejected", 0))
        elif body.get("status") == "duplicate":
            self.stats.duplicates += 1
        else:
            self.stats.accepted += 1
