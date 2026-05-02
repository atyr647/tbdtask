"""Rate limiting backends.

Two implementations, same interface:

* ``InProcessLimiter`` — thread-safe fixed-window counter. Used for
  single-worker deployments and the AppImage. No external dependencies.
* ``RedisLimiter`` — Redis-backed fixed-window counter. Used for
  multi-worker hosted deployments where the in-process counter would
  be per-worker and therefore ineffective.

Both implement ``check(scope, key) -> bool`` (True = allowed, False =
blocked) and ``reset()`` (test-only). The global ``get_limiter()``
factory picks the right backend based on environment.
"""
from __future__ import annotations

import os
import time
from collections import defaultdict
from dataclasses import dataclass, field
from threading import Lock
from typing import Optional


@dataclass
class _Bucket:
    window_start: float = 0.0
    count: int = 0


@dataclass
class InProcessLimiter:
    """Thread-safe fixed-window counter keyed by ``(scope, key)``.

    Suitable for single-worker deployments and the AppImage. In
    multi-worker setups each worker tracks its own counters, so the
    effective limit is ``limit * num_workers``. Use ``RedisLimiter``
    for production multi-worker deployments.
    """

    limit: int
    window_seconds: int
    _buckets: dict = field(default_factory=lambda: defaultdict(_Bucket))
    _lock: Lock = field(default_factory=Lock)

    def check(self, scope: str, key: str) -> bool:
        now = time.monotonic()
        with self._lock:
            bucket = self._buckets[(scope, key)]
            if now - bucket.window_start >= self.window_seconds:
                bucket.window_start = now
                bucket.count = 0
            bucket.count += 1
            return bucket.count <= self.limit

    def reset(self) -> None:
        with self._lock:
            self._buckets.clear()


class RedisLimiter:
    """Redis-backed fixed-window counter.

    Uses a single Redis key per ``(scope, key)`` pair with a TTL equal
    to the window duration. Atomic via ``INCR`` and ``EXPIRE`` in a
    pipeline.

    Requires ``redis`` package. Install with ``pip install redis``.
    """

    def __init__(self, limit: int, window_seconds: int, *, url: Optional[str] = None):
        self.limit = limit
        self.window_seconds = window_seconds
        self._client = self._connect(url)

    @staticmethod
    def _connect(url: Optional[str]):
        try:
            import redis
        except ImportError:
            raise ImportError(
                "RedisLimiter requires the 'redis' package. "
                "Install with: pip install redis"
            )
        return redis.from_url(url or os.environ.get("REDIS_URL", "redis://localhost:6379/0"))

    def check(self, scope: str, key: str) -> bool:
        redis_key = f"tbdtask:ratelimit:{scope}:{key}"
        pipe = self._client.pipeline(True)
        pipe.incr(redis_key)
        pipe.expire(redis_key, self.window_seconds)
        results = pipe.execute()
        current = results[0]
        return current <= self.limit

    def reset(self) -> None:
        """Wipe all rate-limit keys. Test-only."""
        cursor = 0
        while True:
            cursor, keys = self._client.scan(cursor, match="tbdtask:ratelimit:*", count=100)
            if keys:
                self._client.delete(*keys)
            if cursor == 0:
                break


def get_limiter(
    *,
    limit: int = 10,
    window_seconds: int = 300,
    redis_url: Optional[str] = None,
) -> InProcessLimiter | RedisLimiter:
    """Return the appropriate rate limiter for the current deployment.

    If ``REDIS_URL`` is set (or ``redis_url`` is passed), returns a
    ``RedisLimiter``. Otherwise falls back to ``InProcessLimiter``.
    """
    url = redis_url or os.environ.get("REDIS_URL")
    if url:
        return RedisLimiter(limit=limit, window_seconds=window_seconds, url=url)
    return InProcessLimiter(limit=limit, window_seconds=window_seconds)
