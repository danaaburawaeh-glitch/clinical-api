"""Response cache (PART 34).

SQLite in v1, with a storage-agnostic :class:`CacheBackend` interface so
PostgreSQL/Redis is a drop-in replacement rather than a rewrite.

Design points:
  * TTLs differ by endpoint and come from ``evidence_rules.yaml``, which
    can be overridden per deployment. Regulatory data gets a short TTL
    because it changes (PART 61); Crossref metadata gets seven days.
  * Keys are SHA-256 hashes of ``(endpoint, normalised parameters)``.
    The raw query text is stored only in scrubbed form, and never any
    credential.
  * All SQLite access happens on a worker thread via ``asyncio.to_thread``
    so a slow disk cannot block the event loop.
"""

from __future__ import annotations

import asyncio
import json
import logging
import sqlite3
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol
from urllib.parse import urlparse

from app.settings import get_settings
from app.utils.helpers import stable_hash

logger = logging.getLogger(__name__)

__all__ = ["CacheBackend", "SqliteCache", "NullCache", "CacheEntry", "build_cache", "cache_key"]

_SCHEMA = """
CREATE TABLE IF NOT EXISTS cache_entries (
    key         TEXT PRIMARY KEY,
    endpoint    TEXT NOT NULL,
    payload     TEXT NOT NULL,
    created_at  REAL NOT NULL,
    expires_at  REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_cache_expires ON cache_entries(expires_at);
CREATE INDEX IF NOT EXISTS idx_cache_endpoint ON cache_entries(endpoint);
"""


@dataclass
class CacheEntry:
    key: str
    endpoint: str
    payload: Any
    created_at: float
    expires_at: float

    @property
    def age_seconds(self) -> float:
        return max(0.0, time.time() - self.created_at)


class CacheBackend(Protocol):
    """Interface any cache implementation must satisfy."""

    async def get(self, key: str) -> CacheEntry | None: ...
    async def set(self, key: str, endpoint: str, payload: Any, ttl_seconds: int) -> None: ...
    async def purge_expired(self) -> int: ...
    async def close(self) -> None: ...


def cache_key(endpoint: str, params: dict[str, Any]) -> str:
    """Deterministic cache key for an endpoint + parameter set."""
    normalised = {
        k: v for k, v in sorted(params.items())
        if v is not None and v != "" and v != []
    }
    return stable_hash(endpoint, normalised)


class NullCache:
    """No-op cache, used when caching is disabled."""

    async def get(self, key: str) -> CacheEntry | None:
        return None

    async def set(self, key: str, endpoint: str, payload: Any, ttl_seconds: int) -> None:
        return None

    async def purge_expired(self) -> int:
        return 0

    async def close(self) -> None:
        return None


class SqliteCache:
    """SQLite-backed cache. Safe for single-process async use."""

    def __init__(self, database_path: Path, max_rows: int = 20000) -> None:
        self._path = database_path
        self._max_rows = max_rows
        self._lock = asyncio.Lock()
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._init_schema()

    # ------------------------------------------------------------------
    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self._path, timeout=10.0)
        conn.row_factory = sqlite3.Row
        # WAL keeps readers from blocking on the writer.
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=NORMAL")
        return conn

    def _init_schema(self) -> None:
        with self._connect() as conn:
            conn.executescript(_SCHEMA)

    # ------------------------------------------------------------------
    async def get(self, key: str) -> CacheEntry | None:
        try:
            row = await asyncio.to_thread(self._get_sync, key)
        except sqlite3.Error:
            logger.exception("cache_read_failed")
            return None
        if row is None:
            return None
        try:
            payload = json.loads(row["payload"])
        except (json.JSONDecodeError, TypeError):
            logger.warning("cache_payload_corrupt", extra={"cache_key": key[:12]})
            return None
        return CacheEntry(
            key=row["key"],
            endpoint=row["endpoint"],
            payload=payload,
            created_at=row["created_at"],
            expires_at=row["expires_at"],
        )

    def _get_sync(self, key: str) -> sqlite3.Row | None:
        with self._connect() as conn:
            cursor = conn.execute(
                "SELECT * FROM cache_entries WHERE key = ? AND expires_at > ?",
                (key, time.time()),
            )
            return cursor.fetchone()

    async def set(self, key: str, endpoint: str, payload: Any, ttl_seconds: int) -> None:
        if ttl_seconds <= 0:
            return
        try:
            serialised = json.dumps(payload, ensure_ascii=False, default=str)
        except (TypeError, ValueError):
            logger.warning("cache_payload_not_serialisable", extra={"endpoint": endpoint})
            return
        async with self._lock:
            try:
                await asyncio.to_thread(
                    self._set_sync, key, endpoint, serialised, ttl_seconds
                )
            except sqlite3.Error:
                logger.exception("cache_write_failed")

    def _set_sync(self, key: str, endpoint: str, payload: str, ttl: int) -> None:
        now = time.time()
        with self._connect() as conn:
            conn.execute(
                "INSERT OR REPLACE INTO cache_entries "
                "(key, endpoint, payload, created_at, expires_at) VALUES (?, ?, ?, ?, ?)",
                (key, endpoint, payload, now, now + ttl),
            )
            conn.execute("DELETE FROM cache_entries WHERE expires_at <= ?", (now,))
            # Bound growth: drop the oldest rows beyond the cap.
            conn.execute(
                "DELETE FROM cache_entries WHERE key IN ("
                "  SELECT key FROM cache_entries ORDER BY created_at DESC LIMIT -1 OFFSET ?"
                ")",
                (self._max_rows,),
            )

    async def purge_expired(self) -> int:
        try:
            return await asyncio.to_thread(self._purge_sync)
        except sqlite3.Error:
            logger.exception("cache_purge_failed")
            return 0

    def _purge_sync(self) -> int:
        with self._connect() as conn:
            cursor = conn.execute(
                "DELETE FROM cache_entries WHERE expires_at <= ?", (time.time(),)
            )
            return cursor.rowcount or 0

    async def close(self) -> None:
        return None


def build_cache() -> CacheBackend:
    """Construct the configured cache backend."""
    settings = get_settings()
    if not settings.cache_enabled:
        logger.info("cache_disabled")
        return NullCache()

    url = settings.cache_database_url
    if url.startswith("sqlite"):
        path_part = url.split("///", 1)[-1] if "///" in url else "./data/cache.db"
        try:
            return SqliteCache(Path(path_part), settings.cache_max_rows)
        except (OSError, sqlite3.Error):
            logger.exception("cache_init_failed_falling_back_to_null")
            return NullCache()

    if urlparse(url).scheme in {"postgres", "postgresql"}:
        # Deliberate: v1 ships SQLite only. Failing loudly here is better
        # than silently running uncached in production.
        logger.error("cache_backend_not_implemented", extra={"scheme": urlparse(url).scheme})
        raise NotImplementedError(
            "PostgreSQL cache backend is not implemented in v1. "
            "Set CACHE_DATABASE_URL to a sqlite:/// URL or CACHE_ENABLED=false."
        )

    logger.warning("cache_backend_unknown_using_null", extra={"url_scheme": urlparse(url).scheme})
    return NullCache()
