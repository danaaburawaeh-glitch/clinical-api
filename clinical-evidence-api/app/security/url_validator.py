"""Strict URL validation (PART 20, PART 22).

``validate_url`` is the gate every outbound request and every outbound
URL passes through. It composes three checks:

  scheme check  ->  allowlist check  ->  SSRF address check

The order matters: cheap, purely-syntactic rejections happen first so
that a hostile URL never triggers a DNS lookup.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from urllib.parse import urlsplit

from app.security.allowlist import SourceEntry, SourceRegistry, get_source_registry
from app.security.ssrf_guard import SSRFError, resolve_and_validate
from app.settings import get_settings
from app.utils.normalize import normalize_hostname

logger = logging.getLogger(__name__)

__all__ = [
    "UrlValidationError",
    "ValidatedUrl",
    "validate_url_sync",
    "validate_url",
    "is_allowed_url",
]

# Only these schemes may ever be requested. Everything else — file://,
# ftp://, gopher://, data:, javascript:, blob:, jar:, dict://, ldap://,
# php://, netdoc:// — is refused by omission rather than by blocklist,
# because an allowlist cannot be out-run by a novel scheme.
_ALLOWED_SCHEMES: frozenset[str] = frozenset({"https"})
_ALLOWED_SCHEMES_WITH_HTTP: frozenset[str] = frozenset({"https", "http"})

# Explicitly named for clear error messages and for tests.
_KNOWN_DANGEROUS_SCHEMES: frozenset[str] = frozenset(
    {
        "file", "ftp", "ftps", "gopher", "data", "javascript", "vbscript",
        "blob", "jar", "dict", "ldap", "ldaps", "tftp", "sftp", "ssh",
        "telnet", "netdoc", "php", "expect", "mailto", "ws", "wss",
    }
)

_ALLOWED_PORTS: frozenset[int] = frozenset({443, 80})


class UrlValidationError(Exception):
    """Raised when a URL is not permitted."""

    def __init__(self, reason: str, url: str | None = None, code: str = "SOURCE_NOT_ALLOWED"):
        super().__init__(reason)
        self.reason = reason
        self.url = url
        self.code = code


@dataclass(frozen=True)
class ValidatedUrl:
    """A URL that passed every check, plus the allowlist entry behind it."""

    url: str
    host: str
    scheme: str
    port: int
    entry: SourceEntry
    addresses: tuple[str, ...] = ()

    @property
    def domain(self) -> str:
        return self.entry.domain

    @property
    def category(self) -> str:
        return self.entry.category


def _allowed_schemes() -> frozenset[str]:
    return (
        _ALLOWED_SCHEMES_WITH_HTTP
        if get_settings().allow_http_scheme
        else _ALLOWED_SCHEMES
    )


def validate_url_sync(
    url: str | None,
    *,
    registry: SourceRegistry | None = None,
    required_category: str | None = None,
    allowed_categories: frozenset[str] | set[str] | None = None,
) -> ValidatedUrl:
    """Synchronous scheme + allowlist validation (no DNS).

    Use this when you only need to decide whether a URL may be *shown*
    (e.g. filtering results before serialisation). Use :func:`validate_url`
    when you are about to *fetch* it.
    """
    if not url or not isinstance(url, str):
        raise UrlValidationError("empty or non-string URL", url, "INVALID_REQUEST")

    candidate = url.strip()
    if not candidate:
        raise UrlValidationError("empty URL", url, "INVALID_REQUEST")

    # Control characters can be used to smuggle a second request line.
    if any(ord(ch) < 0x20 or ord(ch) == 0x7F for ch in candidate):
        raise UrlValidationError("URL contains control characters", url, "INVALID_REQUEST")

    if len(candidate) > 2048:
        raise UrlValidationError("URL exceeds maximum length", url, "INVALID_REQUEST")

    try:
        parts = urlsplit(candidate)
    except ValueError as exc:
        raise UrlValidationError(f"URL could not be parsed: {exc}", url, "INVALID_REQUEST") from exc

    scheme = (parts.scheme or "").lower()
    if not scheme:
        raise UrlValidationError("URL has no scheme; only https:// is accepted", url)
    if scheme in _KNOWN_DANGEROUS_SCHEMES:
        raise UrlValidationError(f"scheme {scheme!r} is not permitted", url)
    if scheme not in _allowed_schemes():
        raise UrlValidationError(
            f"scheme {scheme!r} is not permitted; only https:// is accepted", url
        )

    # Reject credentials in the authority: https://pubmed.ncbi.nlm.nih.gov@evil.com
    if "@" in (parts.netloc or ""):
        raise UrlValidationError("URL must not contain userinfo credentials", url)

    host = normalize_hostname(candidate)
    if not host:
        raise UrlValidationError("URL has no resolvable hostname", url)

    try:
        port = parts.port or (443 if scheme == "https" else 80)
    except ValueError as exc:
        raise UrlValidationError(f"invalid port: {exc}", url, "INVALID_REQUEST") from exc

    if port not in _ALLOWED_PORTS:
        raise UrlValidationError(f"port {port} is not permitted", url)

    reg = registry or get_source_registry()
    entry = reg.match_host(host)
    if entry is None:
        raise UrlValidationError(
            f"domain {host!r} is not present in the hard allowlist", url
        )

    if required_category and entry.category != required_category:
        raise UrlValidationError(
            f"domain {entry.domain!r} is category {entry.category!r}, "
            f"but {required_category!r} is required here",
            url,
        )

    if allowed_categories is not None and entry.category not in allowed_categories:
        raise UrlValidationError(
            f"domain {entry.domain!r} (category {entry.category!r}) "
            "is not permitted for this operation",
            url,
        )

    return ValidatedUrl(url=candidate, host=host, scheme=scheme, port=port, entry=entry)


async def validate_url(
    url: str | None,
    *,
    registry: SourceRegistry | None = None,
    required_category: str | None = None,
    allowed_categories: frozenset[str] | set[str] | None = None,
) -> ValidatedUrl:
    """Full validation: scheme, allowlist, then DNS/SSRF address check."""
    validated = validate_url_sync(
        url,
        registry=registry,
        required_category=required_category,
        allowed_categories=allowed_categories,
    )

    settings = get_settings()
    try:
        verdict = await resolve_and_validate(
            validated.host,
            validated.port,
            allow_unresolvable=settings.ssrf_allow_unresolvable_hosts,
        )
    except SSRFError as exc:
        raise UrlValidationError(f"SSRF guard rejected destination: {exc.reason}", url) from exc

    return ValidatedUrl(
        url=validated.url,
        host=validated.host,
        scheme=validated.scheme,
        port=validated.port,
        entry=validated.entry,
        addresses=verdict.addresses,
    )


def is_allowed_url(url: str | None, registry: SourceRegistry | None = None) -> bool:
    """Boolean convenience wrapper around :func:`validate_url_sync`."""
    try:
        validate_url_sync(url, registry=registry)
        return True
    except UrlValidationError:
        return False
