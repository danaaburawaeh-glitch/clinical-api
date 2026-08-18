"""API endpoint tests (PART 38, 40, 41, 42, 56, 57, 58, 89)."""

from __future__ import annotations

import httpx
import pytest

from app.models.schemas import EvidenceSearchRequest
from app.services.search_orchestrator import EvidenceOrchestrator, records_to_results
from tests.conftest import TEST_API_KEY, make_http_client, make_record

EUROPEPMC_PAYLOAD = {
    "resultList": {
        "result": [
            {
                "id": "34567890",
                "source": "MED",
                "pmid": "34567890",
                "doi": "10.1016/j.prosdent.2023.01.001",
                "title": "Immediate dentin sealing for indirect restorations: a systematic review and meta-analysis",
                "authorString": "Magne P, Nunes L.",
                "journalInfo": {"journal": {"title": "The Journal of prosthetic dentistry"}},
                "pubYear": "2023",
                "abstractText": "Immediate dentin sealing was associated with significantly higher retention.",
                "pubTypeList": {"pubType": ["systematic review"]},
                "isOpenAccess": "Y",
            },
            {
                "id": "55555555",
                "source": "MED",
                "pmid": "55555555",
                "title": "A prospective cohort of bonded ceramic restorations",
                "pubYear": "2022",
                "abstractText": "Patients were followed prospectively. No significant difference was found.",
                "pubTypeList": {"pubType": ["Journal Article"]},
                "journalInfo": {"journal": {"title": "Journal of Dentistry"}},
            },
        ]
    }
}


def upstream_handler(fixture_text, *, fail: set[str] | None = None):
    """Build a MockTransport handler simulating all upstream providers."""
    fail = fail or set()

    def handler(request: httpx.Request) -> httpx.Response:
        host = request.url.host
        path = request.url.path

        if host == "eutils.ncbi.nlm.nih.gov":
            if "pubmed" in fail:
                raise httpx.ReadTimeout("simulated PubMed outage", request=request)
            if "esearch" in path:
                return httpx.Response(
                    200,
                    json={"esearchresult": {"idlist": ["34567890", "22222222", "33333333"]}},
                    headers={"content-type": "application/json"},
                )
            return httpx.Response(
                200,
                content=fixture_text("pubmed_efetch.xml").encode(),
                headers={"content-type": "text/xml"},
            )

        if host == "www.ebi.ac.uk":
            if "europepmc" in fail:
                raise httpx.ReadTimeout("simulated Europe PMC outage", request=request)
            return httpx.Response(
                200, json=EUROPEPMC_PAYLOAD, headers={"content-type": "application/json"}
            )

        if host == "api.crossref.org":
            if "crossref" in fail:
                raise httpx.ReadTimeout("simulated Crossref outage", request=request)
            return httpx.Response(
                200,
                json={
                    "message": {
                        "DOI": "10.1016/j.prosdent.2023.01.001",
                        "title": [
                            "Immediate dentin sealing for indirect restorations: "
                            "a systematic review and meta-analysis"
                        ],
                        "container-title": ["Journal of Prosthetic Dentistry"],
                        "issued": {"date-parts": [[2023]]},
                        "type": "journal-article",
                    }
                },
                headers={"content-type": "application/json"},
            )

        # Guideline / manufacturer site crawling: return an empty page.
        return httpx.Response(
            200, content=b"<html><body></body></html>",
            headers={"content-type": "text/html"},
        )

    return handler


# ======================================================================
# Health (PART 42)
# ======================================================================
def test_health_is_public_and_reports_status(client):
    response = client.get("/health")
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "ok"
    assert body["version"]
    assert body["allowlisted_domains"] > 50
    names = {c["name"] for c in body["components"]}
    assert {"source_allowlist", "evidence_rules", "cache", "authentication"} <= names


def test_health_does_not_require_authentication(client):
    assert client.get("/health").status_code == 200


def test_deep_health_requires_authentication(client):
    assert client.get("/health/deep").status_code == 401


