"""Token-bucket pacing for providers with a tokens-per-minute ceiling.

Only the Groq analyzer needs this today, but it's provider-shaped, not
Groq-shaped -- any TPM-limited provider could reuse it.
"""
from __future__ import annotations

import threading
import time


class TokenRateLimiter:
    """Token bucket pacing requests under a tokens-per-minute ceiling.

    Groq enforces TPM (prompt + completion) across the whole org, so firing N
    threads at it just produces 429 storms and wasted backoff. We reserve an
    estimate before each call and reconcile against reported usage afterwards,
    which keeps throughput pinned just under the ceiling.
    """

    def __init__(self, tokens_per_minute: int, safety: float = 0.92):
        self.capacity = tokens_per_minute * safety
        self.rate = self.capacity / 60.0  # tokens per second
        self._tokens = self.capacity
        self._updated = time.monotonic()
        self._cv = threading.Condition(threading.Lock())

    def _refill(self) -> None:
        now = time.monotonic()
        self._tokens = min(self.capacity, self._tokens + (now - self._updated) * self.rate)
        self._updated = now

    def acquire(self, amount: int) -> None:
        amount = min(amount, int(self.capacity))
        with self._cv:
            while True:
                self._refill()
                if self._tokens >= amount:
                    self._tokens -= amount
                    return
                self._cv.wait(timeout=max((amount - self._tokens) / self.rate, 0.05))

    def settle(self, estimated: int, actual: int) -> None:
        """Correct the bucket once real usage is known."""
        with self._cv:
            self._tokens = max(-self.capacity, self._tokens - (actual - estimated))
            self._cv.notify_all()

    def refund(self, amount: int) -> None:
        """Return a reservation for a request the server never charged us for.

        A 429 (or a connection failure) means the call was rejected before any
        tokens were generated. Keeping the deduction would make the bucket drift
        permanently below the real quota -- every rejection would shrink our
        effective rate, which compounds into a throughput collapse.
        """
        with self._cv:
            self._tokens = min(self.capacity, self._tokens + amount)
            self._cv.notify_all()
