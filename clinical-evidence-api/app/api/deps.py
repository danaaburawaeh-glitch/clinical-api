"""Shared FastAPI dependencies: auth, rate limiting, HTTP client, cache."""

from __future__ import annotations

import logging
from typing import Annotated, Any

from fastapi import Depends, Request

from app.errors import InvalidApiKeyError, RateLimitedError
from app.security.allowlist import SourceRegistry, get_source_registry
from app.security.authentication import ApiKeyIdentity, AuthError, authenticate_request
from app.security.safe_http import SafeHttpClient
from app.services.cache import CacheBackend
from app.settings import Settings, get_settings

logger = logging.getLogger(__name__)

__all__ = [
    "CurrentIdentity",
    "HttpClient",
    "Cache",
    "Registry",
    "AppSettings",
    "RequestId",
    "get_http_client",
    "get_cache",
    "require_api_key",
    "enforce_rate_limit",
]


def get_http_client(request: Request) -> SafeHttpClient:
    """The shared, hardened HTTP client created at application startup."""
    client = getattr(request.app.state, "http_client", None)
    if client is None:  # pragma: no cover - startup always sets this
        raise RuntimeError("HTTP client is not initialised")
    return client


def get_cache(request: Request) -> CacheBackend:
    cache = getattr(request.app.state, "cache", None)
    if cache is None:  # pragma: no cover
        raise RuntimeError("Cache is not initialised")
    return cache


def get_registry() -> SourceRegistry:
    return get_source_registry()


def get_request_id(request: Request) -> str:
    return getattr(request.state, "request_id", "unknown")


async def require_api_key(request: Request) -> ApiKeyIdentity:
    """Authenticate the caller (PART 6)."""
    try:
        identity = authenticate_request(request)
    except AuthError as exc:
        raise InvalidApiKeyError(exc.message) from exc
    request.state.key_name = identity.key_name
    request.state.key_fingerprint = identity.fingerprint
    return identity


async def enforce_rate_limit(
    request: Request,
    identity: Annotated[ApiKeyIdentity, Depends(require_api_key)],
) -> ApiKeyIdentity:
    """Per-API-key sliding-window rate limit (PART 35)."""
    settings = get_settings()
    if not settings.rate_limit_enabled:
        return identity

    limiter = getattr(request.app.state, "rate_limiter", None)
    if limiter is None:  # pragma: no cover
        return identity

    allowed, remaining, retry_after = await limiter.check(identity.rate_limit_key)
    request.state.rate_limit_remaining = remaining
    if not allowed:
        logger.warning(
            "rate_limit_exceeded",
            extra={"key_name": identity.key_name, "retry_after_s": round(retry_after, 1)},
        )
        raise RateLimitedError(
            f"Rate limit of {settings.rate_limit_per_minute} requests/minute exceeded. "
            f"Retry in {retry_after:.0f}s."
        )
    return identity


# Annotated aliases keep route signatures readable.
CurrentIdentity = Annotated[ApiKeyIdentity, Depends(enforce_rate_limit)]
HttpClient = Annotated[SafeHttpClient, Depends(get_http_client)]
Cache = Annotated[Any, Depends(get_cache)]
Registry = Annotated[SourceRegistry, Depends(get_registry)]
AppSettings = Annotated[Settings, Depends(get_settings)]
RequestId = Annotated[str, Depends(get_request_id)]