# ======================================================================
# Authentication at the HTTP layer (PART 6, 56)
# ======================================================================
@pytest.mark.parametrize(
    "path,payload",
    [
        ("/v1/evidence/search", {"query": "veneers"}),
        ("/v1/regulatory/search", {"query": "device"}),
        ("/v1/manufacturer/search", {"query": "IFU"}),
        ("/v1/source/verify", {"url": "https://pubmed.ncbi.nlm.nih.gov/1/"}),
    ],
)
def test_endpoints_reject_missing_api_key(client, path, payload):
    response = client.post(path, json=payload)
    assert response.status_code == 401
    body = response.json()
    assert body["error"]["code"] == "INVALID_API_KEY"
    assert body["error"]["retryable"] is False
    assert body["error"]["request_id"]


def test_endpoints_reject_wrong_api_key(client):
    response = client.post(
        "/v1/source/verify",
        json={"url": "https://pubmed.ncbi.nlm.nih.gov/1/"},
        headers={"X-Clinical-Key": "definitely-not-the-key"},
    )
    assert response.status_code == 401
    assert response.json()["error"]["code"] == "INVALID_API_KEY"


def test_error_response_never_contains_a_stack_trace(client):
    body = client.post("/v1/evidence/search", json={"query": "x"}).text
    assert "Traceback" not in body
    assert "/home/" not in body


# ======================================================================
# Source verification (PART 41)
# ======================================================================
def test_verify_approved_manufacturer_domain(client, auth_headers):
    response = client.post(
        "/v1/source/verify",
        json={"url": "https://www.ivoclar.com/en/p/monobond"},
        headers=auth_headers,
    )
    assert response.status_code == 200
    body = response.json()
    assert body["allowed"] is True
    assert body["domain"] == "ivoclar.com"
    assert body["source_category"] == "manufacturer"
    assert body["allowed_use"] == "IFU and product technical information only"
    assert "proving_clinical_superiority" in body["forbidden_use"]


def test_verify_unapproved_domain(client, auth_headers):
    response = client.post(
        "/v1/source/verify",
        json={"url": "https://randomdentalblog.com/best-veneers"},
        headers=auth_headers,
    )
    body = response.json()
    assert body["allowed"] is False
    assert body["domain"] == "randomdentalblog.com"
    assert body["source_category"] == "unapproved"
    assert body["allowed_use"] == "none"


def test_verify_reports_the_real_reason_for_a_bad_scheme(client, auth_headers):
    response = client.post(
        "/v1/source/verify", json={"url": "file:///etc/passwd"}, headers=auth_headers
    )
    body = response.json()
    assert body["allowed"] is False
    assert "not permitted" in body["reason"]


@pytest.mark.parametrize(
    "url",
    [
        "https://ivoclar.com.evil.com/ifu",
        "https://pubmed.ncbi.nlm.nih.gov.evil.org/1",
        "http://127.0.0.1/",
        "http://169.254.169.254/latest/meta-data/",
        "file:///etc/passwd",
    ],
)
def test_verify_blocks_attack_urls(client, auth_headers, url):
    response = client.post("/v1/source/verify", json={"url": url}, headers=auth_headers)
    assert response.json()["allowed"] is False


def test_verify_evidence_domain(client, auth_headers):
    body = client.post(
        "/v1/source/verify",
        json={"url": "https://pubmed.ncbi.nlm.nih.gov/34567890/"},
        headers=auth_headers,
    ).json()
    assert body["allowed"] is True
    assert body["source_category"] == "scientific_database"
    assert body["trust_tier"] == "TIER_A_EVIDENCE"


# ======================================================================
# Request validation (PART 56)
# ======================================================================
def test_invalid_request_returns_structured_error(client, auth_headers):
    response = client.post("/v1/evidence/search", json={}, headers=auth_headers)
    assert response.status_code == 422
    body = response.json()
    assert body["error"]["code"] == "INVALID_REQUEST"
    assert "query" in body["error"]["message"]


def test_max_results_upper_bound_enforced(client, auth_headers):
    response = client.post(
        "/v1/evidence/search",
        json={"query": "veneers", "max_results": 500},
        headers=auth_headers,
    )
    assert response.status_code == 422


def test_invalid_specialty_rejected(client, auth_headers):
    response = client.post(
        "/v1/evidence/search",
        json={"query": "veneers", "specialty": "astrology"},
        headers=auth_headers,
    )
    assert response.status_code == 422


