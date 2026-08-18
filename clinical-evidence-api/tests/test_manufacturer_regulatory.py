"""Manufacturer, regulatory and guideline engine tests
(PART 25-32, 60, 69, 86)."""

from __future__ import annotations

import httpx
import pytest

from app.engines.guideline_search import GuidelineSearchEngine
from app.engines.manufacturer_search import ManufacturerSearchEngine
from app.engines.openfda import OpenFdaEngine
from app.engines.sfda import SfdaEngine
from tests.conftest import make_http_client

IVOCLAR_INDEX = b"""
<html><body>
  <a href="/en/downloads/monobond-etch-and-prime-instructions-for-use.pdf">
     Monobond Etch &amp; Prime Instructions for Use
  </a>
  <a href="/en/p/ips-emax">IPS e.max product information</a>
  <a href="https://randomdentalblog.com/review">Independent review (off-domain)</a>
  <a href="https://distributor-shop.example.com/buy">Buy now</a>
</body></html>
"""

IVOCLAR_DOC = b"""
<html><head><title>Monobond Etch &amp; Prime - Instructions for Use</title></head>
<body>
  <p>Instructions for use. Apply Monobond Etch &amp; Prime for 20 seconds,
     then rinse thoroughly. Rev. 3.2 &mdash; 2024-05-14.</p>
  <p>Contraindications: known allergy to any of the ingredients.</p>
</body></html>
"""


# ======================================================================
# Manufacturer engine (PART 25, 26, 60, 69)
# ======================================================================
@pytest.mark.anyio
async def test_manufacturer_search_stays_on_official_domain():
    visited: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        visited.append(request.url.host)
        if request.url.path.endswith(".pdf"):
            return httpx.Response(
                200, content=IVOCLAR_DOC, headers={"content-type": "text/html"}
            )
        return httpx.Response(
            200, content=IVOCLAR_INDEX, headers={"content-type": "text/html"}
        )

    client = make_http_client(handler)
    engine = ManufacturerSearchEngine(client)
    outcome = await engine.search(
        "Monobond Etch and Prime instructions",
        manufacturer="Ivoclar",
        document_type="IFU",
        max_results=2,
    )

    assert outcome.records
    # Every host touched must be the allowlisted manufacturer domain.
    assert set(visited) <= {"www.ivoclar.com", "ivoclar.com"}
    assert "randomdentalblog.com" not in visited
    assert "distributor-shop.example.com" not in visited
    for record in outcome.records:
        assert record.source_domain == "ivoclar.com"
    await client.aclose()


@pytest.mark.anyio
async def test_manufacturer_search_refuses_unknown_manufacturer():
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(str(request.url))
        return httpx.Response(200, content=b"<html></html>",
                              headers={"content-type": "text/html"})

    client = make_http_client(handler)
    engine = ManufacturerSearchEngine(client)
    outcome = await engine.search("IFU", manufacturer="Totally Unknown Dental Co")

    assert outcome.records == []
    assert calls == [], "no request may be made for a non-allowlisted manufacturer"
    assert any("not on the allowlist" in w for w in outcome.warnings)
    await client.aclose()


@pytest.mark.anyio
async def test_manufacturer_search_resolves_brand_to_owner():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=IVOCLAR_INDEX,
                              headers={"content-type": "text/html"})

    client = make_http_client(handler)
    engine = ManufacturerSearchEngine(client)
    # "IPS e.max" names no manufacturer explicitly; the brand map resolves it.
    outcome = await engine.search("IPS e.max etching instructions", document_type="IFU")
    assert outcome.resolved_manufacturer == "ivoclar"
    await client.aclose()


@pytest.mark.anyio
async def test_manufacturer_version_is_read_not_invented():
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith(".pdf"):
            return httpx.Response(200, content=IVOCLAR_DOC,
                                  headers={"content-type": "text/html"})
        return httpx.Response(200, content=IVOCLAR_INDEX,
                              headers={"content-type": "text/html"})

    client = make_http_client(handler)
    engine = ManufacturerSearchEngine(client)
    outcome = await engine.search(
        "Monobond Etch and Prime", manufacturer="Ivoclar", document_type="IFU",
        max_results=1,
    )
    record = outcome.records[0]
    # The fixture literally contains "Rev. 3.2" and "2024-05-14".
    assert record.document_version == "3.2"
    assert record.document_date == "2024-05-14"
    await client.aclose()


@pytest.mark.anyio
async def test_manufacturer_version_absent_means_unverified():
    plain = b"<html><head><title>Product page</title></head><body>Some text.</body></html>"

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith(".pdf"):
            return httpx.Response(200, content=plain,
                                  headers={"content-type": "text/html"})
        return httpx.Response(200, content=IVOCLAR_INDEX,
                              headers={"content-type": "text/html"})

    client = make_http_client(handler)
    engine = ManufacturerSearchEngine(client)
    outcome = await engine.search("Monobond", manufacturer="Ivoclar", max_results=1)
    record = outcome.records[0]
    assert record.document_version is None
    assert record.current_document_verified is False
    await client.aclose()


