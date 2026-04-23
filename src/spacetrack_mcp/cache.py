"""Caching helpers with Redis support and in-memory TTL fallback.

Redis is optional. If REDIS_URL is not set or Redis is unreachable, the
server falls back to a thread-safe in-memory cache with TTL expiration.
This ensures the server works without any external dependencies.
"""

import json
import logging
import os
import threading
import time
from typing import Any, Optional

logger = logging.getLogger(__name__)

# TTL constants (in seconds)
SATCAT_TTL = 86400        # 24 hours  — SATCAT updates daily after 1700 UTC
TLE_CURRENT_TTL = 3600    # 1 hour    — GP updates roughly hourly
TLE_HISTORY_TTL = 21600   # 6 hours   — GP_History: treat as infrequent bulk pull
PROPAGATION_TTL = 300     # 5 minutes — computed locally, cheap to rerun
CONJUNCTION_TTL = 3600    # 1 hour    — CDM_PUBLIC updates ~3×/day
DECAY_TTL = 21600         # 6 hours   — DECAY predictions update daily
BOXSCORE_TTL = 86400      # 24 hours  — BOXSCORE updates daily
LAUNCH_SITE_TTL = 604800  # 7 days    — launch sites rarely change
TIP_TTL = 3600            # 1 hour    — TIP messages update frequently near reentry
ANALYST_TTL = 3600        # 1 hour    — analyst_satellite updates frequently
SENSOR_TTL = 86400        # 24 hours  — sensor catalog rarely changes
MANEUVER_TTL = 3600       # 1 hour    — maneuver data is time-sensitive


class _InMemoryCache:
    """Simple thread-safe in-memory cache with TTL expiration.

    Used as a fallback when Redis is not available. Stores values in a dict
    alongside their expiration timestamps. Expired entries are evicted lazily
    on access and periodically during set operations.
    """

    def __init__(self) -> None:
        self._store: dict[str, tuple[Any, float]] = {}  # key -> (value, expires_at)
        self._lock = threading.Lock()

    def get(self, key: str) -> Optional[Any]:
        """Return cached value or None if missing or expired."""
        with self._lock:
            entry = self._store.get(key)
            if entry is None:
                return None
            value, expires_at = entry
            if time.time() > expires_at:
                del self._store[key]
                return None
            return value

    def set(self, key: str, value: Any, ttl: int) -> None:
        """Store value with the given TTL in seconds."""
        expires_at = time.time() + ttl
        with self._lock:
            self._store[key] = (value, expires_at)
            # Opportunistically evict a batch of expired keys to bound memory usage
            self._evict_expired()

    def _evict_expired(self) -> None:
        """Remove expired entries. Must be called with self._lock held."""
        now = time.time()
        expired = [k for k, (_, exp) in self._store.items() if now > exp]
        for k in expired:
            del self._store[k]

    def ping(self) -> bool:
        return True


class _RedisCacheClient:
    """Wraps Redis with JSON serialization and TTL management."""

    def __init__(self, redis_client: Any) -> None:
        self._client = redis_client

    def get(self, key: str) -> Optional[Any]:
        """Return cached value or None if missing / expired."""
        try:
            raw = self._client.get(key)
            if raw is None:
                return None
            return json.loads(raw)
        except Exception as exc:
            logger.warning("Cache GET error for key %s: %s", key, exc)
            return None

    def set(self, key: str, value: Any, ttl: int) -> None:
        """Persist value as JSON with the given TTL in seconds."""
        try:
            self._client.setex(key, ttl, json.dumps(value))
        except Exception as exc:
            logger.warning("Cache SET error for key %s: %s", key, exc)

    def ping(self) -> bool:
        """Return True if Redis is reachable."""
        try:
            return self._client.ping()
        except Exception:
            return False


# CacheClient is the public type alias used by server.py
CacheClient = _RedisCacheClient | _InMemoryCache

# Module-level singleton — initialised lazily so tests can mock easily.
_cache: Optional[CacheClient] = None


def get_cache() -> CacheClient:
    """Return the cache singleton, initialising it on first call.

    Attempts to connect to Redis if REDIS_URL is set. Falls back to the
    in-memory cache if Redis is not configured or is unreachable.
    """
    global _cache
    if _cache is not None:
        return _cache

    redis_url = os.getenv("REDIS_URL", "")

    if redis_url:
        try:
            import redis  # optional dependency

            client = redis.from_url(redis_url, decode_responses=True)
            # Verify connectivity before committing to Redis
            client.ping()
            _cache = _RedisCacheClient(client)
            logger.info("Cache: connected to Redis at %s", redis_url)
        except ImportError:
            logger.warning(
                "Cache: redis package not installed (install spacetrack-mcp[redis]). "
                "Falling back to in-memory cache."
            )
            _cache = _InMemoryCache()
        except Exception as exc:
            logger.warning(
                "Cache: Redis unreachable (%s). Falling back to in-memory cache.", exc
            )
            _cache = _InMemoryCache()
    else:
        logger.info("Cache: REDIS_URL not set — using in-memory cache.")
        _cache = _InMemoryCache()

    return _cache