def test_inverted_date_range_is_repaired_not_rejected():
    request = EvidenceSearchRequest(query="veneers", date_from=2024, date_to=2010)
    assert request.date_range() == (2010, 2024)


# ======================================================================
# Orchestrator end-to-end (PART 10, 38, 57, 58)
# ======================================================================
@pytest.mark.anyio
async def test_full_evidence_pipeline(fixture_text):
    client = make_http_client(upstream_handler(fixture_text))
    orchestrator = EvidenceOrchestrator(client)

    response = await orchestrator.search(
        EvidenceSearchRequest(
            query="immediate dentin sealing",
            specialty="restorative",
            intervention="immediate dentin sealing",
            comparator="delayed dentin sealing",
            outcome="retention",
            max_results=10,
            include_guidelines=False,
        ),
        request_id="test-request",
    )

    assert response.result_count > 0
    assert "PubMed" in response.successful_sources
    assert "Europe PMC" in response.successful_sources
    assert response.failed_sources == []
    assert response.partial_results is False
    assert response.request_id == "test-request"
    assert response.ranking_method and "non-GRADE" in response.ranking_method
    # The same study came from both providers and was merged.
    assert response.duplicates_merged >= 1

    top = response.results[0]
    assert top.evidence_level in {"HIGH", "GUIDELINE"}
    assert top.verified_source is True
    assert top.url.startswith("https://pubmed.ncbi.nlm.nih.gov/")
    assert "PubMed" in top.providers and "Europe PMC" in top.providers

    await client.aclose()


@pytest.mark.anyio
async def test_laboratory_record_is_labelled_in_the_response(fixture_text):
    client = make_http_client(upstream_handler(fixture_text))
    orchestrator = EvidenceOrchestrator(client)
    response = await orchestrator.search(
        EvidenceSearchRequest(query="bond strength", max_results=10, include_guidelines=False)
    )

    lab = [r for r in response.results if r.evidence_level == "EARLY_PRECLINICAL"]
    assert lab, "the thermocycling/bond-strength record must be flagged"
    assert lab[0].clinical_translation == "uncertain"
    assert any("laboratory" in w.lower() for w in response.warnings)
    await client.aclose()


@pytest.mark.anyio
async def test_retracted_record_is_flagged_and_excluded(fixture_text):
    client = make_http_client(upstream_handler(fixture_text))
    orchestrator = EvidenceOrchestrator(client)
    response = await orchestrator.search(
        EvidenceSearchRequest(query="veneer cement", max_results=10, include_guidelines=False)
    )

    assert response.excluded_count >= 1
    assert any("retracted" in w.lower() for w in response.warnings)
    # If the retracted record is surfaced at all, it carries its warning.
    for result in response.results:
        if result.pmid == "33333333":
            assert result.retraction_warning is True
            assert result.integrity_status == "retracted"
    await client.aclose()


@pytest.mark.anyio
async def test_partial_results_when_one_provider_fails(fixture_text):
    client = make_http_client(upstream_handler(fixture_text, fail={"pubmed"}))
    orchestrator = EvidenceOrchestrator(client)
    response = await orchestrator.search(
        EvidenceSearchRequest(query="veneers", max_results=5, include_guidelines=False)
    )

    assert response.partial_results is True
    assert "Europe PMC" in response.successful_sources
    assert [f.provider for f in response.failed_sources] == ["PubMed"]
    assert response.failed_sources[0].retryable is True
    assert any("PubMed temporarily unavailable" in w for w in response.warnings)
    assert response.result_count > 0  # Europe PMC still delivered
    await client.aclose()


@pytest.mark.anyio
async def test_total_failure_is_reported_honestly(fixture_text):
    client = make_http_client(
        upstream_handler(fixture_text, fail={"pubmed", "europepmc"})
    )
    orchestrator = EvidenceOrchestrator(client)
    response = await orchestrator.search(
        EvidenceSearchRequest(query="veneers", max_results=5, include_guidelines=False)
    )

    assert response.result_count == 0
    assert response.insufficient_evidence is True
    assert response.successful_sources == []
    assert len(response.failed_sources) == 2
    assert any("No substitute sources were consulted" in w for w in response.warnings)
    await client.aclose()