@pytest.mark.anyio
async def test_manufacturer_endpoint_marks_everything_manufacturer_information(
    client, auth_headers, monkeypatch
):
    """End-to-end: the API can only ever emit MANUFACTURER_INFORMATION here."""
    from app.engines.base import RawRecord
    from app.engines.manufacturer_search import ManufacturerSearchOutcome

    async def fake_search(self, query, **kwargs):
        return ManufacturerSearchOutcome(
            records=[
                RawRecord(
                    provider="Manufacturer (official domain)",
                    source_domain="ivoclar.com",
                    url="https://www.ivoclar.com/en/ifu.pdf",
                    title="Clinically proven superior bonding performance",
                    manufacturer="Ivoclar",
                    document_type="IFU",
                    publication_types=["Randomized Controlled Trial"],
                )
            ],
            warnings=[],
            resolved_manufacturer="ivoclar",
            searched_domains=["ivoclar.com"],
        )

    monkeypatch.setattr(ManufacturerSearchEngine, "search", fake_search)

    response = client.post(
        "/v1/manufacturer/search",
        json={"query": "Monobond IFU", "manufacturer": "Ivoclar"},
        headers=auth_headers,
    )
    assert response.status_code == 200
    body = response.json()
    assert body["results"][0]["evidence_level"] == "MANUFACTURER_INFORMATION"
    assert body["results"][0]["clinical_translation"] == "not_applicable"
    assert any("MANUFACTURER INFORMATION" in w for w in body["warnings"])
    assert any("cannot establish" in w for w in body["warnings"])


# ======================================================================
# openFDA (PART 29, 31)
# ======================================================================
K510_PAYLOAD = {
    "results": [
        {
            "k_number": "K251002",
            "device_name": "Videa Dental AI Caries Assist",
            "applicant": "VideaHealth Inc",
            "decision_description": "Substantially Equivalent",
            "decision_date": "2025-06-11",
            "product_code": "QPF",
            "clearance_type": "Traditional",
        }
    ]
}


def test_identifier_detection():
    assert OpenFdaEngine.detect_identifier("Is K251002 cleared?") == ("K251002", "510k")
    assert OpenFdaEngine.detect_identifier("P123456") == ("P123456", "pma")
    assert OpenFdaEngine.detect_identifier("DEN200001") == ("DEN200001", "denovo")
    assert OpenFdaEngine.detect_identifier("no identifier here") is None


@pytest.mark.anyio
async def test_exact_identifier_lookup_is_tried_first():
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request.url.params.get("search", ""))
        return httpx.Response(200, json=K510_PAYLOAD,
                              headers={"content-type": "application/json"})

    client = make_http_client(handler)
    engine = OpenFdaEngine(client)
    records = await engine.search("Is Videa Dental AI K251002 FDA cleared?")

    assert seen[0] == 'k_number:"K251002"'
    assert len(records) == 1
    record = records[0]
    assert record.regulatory_identifier == "K251002"
    assert record.regulatory_authority == "FDA"
    assert "510(k)" in record.regulatory_pathway
    assert "cleared" in record.regulatory_pathway
    assert record.decision_date == "2025-06-11"
    await client.aclose()


@pytest.mark.anyio
async def test_510k_record_states_clearance_is_not_approval():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=K510_PAYLOAD,
                              headers={"content-type": "application/json"})

    client = make_http_client(handler)
    engine = OpenFdaEngine(client)
    record = (await engine.lookup_identifier("K251002"))[0]
    limitation = record.limitations or ""
    assert "NOT an FDA approval" in limitation
    assert "not evidence of clinical superiority" in limitation
    await client.aclose()


@pytest.mark.anyio
async def test_openfda_404_means_no_results_not_an_error():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(404, json={"error": {"code": "NOT_FOUND"}},
                              headers={"content-type": "application/json"})

    client = make_http_client(handler)
    engine = OpenFdaEngine(client)
    assert await engine.search("nonexistent device") == []
    await client.aclose()


@pytest.mark.anyio
async def test_openfda_only_talks_to_the_official_api_host():
    hosts: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        hosts.append(request.url.host)
        return httpx.Response(200, json={"results": []},
                              headers={"content-type": "application/json"})

    client = make_http_client(handler)
    engine = OpenFdaEngine(client)
    await engine.search("dental scanner")
    assert set(hosts) == {"api.fda.gov"}
    await client.aclose()


