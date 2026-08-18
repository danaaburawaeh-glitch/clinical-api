"""SSRF, redirect, response-size, MIME and authentication tests
(PART 21, PART 22, PART 23, PART 6, PART 47)."""

from __future__ import annotations

import httpx
import pytest

from app.security.authentication import AuthError, authenticate_request, key_fingerprint
from app.security.safe_http import (
    DisallowedContentType,
    ResponseTooLarge,
    SafeHttpClient,
    UpstreamError,
)
from app.security.ssrf_guard import (
    SSRFError,
    check_ip_literal,
    is_public_ip,
    resolve_and_validate,
)
from app.security.url_validator import UrlValidationError, validate_url_sync
from app.settings import Settings
from app.utils.helpers import redact_secrets, scrub_pii
from tests.conftest import make_http_client


# ======================================================================
# SSRF — IP classification
# ======================================================================
@pytest.mark.parametrize(
    "address",
    [
        "127.0.0.1",
        "127.1.2.3",
        "0.0.0.0",
        "10.0.0.5",
        "172.16.0.1",
        "172.31.255.254",
        "192.168.1.1",
        "169.254.169.254",   # AWS/Azure/GCP metadata
        "169.254.170.2",     # ECS task metadata
        "100.100.100.200",   # Alibaba metadata
        "100.64.0.1",        # carrier-grade NAT
        "224.0.0.1",         # multicast
        "::1",               # IPv6 loopback
        "fe80::1",           # IPv6 link-local
        "fc00::1",           # IPv6 ULA
        "::ffff:127.0.0.1",  # IPv4-mapped loopback
        "::ffff:10.0.0.1",   # IPv4-mapped private
        "::",                # unspecified
    ],
)
def test_private_and_special_addresses_rejected(address):
    allowed, reason = is_public_ip(address)
    assert allowed is False, f"{address} was wrongly allowed ({reason})"


@pytest.mark.parametrize("address", ["8.8.8.8", "1.1.1.1", "130.14.29.110", "2606:4700::1111"])
def test_public_addresses_allowed(address):
    allowed, _ = is_public_ip(address)
    assert allowed is True


def test_garbage_is_not_an_ip():
    allowed, reason = is_public_ip("not-an-ip")
    assert allowed is False
    assert "valid IP" in reason


def test_check_ip_literal_returns_none_for_hostname():
    assert check_ip_literal("pubmed.ncbi.nlm.nih.gov") is None
    assert check_ip_literal("127.0.0.1") == (False, "loopback address")


# ======================================================================
# SSRF — hostname resolution
# ======================================================================
@pytest.mark.anyio
@pytest.mark.parametrize(
    "host",
    [
        "localhost",
        "metadata.google.internal",
        "instance-data",
        "kubernetes.default.svc",
        "myservice.internal",
        "db.cluster.local",
        "printer.lan",
        "single-label",
    ],
)
async def test_internal_hostnames_rejected(host):
    with pytest.raises(SSRFError):
        await resolve_and_validate(host, allow_unresolvable=True)


@pytest.mark.anyio
async def test_ip_literal_loopback_rejected():
    with pytest.raises(SSRFError, match="loopback"):
        await resolve_and_validate("127.0.0.1", allow_unresolvable=True)


@pytest.mark.anyio
async def test_metadata_ip_rejected():
    with pytest.raises(SSRFError, match="metadata|link-local"):
        await resolve_and_validate("169.254.169.254", allow_unresolvable=True)


# ======================================================================
# Combined URL validation for classic SSRF payloads
# ======================================================================
@pytest.mark.parametrize(
    "url",
    [
        "http://127.0.0.1",
        "https://127.0.0.1/admin",
        "http://169.254.169.254/latest/meta-data/",
        "file:///etc/passwd",
        "https://localhost:443/x",
        "https://[::1]/x",
        "https://10.0.0.1/internal",
    ],
)
def test_ssrf_payload_urls_blocked_synchronously(url, registry):
    # Every one of these fails before DNS: either on scheme or on the
    # fact that the host is not an allowlisted domain.
    with pytest.raises(UrlValidationError):
        validate_url_sync(url, registry=registry)


