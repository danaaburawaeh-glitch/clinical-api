"""SSRF protection (PART 21).

Two independent checks run before any socket is opened:

1. **Name check** — the hostname must resolve to an entry in the hard
   allowlist. Handled by :mod:`app.security.url_validator`.
2. **Address check** — every IP address the hostname resolves to must be
   a public, routable unicast address. This is what stops
   ``metadata.internal-looking-domain.com`` (an attacker-controlled DNS
   name pointing at ``169.254.169.254``) and rebinding attempts.

Both checks are re-run on every redirect hop, because a 302 is a fresh
destination and inherits none of the original URL's trust.

Residual risk, stated honestly: a full DNS-rebinding defence requires
pinning the validated IP into the socket connection itself. This module
resolves, validates, and then hands the hostname to httpx, leaving a
narrow TOCTOU window. Mitigations in place: short DNS-to-connect delay,
per-hop revalidation, and the fact that the hostname must *also* be on
the allowlist — an attacker would need to control DNS for an approved
domain such as ``pubmed.ncbi.nlm.nih.gov``. See README limitations.
"""

from __future__ import annotations

import asyncio
import ipaddress
import logging
import socket
from dataclasses import dataclass

from app.utils.normalize import normalize_hostname

logger = logging.getLogger(__name__)

__all__ = [
    "SSRFError",
    "SSRFVerdict",
    "is_public_ip",
    "check_ip_literal",
    "resolve_and_validate",
    "BLOCKED_HOSTNAMES",
]


class SSRFError(Exception):
    """Raised when a destination fails SSRF validation."""

    def __init__(self, reason: str, host: str | None = None) -> None:
        super().__init__(reason)
        self.reason = reason
        self.host = host


@dataclass(frozen=True)
class SSRFVerdict:
    """Outcome of an SSRF check."""

    allowed: bool
    host: str
    addresses: tuple[str, ...] = ()
    reason: str = ""


# Hostnames rejected outright regardless of DNS.
BLOCKED_HOSTNAMES: frozenset[str] = frozenset(
    {
        "localhost",
        "localhost.localdomain",
        "ip6-localhost",
        "ip6-loopback",
        "metadata",
        "metadata.google.internal",
        "metadata.goog",
        "instance-data",
        "instance-data.ec2.internal",
        "169.254.169.254",
        "kubernetes",
        "kubernetes.default",
        "kubernetes.default.svc",
    }
)

# Hostname suffixes that indicate an internal network name.
_BLOCKED_SUFFIXES: tuple[str, ...] = (
    ".local",
    ".localdomain",
    ".internal",
    ".intranet",
    ".lan",
    ".home",
    ".corp",
    ".private",
    ".test",
    ".example",
    ".invalid",
    ".onion",
    ".svc",
    ".svc.cluster.local",
    ".cluster.local",
)

# Cloud metadata endpoints, checked explicitly in addition to the
# link-local range (defence in depth / readability).
_METADATA_ADDRESSES: frozenset[str] = frozenset(
    {
        "169.254.169.254",   # AWS / Azure / DigitalOcean / OpenStack
        "169.254.170.2",     # AWS ECS task metadata
        "100.100.100.200",   # Alibaba Cloud
        "192.0.0.192",       # Oracle Cloud (legacy)
        "fd00:ec2::254",     # AWS IMDSv2 over IPv6
    }
)


def is_public_ip(address: str) -> tuple[bool, str]:
    """Return ``(is_public, reason)`` for a textual IP address.

    Anything that is not a globally routable unicast address is refused:
    loopback, private (RFC1918 / ULA), link-local (including the cloud
    metadata range), multicast, reserved, unspecified, carrier-grade NAT
    and IPv4-mapped IPv6 forms of any of the above.
    """
    try:
        ip = ipaddress.ip_address(address)
    except ValueError:
        return False, f"not a valid IP address: {address!r}"

    # Unwrap IPv4-mapped / 6to4-style IPv6 so that
    # ::ffff:127.0.0.1 cannot slip past the IPv4 checks.
    if isinstance(ip, ipaddress.IPv6Address):
        if ip.ipv4_mapped is not None:
            return is_public_ip(str(ip.ipv4_mapped))
        if ip.sixtofour is not None:
            return is_public_ip(str(ip.sixtofour))
        if getattr(ip, "teredo", None):
            return False, "Teredo tunnelled address"

    if str(ip) in _METADATA_ADDRESSES:
        return False, "cloud metadata service address"
    if ip.is_unspecified:
        return False, "unspecified address (0.0.0.0 / ::)"
    if ip.is_loopback:
        return False, "loopback address"
    if ip.is_link_local:
        return False, "link-local address"
    if ip.is_private:
        return False, "private / internal address"
    if ip.is_multicast:
        return False, "multicast address"
    if ip.is_reserved:
        return False, "reserved address"
    if not ip.is_global:
        return False, "non-globally-routable address"

    # Carrier-grade NAT 100.64.0.0/10 — is_private covers it on modern
    # Python, but assert explicitly so behaviour is version-independent.
    if isinstance(ip, ipaddress.IPv4Address):
        if ip in ipaddress.ip_network("100.64.0.0/10"):
            return False, "carrier-grade NAT address"

    return True, "public unicast address"