def test_maude_records_carry_a_reporting_bias_limitation():
    engine_records = OpenFdaEngine._parse_event(
        OpenFdaEngine(make_http_client(lambda r: httpx.Response(200, json={}))),
        {
            "results": [
                {
                    "report_number": "1234",
                    "event_type": "Malfunction",
                    "date_received": "2024-01-01",
                    "device": [{"brand_name": "Some Scanner",
                                "manufacturer_d_name": "Some Co"}],
                }
            ]
        },
    )
    assert "reporting bias" in (engine_records[0].limitations or "")


# ======================================================================
# Regulatory endpoint behaviour (PART 31)
# ======================================================================
def test_regulatory_response_separates_status_from_effectiveness(
    client, auth_headers, monkeypatch
):
    from app.engines.base import RawRecord

    async def fake_search(self, query, **kwargs):
        return [
            RawRecord(
                provider="openFDA",
                source_domain="api.fda.gov",
                url="https://accessdata.fda.gov/scripts/cdrh/cfdocs/cfpmn/pmn.cfm?ID=K251002",
                title="Videa Dental AI — FDA 510(k) K251002",
                regulatory_identifier="K251002",
                regulatory_authority="FDA",
                regulatory_pathway="510(k) premarket notification (cleared)",
                regulatory_status="Substantially Equivalent",
                decision_date="2025-06-11",
            )
        ]

    monkeypatch.setattr(OpenFdaEngine, "search", fake_search)

    response = client.post(
        "/v1/regulatory/search",
        json={"query": "Videa Dental AI K251002", "authority": "FDA"},
        headers=auth_headers,
    )
    assert response.status_code == 200
    body = response.json()

    result = body["results"][0]
    assert result["evidence_level"] == "REGULATORY"
    assert result["regulatory_identifier"] == "K251002"
    assert result["retrieved_at"]

    joined = " ".join(body["warnings"])
    assert "not a measure of clinical effectiveness" in joined
    assert "never establishes superiority" in joined
    assert "must not be used interchangeably" in joined


def test_regulatory_empty_result_does_not_imply_unregistered(
    client, auth_headers, monkeypatch
):
    async def fake_search(self, query, **kwargs):
        return []

    monkeypatch.setattr(OpenFdaEngine, "search", fake_search)
    response = client.post(
        "/v1/regulatory/search",
        json={"query": "an unknown device", "authority": "FDA"},
        headers=auth_headers,
    )
    body = response.json()
    assert body["insufficient_evidence"] is True
    assert any("NOT evidence that a product lacks" in w for w in body["warnings"])


# ======================================================================
# SFDA (PART 30)
# ======================================================================
@pytest.mark.anyio
async def test_sfda_only_touches_the_official_domain():
    hosts: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        hosts.append(request.url.host)
        return httpx.Response(
            200,
            content=b'<html><body><a href="/en/recalls/device-x">Device X recall</a></body></html>',
            headers={"content-type": "text/html"},
        )

    client = make_http_client(handler)
    engine = SfdaEngine(client)
    outcome = await engine.search("device x", regulatory_type="recall", max_results=2)

    assert set(hosts) <= {"www.sfda.gov.sa", "sfda.gov.sa"}
    for record in outcome.records:
        assert record.source_domain == "sfda.gov.sa"
        # No status is asserted from a page scrape.
        assert record.regulatory_status is None
        assert record.regulatory_identifier is None
    await client.aclose()


@pytest.mark.anyio
async def test_sfda_always_warns_about_the_api_limitation():
    client = make_http_client(
        lambda r: httpx.Response(200, content=b"<html></html>",
                                 headers={"content-type": "text/html"})
    )
    engine = SfdaEngine(client)
    outcome = await engine.search("anything")
    assert any("does not currently expose a public machine-readable" in w
               for w in outcome.warnings)
    assert any("confirmed directly with SFDA" in w for w in outcome.warnings)
    await client.aclose()


# ======================================================================
# Guideline engine (PART 32)
# ======================================================================
ADA_PAGE = b"""
<html><body>
  <a href="/en/resources/clinical-practice-guideline-caries-2024">
     Clinical Practice Guideline on Caries Management (2024)</a>
  <a href="/en/about/position-statements/fluoride">Position statement on fluoride</a>
  <a href="/en/about/consensus-report-2019">Consensus report on adhesives</a>
  <a href="https://someblog.example.com/opinion">Blog opinion</a>
</body></html>
"""