# ======================================================================
# Redirect validation (PART 22)
# ======================================================================
@pytest.mark.anyio
async def test_redirect_to_unapproved_domain_is_blocked():
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host == "pubmed.ncbi.nlm.nih.gov":
            return httpx.Response(
                302, headers={"location": "https://unapproved-blog.com/stolen"}
            )
        return httpx.Response(200, json={"leaked": True})

    client = make_http_client(handler)
    with pytest.raises(UrlValidationError, match="unapproved destination"):
        await client.request("GET", "https://pubmed.ncbi.nlm.nih.gov/1/")
    await client.aclose()


@pytest.mark.anyio
async def test_redirect_to_internal_ip_is_blocked():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(302, headers={"location": "http://169.254.169.254/latest/"})

    client = make_http_client(handler)
    with pytest.raises(UrlValidationError):
        await client.request("GET", "https://pubmed.ncbi.nlm.nih.gov/1/")
    await client.aclose()


@pytest.mark.anyio
async def test_redirect_within_approved_domains_is_followed():
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/old":
            return httpx.Response(
                301, headers={"location": "https://europepmc.org/new"}
            )
        return httpx.Response(
            200, json={"ok": True}, headers={"content-type": "application/json"}
        )

    client = make_http_client(handler)
    response = await client.request("GET", "https://europepmc.org/old")
    assert response.status_code == 200
    assert response.json() == {"ok": True}
    assert response.redirect_chain == ["https://europepmc.org/new"]
    await client.aclose()


@pytest.mark.anyio
async def test_relative_redirect_is_resolved_and_validated():
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(str(request.url))
        if request.url.path == "/a":
            return httpx.Response(302, headers={"location": "/b"})
        return httpx.Response(
            200, json={"ok": True}, headers={"content-type": "application/json"}
        )

    client = make_http_client(handler)
    response = await client.request("GET", "https://europepmc.org/a")
    assert response.status_code == 200
    assert calls[-1] == "https://europepmc.org/b"
    await client.aclose()


@pytest.mark.anyio
async def test_redirect_loop_hits_the_limit():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(302, headers={"location": "https://europepmc.org/loop"})

    client = make_http_client(handler)
    with pytest.raises(UpstreamError, match="redirect limit"):
        await client.request("GET", "https://europepmc.org/loop")
    await client.aclose()


@pytest.mark.anyio
async def test_redirect_without_location_header_errors():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(302)

    client = make_http_client(handler)
    with pytest.raises(UpstreamError, match="Location"):
        await client.request("GET", "https://europepmc.org/x")
    await client.aclose()


# ======================================================================
# Response size and content type (PART 23)
# ======================================================================
@pytest.mark.anyio
async def test_oversized_declared_content_length_rejected():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            content=b"{}",
            headers={"content-type": "application/json", "content-length": "999999999"},
        )

    client = make_http_client(handler)
    with pytest.raises(ResponseTooLarge):
        await client.request("GET", "https://europepmc.org/x", max_bytes=1024)
    await client.aclose()


@pytest.mark.anyio
async def test_oversized_actual_body_rejected_even_if_content_length_lies():
    body = b"x" * 5000

    def handler(request: httpx.Request) -> httpx.Response:
        # Deliberately understate the length; the reader must still stop.
        return httpx.Response(
            200, content=body, headers={"content-type": "application/json"}
        )

    client = make_http_client(handler)
    with pytest.raises(ResponseTooLarge):
        await client.request("GET", "https://europepmc.org/x", max_bytes=100)
    await client.aclose()


@pytest.mark.anyio
async def test_unexpected_content_type_rejected():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200, content=b"<html></html>", headers={"content-type": "text/html"}
        )

    client = make_http_client(handler)
    with pytest.raises(DisallowedContentType):
        await client.get_json("https://europepmc.org/x")
    await client.aclose()


@pytest.mark.anyio
async def test_upstream_429_becomes_rate_limit_error():
    from app.security.safe_http import UpstreamRateLimited

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(429, json={})

    client = make_http_client(handler)
    with pytest.raises(UpstreamRateLimited):
        await client.request("GET", "https://europepmc.org/x")
    await client.aclose()