def check_ip_literal(host: str) -> tuple[bool, str] | None:
    """If ``host`` is a bare IP literal, validate it; else return ``None``."""
    stripped = host.strip().strip("[]")
    try:
        ipaddress.ip_address(stripped)
    except ValueError:
        return None
    return is_public_ip(stripped)


def _hostname_is_structurally_blocked(host: str) -> str | None:
    """Return a rejection reason, or ``None`` if the name looks fine."""
    if not host:
        return "empty hostname"
    if host in BLOCKED_HOSTNAMES:
        return f"blocked hostname: {host}"
    if "." not in host:
        # Single-label names are internal by definition on any sane network.
        return "single-label (internal) hostname"
    for suffix in _BLOCKED_SUFFIXES:
        if host.endswith(suffix):
            return f"internal hostname suffix: {suffix}"
    return None


async def _getaddrinfo(host: str, port: int) -> list[str]:
    """Resolve ``host`` to a list of textual IP addresses (off-thread)."""
    loop = asyncio.get_running_loop()
    infos = await loop.getaddrinfo(
        host, port, family=socket.AF_UNSPEC, type=socket.SOCK_STREAM
    )
    addresses: list[str] = []
    for info in infos:
        sockaddr = info[4]
        if sockaddr and isinstance(sockaddr[0], str):
            addr = sockaddr[0].split("%")[0]  # strip IPv6 zone id
            if addr not in addresses:
                addresses.append(addr)
    return addresses


async def resolve_and_validate(
    host: str,
    port: int = 443,
    *,
    allow_unresolvable: bool = False,
) -> SSRFVerdict:
    """Resolve ``host`` and verify every resulting address is public.

    Raises :class:`SSRFError` on any violation. A hostname that resolves
    to *any* non-public address is rejected in full — a mixed A-record
    set is a classic rebinding signature, not a partial success.

    ``allow_unresolvable`` exists solely for unit tests running against
    httpx mock transports, where no real DNS lookup should occur. It is
    driven by ``SSRF_ALLOW_UNRESOLVABLE_HOSTS`` and defaults to false.
    """
    normalised = normalize_hostname(host)
    if not normalised:
        raise SSRFError("hostname could not be normalised", host)

    literal_verdict = check_ip_literal(normalised)
    if literal_verdict is not None:
        ok, reason = literal_verdict
        if not ok:
            raise SSRFError(f"IP literal rejected: {reason}", normalised)
        return SSRFVerdict(True, normalised, (normalised,), reason)

    structural = _hostname_is_structurally_blocked(normalised)
    if structural:
        raise SSRFError(structural, normalised)

    try:
        addresses = await _getaddrinfo(normalised, port)
    except (socket.gaierror, OSError) as exc:
        if allow_unresolvable:
            logger.debug("ssrf_resolution_skipped", extra={"host": normalised})
            return SSRFVerdict(True, normalised, (), "resolution skipped (test mode)")
        raise SSRFError(f"DNS resolution failed: {exc.__class__.__name__}", normalised) from exc

    if not addresses:
        raise SSRFError("hostname resolved to no addresses", normalised)

    for address in addresses:
        ok, reason = is_public_ip(address)
        if not ok:
            raise SSRFError(
                f"hostname resolves to a non-public address ({address}: {reason})",
                normalised,
            )

    return SSRFVerdict(True, normalised, tuple(addresses), "all addresses public")