@pytest.mark.anyio
async def test_guideline_document_types_are_distinguished():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=ADA_PAGE,
                              headers={"content-type": "text/html"})

    client = make_http_client(handler)
    engine = GuidelineSearchEngine(client)
    outcome = await engine.search(
        "caries management", specialty="preventive_dentistry", max_results=5,
        max_organisations=1,
    )

    types = {r.guideline_type for r in outcome.records}
    assert "clinical_practice_guideline" in types
    # A position statement must NOT be labelled a guideline.
    assert "position_statement" in types
    assert "consensus_report" in types

    guideline = next(
        r for r in outcome.records if r.guideline_type == "clinical_practice_guideline"
    )
    assert guideline.publication_year == 2024
    assert "Practice Guideline" in guideline.publication_types

    position = next(r for r in outcome.records if r.guideline_type == "position_statement")
    assert position.publication_types == []
    await client.aclose()


@pytest.mark.anyio
async def test_guideline_engine_drops_off_domain_links():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=ADA_PAGE,
                              headers={"content-type": "text/html"})

    client = make_http_client(handler)
    engine = GuidelineSearchEngine(client)
    outcome = await engine.search("caries", specialty="preventive_dentistry",
                                  max_organisations=1)
    for record in outcome.records:
        assert "someblog.example.com" not in (record.url or "")
    await client.aclose()


@pytest.mark.anyio
async def test_guideline_specialty_routing_picks_the_right_bodies():
    from app.engines.guideline_search import guideline_organisations

    endo = {e.domain for e in guideline_organisations("endodontics")}
    perio = {e.domain for e in guideline_organisations("periodontology")}
    assert "aae.org" in endo
    assert "perio.org" in perio or "efp.org" in perio


# ======================================================================
# Outage must never be reported as absence (PART 57, PART 40)
# ======================================================================
@pytest.mark.anyio
async def test_openfda_total_outage_raises_instead_of_returning_empty():
    """A network failure must not look like 'this device has no clearance'."""
    from app.engines.base import EngineError

    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("simulated network outage", request=request)

    client = make_http_client(handler)
    with pytest.raises(EngineError, match="no regulatory status could be checked"):
        await OpenFdaEngine(client).search("some dental device")
    await client.aclose()


@pytest.mark.anyio
async def test_openfda_genuine_404_still_returns_empty():
    """404 means 'no such record', which IS a legitimate empty result."""
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(404, json={"error": {"code": "NOT_FOUND"}},
                              headers={"content-type": "application/json"})

    client = make_http_client(handler)
    assert await OpenFdaEngine(client).search("nonexistent device") == []
    await client.aclose()


@pytest.mark.anyio
async def test_sfda_outage_is_reported_as_connectivity_failure():
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("simulated outage", request=request)

    client = make_http_client(handler)
    outcome = await SfdaEngine(client).search("device x")
    assert outcome.records == []
    assert any("could not be reached" in w for w in outcome.warnings)
    assert any("not an absence of registration" in w for w in outcome.warnings)
    await client.aclose()


@pytest.mark.anyio
async def test_manufacturer_outage_is_distinguished_from_missing_document():
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("simulated outage", request=request)

    client = make_http_client(handler)
    outcome = await ManufacturerSearchEngine(client).search(
        "Monobond IFU", manufacturer="Ivoclar"
    )
    assert outcome.records == []
    assert any("could not be reached" in w for w in outcome.warnings)
    assert any("not evidence that the document does not exist" in w
               for w in outcome.warnings)
    await client.aclose()


@pytest.mark.anyio
async def test_manufacturer_empty_site_says_document_not_found_not_outage():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=b"<html><body></body></html>",
                              headers={"content-type": "text/html"})

    client = make_http_client(handler)
    outcome = await ManufacturerSearchEngine(client).search(
        "Monobond IFU", manufacturer="Ivoclar"
    )
    assert outcome.records == []
    joined = " ".join(outcome.warnings)
    assert "could not be reached" not in joined
    assert "does not mean the document does not exist" in joined
    await client.aclose()


@pytest.mark.anyio
async def test_guideline_outage_is_reported_as_connectivity_failure():
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("simulated outage", request=request)

    client = make_http_client(handler)
    outcome = await GuidelineSearchEngine(client).search(
        "caries", specialty="preventive_dentistry", max_organisations=1
    )
    assert outcome.records == []
    assert any("connectivity failure" in w for w in outcome.warnings)
    await client.aclose()


def test_regulatory_endpoint_reports_a_failed_provider_on_outage(
    client, auth_headers, monkeypatch
):
    """End-to-end: an outage shows in failed_sources, not as a clean zero."""
    from app.engines.base import EngineError

    async def failing(self, query, **kwargs):
        raise EngineError("openFDA", "all openFDA endpoints failed", retryable=True)

    monkeypatch.setattr(OpenFdaEngine, "search", failing)
    body = client.post(
        "/v1/regulatory/search",
        json={"query": "device", "authority": "FDA"},
        headers=auth_headers,
    ).json()

    assert "openFDA" in [f["provider"] for f in body["failed_sources"]]
    assert body["failed_sources"][0]["retryable"] is True
