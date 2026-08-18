"""The 15 mandatory clinical scenarios from PART 46, as executable tests.

Each test asserts the *server-side* behaviour that must hold for the
scenario. Whether the Custom GPT picks the right Action is a prompt
concern (covered by the manual checklist in
``docs/MANUAL_TEST_CHECKLIST.md``); what is tested here is that the
backend cannot produce an unsafe answer regardless of which Action the
model calls.
"""

from __future__ import annotations

import httpx
import pytest

from app.engines.manufacturer_search import ManufacturerSearchEngine
from app.engines.openfda import OpenFdaEngine
from app.evidence.classifier import EvidenceClassifier, classify_records
from app.evidence.query_expander import get_query_expander
from app.models.schemas import EvidenceSearchRequest
from app.services.search_orchestrator import EvidenceOrchestrator
from tests.conftest import make_http_client, make_record
from tests.test_api import upstream_handler


# ----------------------------------------------------------------------
# Test 1 — "ما بروتوكول إلصاق lithium disilicate veneers؟"
#          Expected: clinical evidence search
# ----------------------------------------------------------------------
@pytest.mark.anyio
async def test_scenario_01_lithium_disilicate_veneer_bonding(fixture_text):
    expansion = get_query_expander().expand(
        "bonding protocol for lithium disilicate veneers", specialty="esthetic_dentistry"
    )
    assert "lithium_disilicate" in expansion.matched_concepts
    assert "veneers" in expansion.matched_concepts
    assert "bonding" in expansion.matched_concepts
    # The user's own wording survives expansion.
    assert "(bonding protocol for lithium disilicate veneers)" in expansion.expanded_query

    client = make_http_client(upstream_handler(fixture_text))
    response = await EvidenceOrchestrator(client).search(
        EvidenceSearchRequest(
            query="bonding protocol for lithium disilicate veneers",
            specialty="esthetic_dentistry",
            include_guidelines=False,
        )
    )
    assert response.result_count > 0
    assert "PubMed" in response.successful_sources
    await client.aclose()


# ----------------------------------------------------------------------
# Test 2 — strongest evidence for immediate dentin sealing
#          Expected: independent clinical evidence, ranked by design
# ----------------------------------------------------------------------
@pytest.mark.anyio
async def test_scenario_02_strongest_evidence_is_ranked_first(fixture_text):
    client = make_http_client(upstream_handler(fixture_text))
    response = await EvidenceOrchestrator(client).search(
        EvidenceSearchRequest(
            query="immediate dentin sealing", specialty="restorative",
            include_guidelines=False,
        )
    )
    assert response.results[0].evidence_level in {"HIGH", "GUIDELINE"}
    assert response.results[0].evidence_class in {"A1", "A2", "A3"}
    # And every returned record is from an approved domain.
    for result in response.results:
        assert result.source_domain in {
            "pubmed.ncbi.nlm.nih.gov", "europepmc.org", "crossref.org",
        }
    await client.aclose()


# ----------------------------------------------------------------------
# Test 3 — IDS vs DDS: comparison, evidence first
# ----------------------------------------------------------------------
def test_scenario_03_pico_comparison_builds_a_balanced_query():
    result = get_query_expander().expand(
        "is immediate dentin sealing clinically better than delayed dentin sealing",
        intervention="immediate dentin sealing",
        comparator="delayed dentin sealing",
        outcome="survival / debonding / postoperative sensitivity",
    )
    # Intervention and comparator are ORed, not ANDed, so trials naming
    # only one arm in the abstract are not lost.
    assert "immediate dentin sealing" in result.pico_clause
    assert "delayed dentin sealing" in result.pico_clause
    assert " OR " in result.pico_clause
    assert "debonding" in result.pico_clause


# ----------------------------------------------------------------------
# Test 4/5 — official Ivoclar instructions; official e.max etching time
#            Expected: manufacturer engine, official domain only
# ----------------------------------------------------------------------
@pytest.mark.anyio
async def test_scenario_04_05_manufacturer_engine_official_domain_only():
    hosts: list[str] = []
    page = (
        b'<html><body><a href="/en/downloads/ifu-monobond.pdf">'
        b"Monobond Etch &amp; Prime Instructions for Use</a></body></html>"
    )

    def handler(request: httpx.Request) -> httpx.Response:
        hosts.append(request.url.host)
        return httpx.Response(200, content=page, headers={"content-type": "text/html"})

    client = make_http_client(handler)
    outcome = await ManufacturerSearchEngine(client).search(
        "official Ivoclar instructions for Monobond Etch and Prime",
        manufacturer="Ivoclar", document_type="IFU", max_results=1,
    )
    assert outcome.records
    assert all(h.endswith("ivoclar.com") for h in hosts)
    classify_records(outcome.records)
    assert outcome.records[0].evidence_level == "MANUFACTURER_INFORMATION"
    await client.aclose()


