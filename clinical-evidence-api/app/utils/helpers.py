"""Small shared utilities: hashing, timing, async rate limiting, redaction."""

from __future__ import annotations

import asyncio
import hashlib
import json
import re
import time
import uuid
from collections import deque
from datetime import datetime, timezone
from typing import Any, Iterable

__all__ = [
    "utc_now",
    "utc_now_iso",
    "new_request_id",
    "stable_hash",
    "chunked",
    "AsyncRateLimiter",
    "SlidingWindowLimiter",
    "redact_secrets",
    "scrub_pii",
    "Stopwatch",
]

# Patterns that must never reach a log line (PART 36, PART 37).
_SECRET_PATTERNS = [
    re.compile(r"(?i)(x-clinical-key\s*[:=]\s*)([^\s,;\"']+)"),
    # Consume the whole credential, including an auth scheme prefix such
    # as "Bearer " — matching only the first token would leave the secret
    # itself in the log line.
    re.compile(r"(?i)(authorization\s*[:=]\s*)(?:(?:bearer|basic|token)\s+)?([^\s,;\"']+)"),
    re.compile(r"(?i)(\bbearer\s+)([A-Za-z0-9._\-]{6,})"),
    re.compile(r"(?i)([?&](?:api_?key|key|token|secret)=)([^&\s]+)"),
    re.compile(r"(?i)(\"?(?:api_?key|apikey|token|secret|password)\"?\s*[:=]\s*\"?)([^\s,\"'}]+)"),
]

# Direct patient identifiers that should not be persisted in logs even if
# a caller pastes them into a query (PART 37).
_PII_PATTERNS = [
    (re.compile(r"\b\d{10,16}\b"), "[ID-REDACTED]"),          # national ID / MRN-like
    (re.compile(r"\b[\w.+-]+@[\w-]+\.[\w.]+\b"), "[EMAIL-REDACTED]"),
    (re.compile(r"(?<!\d)(?:\+?\d{1,3}[\s-]?)?\(?\d{3}\)?[\s-]?\d{3}[\s-]?\d{4}(?!\d)"),
     "[PHONE-REDACTED]"),
    (re.compile(r"\b\d{4}-\d{2}-\d{2}\b"), "[DATE-REDACTED]"),
]


def utc_now() -> datetime:
    """Timezone-aware current UTC time."""
    return datetime.now(timezone.utc)


def utc_now_iso() -> str:
    """ISO-8601 UTC timestamp with second precision."""
    return utc_now().replace(microsecond=0).isoformat()


def new_request_id() -> str:
    """Short, collision-resistant request identifier for structured logs."""
    return uuid.uuid4().hex[:16]


def stable_hash(*parts: Any) -> str:
    """Deterministic SHA-256 hash over arbitrary JSON-serialisable parts.

    Used for cache keys. Dict ordering is normalised so that logically
    identical requests collapse onto the same key.
    """
    payload = json.dumps(parts, sort_keys=True, default=str, ensure_ascii=False)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def chunked(items: Iterable[Any], size: int) -> Iterable[list[Any]]:
    """Yield successive lists of at most ``size`` items."""
    if size <= 0:
        raise ValueError("size must be positive")
    batch: list[Any] = []
    for item in items:
        batch.append(item)
        if len(batch) >= size:
            yield batch
            batch = []
    if batch:
        yield batch


def redact_secrets(text: str | None) -> str:
    """Replace anything that looks like a credential with ``***``."""
    if not text:
        return ""
    result = text
    for pattern in _SECRET_PATTERNS:
        result = pattern.sub(lambda m: f"{m.group(1)}***", result)
    return result


def scrub_pii(text: str | None, max_chars: int = 200) -> str:
    """Redact obvious direct identifiers and truncate.

    This is defence in depth, not a compliance guarantee. The service is
    designed to receive clinical *questions*, not patient records; see
    PRIVACY_POLICY.md.
    """
    if not text:
        return ""
    result = redact_secrets(text)
    for pattern, replacement in _PII_PATTERNS:
        result = pattern.sub(replacement, result)
    if len(result) > max_chars:
        result = result[:max_chars] + "…"
    return result


class AsyncRateLimiter:
    """Simple async token-bucket limiter for a single upstream provider.

    Guarantees a minimum interval between requests so we stay inside
    NCBI / Europe PMC / Crossref / openFDA politeness limits even when
    the orchestrator fans out concurrently.
    """

    def __init__(self, rate_per_second: float) -> None:
        if rate_per_second <= 0:
            raise ValueError("rate_per_second must be positive")
        self._min_interval = 1.0 / rate_per_second
        self._lock = asyncio.Lock()
        self._next_available = 0.0

    async def acquire(self) -> None:
        async with self._lock:
            now = time.monotonic()
            wait_for = self._next_available - now
            if wait_for > 0:
                await asyncio.sleep(wait_for)
                now = time.monotonic()
            self._next_available = max(now, self._next_available) + self._min_interval


class SlidingWindowLimiter:
    """Per-key sliding-window limiter used for inbound API rate limiting.

    In-process only. For multi-replica deployments this must be replaced
    by a shared store (see README "Known limitations").
    """

    def __init__(self, limit: int, window_seconds: float = 60.0) -> None:
        self._limit = max(1, limit)
        self._window = window_seconds
        self._hits: dict[str, deque[float]] = {}
        self._lock = asyncio.Lock()

    async def check(self, key: str) -> tuple[bool, int, float]:
        """Return ``(allowed, remaining, retry_after_seconds)``."""
        async with self._lock:
            now = time.monotonic()
            bucket = self._hits.setdefault(key, deque())
            cutoff = now - self._window
            while bucket and bucket[0] < cutoff:
                bucket.popleft()

            if len(bucket) >= self._limit:
                retry_after = max(0.0, bucket[0] + self._window - now)
                return False, 0, retry_after

            bucket.append(now)
            # Opportunistic cleanup so idle keys do not accumulate.
            if len(self._hits) > 4096:
                for stale_key in [k for k, v in self._hits.items() if not v]:
                    self._hits.pop(stale_key, None)
            return True, self._limit - len(bucket), 0.0


class Stopwatch:
    """Context manager measuring elapsed milliseconds."""

    def __init__(self) -> None:
        self._start = 0.0
        self.elapsed_ms = 0.0

    def __enter__(self) -> "Stopwatch":
        self._start = time.perf_counter()
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.elapsed_ms = round((time.perf_counter() - self._start) * 1000, 2)
