"""Shared pytest fixtures.

Tests never touch the network. Upstream providers are simulated with
``httpx.MockTransport``, which exercises the real request-building,
redirect-handling and parsing code paths while keeping the suite
deterministic and offline.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

# Environment must be set before app.settings is first imported.
os.environ.setdefault("CLINICAL_API_KEY", "test-secret-key")
os.environ.setdefault("ENVIRONMENT", "test")
os.environ.setdefault("LOG_FORMAT", "plain")
os.environ.setdefault("LOG_LEVEL", "WARNING")
os.environ.setdefault("CACHE_ENABLED", "false")
os.environ.setdefault("RATE_LIMIT_ENABLED", "false")
# Mock transports target hosts that do not resolve; skip the DNS probe.
os.environ.setdefault("SSRF_ALLOW_UNRESOLVABLE_HOSTS", "true")

import httpx  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from app.engines.base import RawRecord  # noqa: E402
from app.security.allowlist import get_source_registry  # noqa: E402
from app.security.safe_http import SafeHttpClient  # noqa: E402
from app.settings import get_settings  # noqa: E402

FIXTURES = Path(__file__).parent / "fixtures"

TEST_API_KEY = "test-secret-key"


@pytest.fixture(scope="session")
def anyio_backend() -> str:
    return "asyncio"


@pytest.fixture
def settings():
    return get_settings()


@pytest.fixture
def registry():
    return get_source_registry()


@pytest.fixture
def fixture_text():
    def _load(name: str) -> str:
        return (FIXTURES / name).read_text(encoding="utf-8")

    return _load


def make_http_client(handler) -> SafeHttpClient:
    """Build a :class:`SafeHttpClient` backed by a mock transport."""
    transport = httpx.MockTransport(handler)
    client = httpx.AsyncClient(
        transport=transport,
        follow_redirects=False,
        headers={"User-Agent": "test"},
    )
    return SafeHttpClient(client=client)


@pytest.fixture
def http_factory():
    return make_http_client


@pytest.fixture
def client():
    """FastAPI test client with the app's real dependency graph."""
    from app.main import app

    with TestClient(app) as test_client:
        yield test_client


@pytest.fixture
def auth_headers() -> dict[str, str]:
    return {"X-Clinical-Key": TEST_API_KEY}


# ----------------------------------------------------------------------
# Record builders
# ----------------------------------------------------------------------
def make_record(**overrides) -> RawRecord:
    """Build a RawRecord with sensible defaults for pipeline tests."""
    defaults: dict = {
        "provider": "PubMed",
        "source_domain": "pubmed.ncbi.nlm.nih.gov",
        "url": "https://pubmed.ncbi.nlm.nih.gov/11111111/",
        "title": "A clinical study of something dental",
        "authors": ["Smith J", "Jones A"],
        "journal": "Journal of Dentistry",
        "publication_year": 2023,
        "abstract": "This study evaluated a dental intervention in patients.",
        "pmid": "11111111",
        "publication_types": ["Journal Article"],
    }
    defaults.update(overrides)
    return RawRecord(**defaults)


@pytest.fixture
def record_factory():
    return make_record