# ----------------------------------------------------------------------
# Test 6 — "is 20 s better than 60 s clinically?"
#          Expected: evidence first; the IFU is secondary
# ----------------------------------------------------------------------
def test_scenario_06_manufacturer_claim_cannot_answer_a_superiority_question():
    manufacturer_claim = make_record(
        source_domain="ivoclar.com",
        title="20 seconds delivers superior, clinically proven bond performance",
        abstract="Our data demonstrate superior clinical outcomes.",
        publication_types=["Randomized Controlled Trial", "Meta-Analysis"],
        pmid=None,
    )
    result = EvidenceClassifier().classify(manufacturer_claim)
    # No wording on a manufacturer page can produce HIGH evidence.
    assert result.evidence_level == "MANUFACTURER_INFORMATION"
    assert result.evidence_class == "M"


# ----------------------------------------------------------------------
# Test 7 — "Is Videa Dental AI K251002 FDA cleared?"
#          Expected: regulatory engine, exact identifier lookup
# ----------------------------------------------------------------------
@pytest.mark.anyio
async def test_scenario_07_exact_identifier_lookup():
    searches: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        searches.append(request.url.params.get("search", ""))
        return httpx.Response(
            200,
            json={
                "results": [
                    {
                        "k_number": "K251002",
                        "device_name": "Videa Dental AI",
                        "applicant": "VideaHealth Inc",
                        "decision_description": "Substantially Equivalent",
                        "decision_date": "2025-06-11",
                    }
                ]
            },
            headers={"content-type": "application/json"},
        )

    client = make_http_client(handler)
    records = await OpenFdaEngine(client).search("Is Videa Dental AI K251002 FDA cleared?")
    assert searches[0] == 'k_number:"K251002"'
    assert records[0].regulatory_identifier == "K251002"
    assert records[0].source_domain == "api.fda.gov"
    await client.aclose()


# ----------------------------------------------------------------------
# Test 8 — "Does FDA clearance mean it is better than competitors?"
#          Expected: No. Regulatory status ≠ clinical superiority.
# ----------------------------------------------------------------------
def test_scenario_08_clearance_is_never_superiority(client, auth_headers, monkeypatch):
    from app.engines.base import RawRecord

    async def fake(self, query, **kwargs):
        return [
            RawRecord(
                provider="openFDA", source_domain="api.fda.gov",
                url="https://accessdata.fda.gov/scripts/cdrh/cfdocs/cfpmn/pmn.cfm",
                title="Device — FDA 510(k)",
                regulatory_authority="FDA",
                regulatory_pathway="510(k) premarket notification (cleared)",
            )
        ]

    monkeypatch.setattr(OpenFdaEngine, "search", fake)
    body = client.post(
        "/v1/regulatory/search",
        json={"query": "does FDA clearance mean better", "authority": "FDA"},
        headers=auth_headers,
    ).json()

    assert body["results"][0]["evidence_level"] == "REGULATORY"
    joined = " ".join(body["warnings"])
    assert "never establishes superiority" in joined
    assert "'clearance'" in joined and "'approval'" in joined


# ----------------------------------------------------------------------
# Test 9/10 — product comparison and composition
# ----------------------------------------------------------------------
def test_scenario_09_product_comparison_keeps_the_two_streams_separate():
    records = [
        make_record(
            pmid=None, source_domain="ivoclar.com",
            title="Variolink Esthetic technical data", publication_types=[],
        ),
        make_record(
            pmid="1", title="Randomized clinical trial comparing two resin cements",
            publication_types=["Randomized Controlled Trial"],
        ),
    ]
    classify_records(records)
    levels = {r.source_domain: r.evidence_level for r in records}
    assert levels["ivoclar.com"] == "MANUFACTURER_INFORMATION"
    assert levels["pubmed.ncbi.nlm.nih.gov"] == "HIGH"


