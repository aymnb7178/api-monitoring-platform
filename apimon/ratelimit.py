"""In-memory token-bucket rate limiting (single process; state is lost on restart)."""
from __future__ import annotations

import threading
import time


class TokenBucket:
    def __init__(self, capacity: float, refill_per_s: float, clock=time.monotonic):
        if capacity <= 0 or refill_per_s <= 0:
            raise ValueError("capacity and refill_per_s must be positive")
        self.capacity = float(capacity)
        self.refill = float(refill_per_s)
        self.clock = clock
        self.tokens = float(capacity)
        self.updated = clock()
        self._lock = threading.Lock()

    def allow(self, cost: float = 1.0) -> tuple[bool, float]:
        """Return (allowed, retry_after_seconds)."""
        with self._lock:
            now = self.clock()
            self.tokens = min(self.capacity, self.tokens + (now - self.updated) * self.refill)
            self.updated = now
            if self.tokens >= cost:
                self.tokens -= cost
                return True, 0.0
            return False, (cost - self.tokens) / self.refill


class RateLimiter:
    """One bucket per key (e.g. per API key)."""

    def __init__(self, capacity: float, refill_per_s: float, clock=time.monotonic):
        self.capacity, self.refill, self.clock = capacity, refill_per_s, clock
        self._buckets: dict[str, TokenBucket] = {}
        self._lock = threading.Lock()

    def allow(self, key: str, cost: float = 1.0) -> tuple[bool, float]:
        with self._lock:
            b = self._buckets.get(key)
            if b is None:
                b = self._buckets[key] = TokenBucket(self.capacity, self.refill, self.clock)
        return b.allow(cost)
