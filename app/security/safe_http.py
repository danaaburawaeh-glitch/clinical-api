"""Hardened outbound HTTP client (PART 22, PART 23).

Every outbound request in this service goes through :class:`SafeHttpClient`.
It enforces, per request:

  * https-only, allowlisted destination, SSRF-validated addresses
  * **manual redirect following** — each hop is re-validated from scratch,
    so an approved domain cannot 302 you onto an unapproved one
  * a hard cap on redirect count
  * connect and read timeouts
  * a streamed response-size ceiling (both ``Content-Length`` and actual
    bytes read, because ``Content-Length`` is attacker-controlled)
  * a MIME allowlist per call site
  * a descriptive, contactable User-Agent
  * per-provider request pacing

Redirects are followed manually rather than with ``follow_redirects=True``
precisely because httpx would otherwise perform the hop before we get a
chance to inspect the ``Location`` header.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any, Mapping
from urllib.parse import urljoin

import httpx

from app.security.url_validator import UrlValidationError, ValidatedUrl, validate_url
from app.settings import get_settings
from app.utils.helpers import AsyncRateLimiter

logger = logging.getLogger(__name__)

__all__ = [
    "SafeHttpClient",
    "SafeResponse",
    "UpstreamError",
    "UpstreamTimeout",
    "UpstreamRateLimited",
    "ResponseTooLarge",
    "DisallowedContentType",
    "MIME_JSON",
    "MIME_XML",
    "MIME_HTML",
    "MIME_PDF",
]

MIME_JSON = frozenset({"application/json", "text/json", "application/problem+json"})
MIME_XML = frozenset({"application/xml", "text/xml", "application/rss+xml"})
MIME_HTML = frozenset({"text/html", "application/xhtml+xml"})
MIME_PDF = frozenset({"application/pdf"})
MIME_TEXT = frozenset({"text/plain"})


class UpstreamError(Exception):
    """Generic upstream failure."""

    code = "UPSTREAM_ERROR"
    retryable = True

    def __init__(self, message: str, *, status_code: int | None = None) -> None:
        super().__init__(message)
        self.message = message
        self.status_code = status_code


class UpstreamTimeout(UpstreamError):
    code = "UPSTREAM_TIMEOUT"
    retryable = True


class UpstreamRateLimited(UpstreamError):
    code = "UPSTREAM_RATE_LIMIT"
    retryable = True


class ResponseTooLarge(UpstreamError):
    code = "RESPONSE_TOO_LARGE"
    retryable = False


class DisallowedContentType(UpstreamError):
    code = "DISALLOWED_CONTENT_TYPE"
    retryable = False


@dataclass
class SafeResponse:
    """A validated upstream response."""

    url: str
    final_url: str
    status_code: int
    content_type: str
    content: bytes
    validated: ValidatedUrl
    redirect_chain: list[str] = field(default_factory=list)

    @property
    def text(self) -> str:
        return self.content.decode("utf-8", errors="replace")

    def json(self) -> Any:
        import json as _json

        return _json.loads(self.text)


class SafeHttpClient:
    """Async HTTP client with allowlist, SSRF and redirect enforcement."""

    def __init__(
        self,
        *,
        client: httpx.AsyncClient | None = None,
        rate_limiters: Mapping[str, AsyncRateLimiter] | None = None,
    ) -> None:
        settings = get_settings()
        self._settings = settings
        self._owns_client = client is None
        self._client = client or httpx.AsyncClient(
            follow_redirects=False,  # non-negotiable: we validate each hop
            timeout=httpx.Timeout(
                settings.http_timeout_seconds,
                connect=settings.http_connect_timeout_seconds,
            ),
            limits=httpx.Limits(
                max_connections=settings.http_max_connections,
                max_keepalive_connections=max(2, settings.http_max_connections // 2),
            ),
            headers={
                "User-Agent": settings.http_user_agent,
                "Accept-Encoding": "gzip, deflate",
            },
            trust_env=True,
        )
        self._rate_limiters: dict[str, AsyncRateLimiter] = dict(rate_limiters or {})

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------
    async def aclose(self) -> None:
        if self._owns_client:
            await self._client.aclose()

    async def __aenter__(self) -> "SafeHttpClient":
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self.aclose()

    def register_rate_limiter(self, provider: str, rate_per_second: float) -> None:
        self._rate_limiters[provider] = AsyncRateLimiter(rate_per_second)

    # ------------------------------------------------------------------
    # Core request
    # ------------------------------------------------------------------
    async def request(
        self,
        method: str,
        url: str,
        *,
        params: Mapping[str, Any] | None = None,
        data: Mapping[str, Any] | None = None,
        headers: Mapping[str, str] | None = None,
        accept_mime: frozenset[str] | set[str] = MIME_JSON,
        provider: str | None = None,
        required_category: str | None = None,
        allowed_categories: frozenset[str] | set[str] | None = None,
        max_bytes: int | None = None,
    ) -> SafeResponse:
        """Perform a validated request, following redirects manually."""
        settings = self._settings
        ceiling = max_bytes or settings.http_max_response_bytes

        if provider and provider in self._rate_limiters:
            await self._rate_limiters[provider].acquire()

        current_url = url
        redirect_chain: list[str] = []
        original_validated: ValidatedUrl | None = None

        for hop in range(settings.http_max_redirects + 1):
            validated = await validate_url(
                current_url,
                required_category=required_category,
                allowed_categories=allowed_categories,
            )
            if original_validated is None:
                original_validated = validated

            try:
                response = await self._send(
                    method if hop == 0 else "GET",
                    validated.url,
                    params=params if hop == 0 else None,
                    data=data if hop == 0 else None,
                    headers=headers,
                    ceiling=ceiling,
                )
            except httpx.TimeoutException as exc:
                raise UpstreamTimeout(f"request to {validated.host} timed out") from exc
            except httpx.TransportError as exc:
                raise UpstreamError(
                    f"transport error contacting {validated.host}: {exc.__class__.__name__}"
                ) from exc

            if response.status_code in (301, 302, 303, 307, 308):
                location = response.headers.get("location", "")
                if not location:
                    raise UpstreamError(
                        f"{response.status_code} redirect from {validated.host} "
                        "without a Location header"
                    )
                # Resolve relative redirects against the current URL, then
                # re-run the full validation pipeline on the result.
                next_url = urljoin(validated.url, location)
                redirect_chain.append(next_url)

                if hop >= settings.http_max_redirects:
                    raise UpstreamError(
                        f"redirect limit ({settings.http_max_redirects}) exceeded"
                    )

                try:
                    await validate_url(next_url)
                except UrlValidationError as exc:
                    logger.warning(
                        "redirect_blocked",
                        extra={
                            "from_host": validated.host,
                            "reason": exc.reason,
                            "hop": hop,
                        },
                    )
                    raise UrlValidationError(
                        f"redirect from {validated.host} to an unapproved destination "
                        f"was blocked: {exc.reason}",
                        next_url,
                    ) from exc

                current_url = next_url
                continue

            if response.status_code == 429:
                raise UpstreamRateLimited(
                    f"{validated.host} returned 429 (rate limited)", status_code=429
                )
            if response.status_code >= 500:
                raise UpstreamError(
                    f"{validated.host} returned {response.status_code}",
                    status_code=response.status_code,
                )
            if response.status_code >= 400:
                raise UpstreamError(
                    f"{validated.host} returned {response.status_code}",
                    status_code=response.status_code,
                )

            content_type = (
                response.headers.get("content-type", "").split(";")[0].strip().lower()
            )
            if accept_mime and content_type and content_type not in accept_mime:
                raise DisallowedContentType(
                    f"{validated.host} returned content-type {content_type!r}, "
                    f"expected one of {sorted(accept_mime)}"
                )

            return SafeResponse(
                url=url,
                final_url=validated.url,
                status_code=response.status_code,
                content_type=content_type,
                content=response.content,
                validated=validated,
                redirect_chain=redirect_chain,
            )

        raise UpstreamError("redirect handling terminated unexpectedly")

    async def _send(
        self,
        method: str,
        url: str,
        *,
        params: Mapping[str, Any] | None,
        data: Mapping[str, Any] | None,
        headers: Mapping[str, str] | None,
        ceiling: int,
    ) -> httpx.Response:
        """Send one hop and enforce the response-size ceiling while streaming."""
        request = self._client.build_request(
            method,
            url,
            params=params,
            data=data,
            headers=dict(headers or {}),
        )
        response = await self._client.send(request, stream=True)
        try:
            declared = response.headers.get("content-length")
            if declared and declared.isdigit() and int(declared) > ceiling:
                raise ResponseTooLarge(
                    f"declared Content-Length {declared} exceeds ceiling {ceiling}"
                )

            # Content-Length is advisory; count the bytes we actually read.
            chunks: list[bytes] = []
            total = 0
            async for chunk in response.aiter_bytes():
                total += len(chunk)
                if total > ceiling:
                    raise ResponseTooLarge(
                        f"response body exceeded ceiling of {ceiling} bytes"
                    )
                chunks.append(chunk)
            body = b"".join(chunks)
        finally:
            await response.aclose()

        # Rebuild a non-streaming response object carrying the read body.
        #
        # ``aiter_bytes()`` yields DECODED bytes: httpx has already undone
        # any Content-Encoding. Carrying the original Content-Encoding over
        # to the rebuilt response would make httpx try to decompress the
        # already-decompressed body on the next ``.json()`` / ``.text``
        # access, raising DecodingError. Content-Length is stale for the
        # same reason. Both must be dropped.
        safe_headers = httpx.Headers(
            [
                (key, value)
                for key, value in response.headers.multi_items()
                if key.lower() not in ("content-encoding", "content-length")
            ]
        )
        return httpx.Response(
            status_code=response.status_code,
            headers=safe_headers,
            content=body,
            request=request,
        )

    # ------------------------------------------------------------------
    # Convenience wrappers
    # ------------------------------------------------------------------
    async def get_json(
        self,
        url: str,
        *,
        params: Mapping[str, Any] | None = None,
        provider: str | None = None,
        headers: Mapping[str, str] | None = None,
    ) -> Any:
        response = await self.request(
            "GET", url, params=params, accept_mime=MIME_JSON | MIME_TEXT,
            provider=provider, headers=headers,
        )
        return response.json()

    async def get_xml(
        self,
        url: str,
        *,
        params: Mapping[str, Any] | None = None,
        provider: str | None = None,
    ) -> str:
        response = await self.request(
            "GET", url, params=params, accept_mime=MIME_XML | MIME_TEXT, provider=provider
        )
        return response.text

    async def get_html(
        self,
        url: str,
        *,
        params: Mapping[str, Any] | None = None,
        provider: str | None = None,
        required_category: str | None = None,
    ) -> SafeResponse:
        return await self.request(
            "GET",
            url,
            params=params,
            accept_mime=MIME_HTML | MIME_TEXT,
            provider=provider,
            required_category=required_category,
        )