@pytest.mark.anyio
async def test_timeout_becomes_upstream_timeout():
    from app.security.safe_http import UpstreamTimeout

    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("simulated", request=request)

    client = make_http_client(handler)
    with pytest.raises(UpstreamTimeout):
        await client.request("GET", "https://europepmc.org/x")
    await client.aclose()


@pytest.mark.anyio
async def test_client_cannot_reach_unapproved_domain_at_all():
    called = False

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal called
        called = True
        return httpx.Response(200, json={})

    client = make_http_client(handler)
    with pytest.raises(UrlValidationError):
        await client.request("GET", "https://randomdentalblog.com/best")
    assert called is False, "the transport must never be reached for a blocked host"
    await client.aclose()


# ======================================================================
# Authentication (PART 6, PART 55)
# ======================================================================
class _FakeRequest:
    def __init__(self, headers: dict[str, str]) -> None:
        self.headers = headers


def _settings(**overrides) -> Settings:
    base = {
        "clinical_api_key": "correct-key",
        "clinical_api_keys": "",
        "revoked_api_keys": "",
        "auth_required": True,
    }
    base.update(overrides)
    return Settings(**base)


def test_valid_key_accepted():
    identity = authenticate_request(
        _FakeRequest({"X-Clinical-Key": "correct-key"}), _settings()
    )
    assert identity.key_name == "default"


def test_invalid_key_rejected():
    with pytest.raises(AuthError, match="Invalid API key"):
        authenticate_request(_FakeRequest({"X-Clinical-Key": "wrong"}), _settings())


def test_missing_key_rejected():
    with pytest.raises(AuthError, match="Missing"):
        authenticate_request(_FakeRequest({}), _settings())


def test_bearer_token_accepted_for_local_testing():
    identity = authenticate_request(
        _FakeRequest({"Authorization": "Bearer correct-key"}), _settings()
    )
    assert identity.key_name == "default"


def test_multiple_named_keys():
    cfg = _settings(
        clinical_api_key="", clinical_api_keys="prod:key-a,staging:key-b"
    )
    assert authenticate_request(_FakeRequest({"X-Clinical-Key": "key-a"}), cfg).key_name == "prod"
    assert (
        authenticate_request(_FakeRequest({"X-Clinical-Key": "key-b"}), cfg).key_name
        == "staging"
    )


def test_revoked_key_rejected():
    cfg = _settings(clinical_api_keys="prod:key-a", revoked_api_keys="key-a")
    with pytest.raises(AuthError):
        authenticate_request(_FakeRequest({"X-Clinical-Key": "key-a"}), cfg)


def test_server_without_keys_fails_closed():
    cfg = _settings(clinical_api_key="", clinical_api_keys="")
    with pytest.raises(AuthError, match="no API keys configured"):
        authenticate_request(_FakeRequest({"X-Clinical-Key": "anything"}), cfg)


def test_fingerprint_does_not_reveal_key():
    fingerprint = key_fingerprint("super-secret-value")
    assert "super" not in fingerprint
    assert len(fingerprint) == 12
    assert key_fingerprint("super-secret-value") == fingerprint


# ======================================================================
# Log hygiene (PART 36, PART 37)
# ======================================================================
def test_redact_secrets_removes_api_keys():
    assert "sk-abc123" not in redact_secrets("X-Clinical-Key: sk-abc123")
    assert "tok-9" not in redact_secrets("Authorization: Bearer tok-9")
    assert "hidden" not in redact_secrets("https://api.example.org/x?api_key=hidden")
    assert "v3ry" not in redact_secrets('{"token": "v3ry-secret"}')


def test_scrub_pii_removes_direct_identifiers():
    scrubbed = scrub_pii(
        "Patient 1234567890123 born 1985-04-12, email a.patient@example.com, "
        "phone 555-123-4567"
    )
    assert "1234567890123" not in scrubbed
    assert "a.patient@example.com" not in scrubbed
    assert "1985-04-12" not in scrubbed
    assert "555-123-4567" not in scrubbed


def test_scrub_pii_truncates():
    assert len(scrub_pii("a" * 500, max_chars=50)) <= 51