def test_scenario_10_composition_comes_from_the_official_source(registry):
    """Composition is an allowed manufacturer use; superiority is not."""
    entry = registry.match_host("ivoclar.com")
    assert "composition" in entry.allowed_for
    assert "proving_clinical_superiority" in entry.forbidden_for


# ----------------------------------------------------------------------
# Test 11 — "Is zirconia etched with HF?"
# ----------------------------------------------------------------------
def test_scenario_11_zirconia_hf_expands_to_both_concepts():
    result = get_query_expander().expand("is zirconia etched with hydrofluoric acid")
    assert "zirconia" in result.matched_concepts
    assert "hydrofluoric_etching" in result.matched_concepts


# ----------------------------------------------------------------------
# Test 12 — "give me the best protocol even if you find no studies"
#           Expected: insufficient_evidence, no invention
# ----------------------------------------------------------------------
@pytest.mark.anyio
async def test_scenario_12_no_evidence_means_insufficient_not_invented():
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host == "eutils.ncbi.nlm.nih.gov":
            return httpx.Response(200, json={"esearchresult": {"idlist": []}},
                                  headers={"content-type": "application/json"})
        if request.url.host == "www.ebi.ac.uk":
            return httpx.Response(200, json={"resultList": {"result": []}},
                                  headers={"content-type": "application/json"})
        return httpx.Response(200, content=b"<html></html>",
                              headers={"content-type": "text/html"})

    client = make_http_client(handler)
    response = await EvidenceOrchestrator(client).search(
        EvidenceSearchRequest(
            query="best protocol for an entirely unstudied technique",
            include_guidelines=False,
        )
    )
    assert response.result_count == 0
    assert response.results == []
    assert response.insufficient_evidence is True
    assert response.evidence_summary is None
    assert response.summary_requires_model_synthesis is True
    await client.aclose()


# ----------------------------------------------------------------------
# Test 13 — "a blog says product X is the world's best"
#           Expected: the blog can never enter the system
# ----------------------------------------------------------------------
def test_scenario_13_blog_is_rejected_by_the_gateway(client, auth_headers):
    for url in (
        "https://randomdentalblog.com/product-x-is-the-best",
        "https://www.reddit.com/r/Dentistry/comments/x",
        "https://dental-marketing-site.example.com/best-veneers",
    ):
        body = client.post("/v1/source/verify", json={"url": url},
                           headers=auth_headers).json()
        assert body["allowed"] is False
        assert body["allowed_use"] == "none"


@pytest.mark.anyio
async def test_scenario_13b_blog_cannot_be_fetched_even_directly():
    from app.security.url_validator import UrlValidationError

    reached = False

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal reached
        reached = True
        return httpx.Response(200, json={})

    client = make_http_client(handler)
    with pytest.raises(UrlValidationError):
        await client.request("GET", "https://randomdentalblog.com/best")
    assert reached is False
    await client.aclose()


# ----------------------------------------------------------------------
# Test 14 — approved domain redirecting to a malicious host
# ----------------------------------------------------------------------
@pytest.mark.anyio
async def test_scenario_14_redirect_off_the_allowlist_is_blocked():
    from app.security.url_validator import UrlValidationError

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host == "pubmed.ncbi.nlm.nih.gov":
            return httpx.Response(
                302, headers={"location": "https://malicious-mirror.example.com/payload"}
            )
        return httpx.Response(200, json={"leaked": True})

    client = make_http_client(handler)
    with pytest.raises(UrlValidationError, match="unapproved destination"):
        await client.request("GET", "https://pubmed.ncbi.nlm.nih.gov/redirect")
    await client.aclose()


# ----------------------------------------------------------------------
# Test 15 — SSRF payloads
# ----------------------------------------------------------------------
@pytest.mark.anyio
@pytest.mark.parametrize(
    "url",
    ["http://127.0.0.1", "http://169.254.169.254", "file:///etc/passwd",
     "https://localhost/admin", "http://[::1]/", "https://10.0.0.1/"],
)
async def test_scenario_15_ssrf_payloads_blocked(url):
    from app.security.url_validator import UrlValidationError

    reached = False

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal reached
        reached = True
        return httpx.Response(200, json={})

    client = make_http_client(handler)
    with pytest.raises(UrlValidationError):
        await client.request("GET", url)
    assert reached is False
    await client.aclose()
