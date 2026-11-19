"""Token bucket per (partner, use case), shared across pods through Redis.

With a Redis URL the bucket lives in Redis and every pod and worker draws from
the same one: one Lua script (EVALSHA, reloaded on NOSCRIPT) refills and takes a
token atomically, using the Redis server clock; the key expires once an idle
bucket would be full again, so idle buckets cost nothing.

Without Redis, or while Redis is unreachable, each worker process keeps its own
in-memory bucket (the effective ceiling is then up to pods × workers × the
configured rate). This is deliberate: availability over exactness. A Redis
outage never fails a partner call; it only loosens the limit to per-process
until Redis answers again (retried every ``redis_retry_seconds``).
"""

import logging
import math
import threading
import time
from typing import Callable, Dict, Optional, Tuple

_logger = logging.getLogger(__name__)

# KEYS[1] bucket; ARGV: capacity, refill per second, TTL seconds, now ("" → Redis TIME).
_TOKEN_BUCKET_LUA = """
local capacity = tonumber(ARGV[1])
local refill = tonumber(ARGV[2])
local now = tonumber(ARGV[4])
if not now then
  local t = redis.call('TIME')
  now = tonumber(t[1]) + tonumber(t[2]) / 1000000
end
local b = redis.call('HMGET', KEYS[1], 'tokens', 'ts')
local tokens = tonumber(b[1])
local ts = tonumber(b[2])
if not tokens or not ts then
  tokens = capacity
  ts = now
end
local elapsed = now - ts
if elapsed < 0 then elapsed = 0 end
tokens = math.min(capacity, tokens + elapsed * refill)
local allowed = 0
if tokens >= 1 then
  tokens = tokens - 1
  allowed = 1
end
redis.call('HSET', KEYS[1], 'tokens', tostring(tokens), 'ts', tostring(now))
redis.call('EXPIRE', KEYS[1], tonumber(ARGV[3]))
return allowed
"""


class RateLimiter:
    def __init__(
        self,
        clock=time.monotonic,
        max_keys: int = 10000,
        redis_url: str = "",
        redis_password: str = "",
        key_prefix: str = "agri-composite:ratelimit:",
        redis_timeout_seconds: float = 0.25,
        redis_retry_seconds: float = 10.0,
        redis_client=None,
        redis_clock: Optional[Callable[[], float]] = None,
    ):
        self._clock = clock
        self._buckets: Dict[Tuple[str, str], Tuple[float, float]] = {}  # key -> (tokens, last)
        self._lock = threading.Lock()
        self._max_keys = max_keys
        self._redis_url = redis_url
        self._redis_password = redis_password
        self._prefix = key_prefix
        self._timeout = redis_timeout_seconds
        self._retry_seconds = redis_retry_seconds
        self._redis = redis_client  # tests inject one; otherwise created lazily on the worker's loop
        self._redis_clock = redis_clock  # tests only; None → the Redis server clock
        self._script = None
        self._down_until = 0.0  # after a Redis failure, skip Redis until then (monotonic)

    @property
    def shared(self) -> bool:
        return bool(self._redis_url or self._redis is not None)

    async def acquire(self, partner_id: str, use_case: str, requests: int, period_seconds: int) -> bool:
        """Take one token; False when the bucket is empty."""
        if not self.shared:
            return self.allow(partner_id, use_case, requests, period_seconds)
        if time.monotonic() >= self._down_until:
            try:
                return await self._allow_redis(partner_id, use_case, requests, period_seconds)
            except Exception as e:
                self._down_until = time.monotonic() + self._retry_seconds
                _logger.warning(
                    "Rate limit Redis unreachable (%s: %s); counting per worker process for %ss",
                    type(e).__name__, e, self._retry_seconds,
                )
        # Availability over exactness: a per-process bucket until Redis answers again.
        return self.allow(partner_id, use_case, requests, period_seconds)

    def allow(self, partner_id: str, use_case: str, requests: int, period_seconds: int) -> bool:
        """The in-memory bucket of this worker process."""
        capacity = float(requests)
        refill = capacity / float(period_seconds)
        key = (partner_id, use_case)
        now = self._clock()
        with self._lock:
            tokens, last = self._buckets.get(key, (capacity, now))
            tokens = min(capacity, tokens + (now - last) * refill)
            allowed = tokens >= 1.0
            if allowed:
                tokens -= 1.0
            if len(self._buckets) >= self._max_keys and key not in self._buckets:
                self._buckets.clear()  # bounded memory; a reset only ever allows more
            self._buckets[key] = (tokens, now)
            return allowed

    async def _allow_redis(self, partner_id: str, use_case: str, requests: int, period_seconds: int) -> bool:
        if self._redis is None:
            import redis.asyncio as aioredis

            self._redis = aioredis.Redis.from_url(
                self._redis_url,
                password=self._redis_password or None,
                socket_timeout=self._timeout,
                socket_connect_timeout=self._timeout,
            )
        if self._script is None:
            self._script = self._redis.register_script(_TOKEN_BUCKET_LUA)  # EVALSHA, EVAL on NOSCRIPT
        capacity = float(requests)
        ttl = max(1, math.ceil(float(period_seconds)))  # an idle bucket is full again after one period
        now = "" if self._redis_clock is None else repr(self._redis_clock())
        allowed = await self._script(
            keys=[f"{self._prefix}{partner_id}:{use_case}"],
            args=[repr(capacity), repr(capacity / float(period_seconds)), ttl, now],
        )
        return int(allowed) == 1

    async def close(self):
        if self._redis is not None and self._redis_url:
            try:
                await self._redis.aclose()
            except Exception:
                pass
            self._redis = None
            self._script = None