@pytest.mark.anyio
async def test_evidence_summary_states_no_statistics(fixture_text):
    client = make_http_client(upstream_handler(fixture_text))
    orchestrator = EvidenceOrchestrator(client)
    response = await orchestrator.search(
        EvidenceSearchRequest(query="dentin sealing", max_results=10, include_guidelines=False)
    )

    summary = response.evidence_summary or ""
    assert "no effect size, p-value, confidence interval" in summary.lower()
    # It must not invent statistics.
    for forbidden in ("p =", "p<0.0", "95% CI", "n ="):
        assert forbidden not in summary


@pytest.mark.anyio
async def test_crossref_validation_runs_and_is_reported(fixture_text):
    client = make_http_client(upstream_handler(fixture_text))
    orchestrator = EvidenceOrchestrator(client)
    response = await orchestrator.search(
        EvidenceSearchRequest(query="dentin sealing", max_results=10, include_guidelines=False)
    )
    assert "Crossref" in response.searched_sources
    await client.aclose()


# ======================================================================
# URL re-validation at serialisation time
# ======================================================================
def test_records_with_unapproved_urls_are_stripped_not_emitted():
    """Last line of defence: a bad URL never reaches the client."""
    record = make_record(
        source_domain="pubmed.ncbi.nlm.nih.gov",
        url="https://malicious-mirror.example.com/article/1",
    )
    results = records_to_results([record])
    assert results[0].url is None
    assert results[0].verified_source is False


def test_records_with_approved_urls_are_verified():
    record = make_record(url="https://pubmed.ncbi.nlm.nih.gov/34567890/")
    results = records_to_results([record])
    assert results[0].verified_source is True
    assert results[0].url == "https://pubmed.ncbi.nlm.nih.gov/34567890/"


def test_result_carries_source_category_and_trust_tier():
    results = records_to_results([make_record()])
    assert results[0].source_category == "scientific_database"
    assert results[0].trust_tier == "TIER_A_EVIDENCE"


def test_missing_identifiers_serialise_as_null():
    record = make_record(pmid=None, doi=None, pmcid=None, publication_year=None)
    result = records_to_results([record])[0]
    assert result.pmid is None
    assert result.doi is None
    assert result.pmcid is None
    assert result.publication_year is None


def test_full_text_reviewed_defaults_to_false():
    assert records_to_results([make_record()])[0].full_text_reviewed is False


# ======================================================================
# OpenAPI schema
# ======================================================================
def test_openapi_exposes_the_expected_operation_ids(client):
    schema = client.get("/openapi.json").json()
    operation_ids = {
        operation["operationId"]
        for path in schema["paths"].values()
        for method, operation in path.items()
        if method in {"get", "post"} and "operationId" in operation
    }
    assert {
        "searchClinicalEvidence",
        "searchRegulatoryEvidence",
        "getManufacturerDocument",
        "verifyClinicalSource",
    } <= operation_ids


def test_openapi_declares_the_api_key_security_scheme(client):
    schema = client.get("/openapi.json").json()
    scheme = schema["components"]["securitySchemes"]["ClinicalAPIKey"]
    assert scheme == {"type": "apiKey", "in": "header", "name": "X-Clinical-Key"}
    assert schema["security"] == [{"ClinicalAPIKey": []}]


def test_response_carries_request_id_header(client):
    response = client.get("/health")
    assert response.headers.get("X-Request-ID")
    assert response.headers.get("X-Content-Type-Options") == "nosniff"


# ======================================================================
# Rate limiting (PART 35)
# ======================================================================
@pytest.mark.anyio
async def test_sliding_window_limiter_blocks_over_limit():
    from app.utils.helpers import SlidingWindowLimiter

    limiter = SlidingWindowLimiter(limit=3, window_seconds=60)
    for _ in range(3):
        allowed, _, _ = await limiter.check("key")
        assert allowed is True

    allowed, remaining, retry_after = await limiter.check("key")
    assert allowed is False
    assert remaining == 0
    assert retry_after > 0


@pytest.mark.anyio
async def test_rate_limit_is_per_key():
    from app.utils.helpers import SlidingWindowLimiter

    limiter = SlidingWindowLimiter(limit=1, window_seconds=60)
    assert (await limiter.check("a"))[0] is True
    assert (await limiter.check("a"))[0] is False
    assert (await limiter.check("b"))[0] is True
