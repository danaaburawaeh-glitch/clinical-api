"""PubMed, Europe PMC and Crossref engine tests (PART 7, 8, 9, 47)."""

from __future__ import annotations

import httpx
import pytest

from app.engines.base import EngineError
from app.engines.crossref import CrossrefEngine
from app.engines.europe_pmc import EuropePmcEngine
from app.engines.pubmed import PubMedEngine
from app.utils.normalize import normalize_doi, normalize_pmcid, normalize_pmid
from tests.conftest import make_http_client


# ======================================================================
# PubMed XML parsing
# ======================================================================
def test_parse_efetch_extracts_full_metadata(fixture_text):
    records = PubMedEngine.parse_efetch_xml(fixture_text("pubmed_efetch.xml"))
    assert len(records) == 3

    first = records[0]
    assert first.pmid == "34567890"
    assert first.doi == "10.1016/j.prosdent.2023.01.001"
    assert first.pmcid == "PMC9999999"
    assert first.publication_year == 2023
    assert first.journal == "The Journal of prosthetic dentistry"
    assert first.authors == ["Magne P", "Nunes L"]
    assert first.language == "eng"
    assert "Systematic Review" in first.publication_types
    assert "Meta-Analysis" in first.publication_types
    assert "Dental Bonding" in first.mesh_terms
    assert "adhesion" in first.keywords
    # Structured abstract sections keep their labels.
    assert "STATEMENT OF PROBLEM:" in (first.abstract or "")
    assert "RESULTS:" in (first.abstract or "")
    assert first.url == "https://pubmed.ncbi.nlm.nih.gov/34567890/"
    assert first.source_domain == "pubmed.ncbi.nlm.nih.gov"


def test_parse_efetch_handles_medline_date():
    """A MedlineDate like '2019 Spring' still yields a year."""
    records = PubMedEngine.parse_efetch_xml(
        (
            "<PubmedArticleSet><PubmedArticle><MedlineCitation>"
            "<PMID>1</PMID><Article><Journal><JournalIssue><PubDate>"
            "<MedlineDate>2019 Spring</MedlineDate>"
            "</PubDate></JournalIssue><Title>J</Title></Journal>"
            "<ArticleTitle>T</ArticleTitle></Article></MedlineCitation>"
            "</PubmedArticle></PubmedArticleSet>"
        )
    )
    assert records[0].publication_year == 2019


def test_parse_efetch_returns_none_for_absent_doi(fixture_text):
    records = PubMedEngine.parse_efetch_xml(fixture_text("pubmed_efetch.xml"))
    retracted = next(r for r in records if r.pmid == "33333333")
    # No DOI in the fixture -> None, never a fabricated identifier.
    assert retracted.doi is None
    assert retracted.pmcid is None


def test_parse_efetch_captures_retraction_linkage(fixture_text):
    records = PubMedEngine.parse_efetch_xml(fixture_text("pubmed_efetch.xml"))
    retracted = next(r for r in records if r.pmid == "33333333")
    assert "Retracted Publication" in retracted.publication_types
    assert "RetractionIn" in retracted.extra["comments_corrections"]


def test_parse_efetch_survives_malformed_article():
    """One bad article must not destroy the whole batch."""
    xml = (
        "<PubmedArticleSet>"
        "<PubmedArticle><MedlineCitation></MedlineCitation></PubmedArticle>"
        "<PubmedArticle><MedlineCitation><PMID>7</PMID>"
        "<Article><ArticleTitle>Good one</ArticleTitle></Article>"
        "</MedlineCitation></PubmedArticle>"
        "</PubmedArticleSet>"
    )
    records = PubMedEngine.parse_efetch_xml(xml)
    assert len(records) == 1
    assert records[0].title == "Good one"


def test_parse_efetch_rejects_malformed_xml():
    with pytest.raises(EngineError):
        PubMedEngine.parse_efetch_xml("<PubmedArticleSet><broken>")


def test_parse_efetch_empty_input():
    assert PubMedEngine.parse_efetch_xml("") == []


def test_title_with_inline_markup_is_flattened():
    xml = (
        "<PubmedArticleSet><PubmedArticle><MedlineCitation><PMID>9</PMID>"
        "<Article><ArticleTitle>Effect of <i>S. mutans</i> on enamel</ArticleTitle>"
        "</Article></MedlineCitation></PubmedArticle></PubmedArticleSet>"
    )
    records = PubMedEngine.parse_efetch_xml(xml)
    assert records[0].title == "Effect of S. mutans on enamel"


# ======================================================================
# PubMed query construction
# ======================================================================
@pytest.mark.anyio
async def test_build_query_includes_dental_scope_and_filters():
    client = make_http_client(lambda r: httpx.Response(200, json={}))
    engine = PubMedEngine(client)
    query = engine.build_query(
        "immediate dentin sealing",
        date_from=2015,
        date_to=2024,
        study_designs=["systematic_review", "randomized_controlled_trial"],
    )
    assert "(immediate dentin sealing)" in query
    assert "Dentistry" in query
    assert "Systematic Review" in query
    assert "Randomized Controlled Trial" in query
    assert '"2015"[Date - Publication] : "2024"[Date - Publication]' in query
    await client.aclose()


@pytest.mark.anyio
async def test_build_query_rejects_empty_term():
    client = make_http_client(lambda r: httpx.Response(200, json={}))
    engine = PubMedEngine(client)
    with pytest.raises(EngineError):
        engine.build_query("   ")
    await client.aclose()


@pytest.mark.anyio
async def test_full_search_flow(fixture_text):
    """ESearch -> EFetch, exercising real request building."""
    seen_params: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen_params.append(dict(request.url.params))
        if "esearch" in request.url.path:
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

    client = make_http_client(handler)
    engine = PubMedEngine(client)
    records = await engine.search("immediate dentin sealing", max_results=3)

    assert len(records) == 3
    assert seen_params[0]["db"] == "pubmed"
    assert seen_params[1]["id"] == "34567890,22222222,33333333"
    await client.aclose()


@pytest.mark.anyio
async def test_esearch_error_is_surfaced():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={"esearchresult": {"ERROR": "Invalid db name"}},
            headers={"content-type": "application/json"},
        )

    client = make_http_client(handler)
    engine = PubMedEngine(client)
    with pytest.raises(EngineError, match="ESearch error"):
        await engine.esearch("x")
    await client.aclose()


@pytest.mark.anyio
async def test_pubmed_timeout_becomes_engine_error():
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("slow", request=request)

    client = make_http_client(handler)
    engine = PubMedEngine(client)
    with pytest.raises(EngineError, match="timeout"):
        await engine.esearch("x")
    await client.aclose()


@pytest.mark.anyio
async def test_empty_result_set_returns_empty_list():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={"esearchresult": {"idlist": []}},
            headers={"content-type": "application/json"},
        )

    client = make_http_client(handler)
    engine = PubMedEngine(client)
    assert await engine.search("nonexistent topic") == []
    await client.aclose()


# ======================================================================
# Europe PMC
# ======================================================================
EUROPEPMC_PAYLOAD = {
    "resultList": {
        "result": [
            {
                "id": "34567890",
                "source": "MED",
                "pmid": "34567890",
                "pmcid": "PMC9999999",
                "doi": "10.1016/J.PROSDENT.2023.01.001",
                "title": "Immediate dentin sealing for indirect restorations: a systematic review and meta-analysis",
                "authorString": "Magne P, Nunes L.",
                "journalInfo": {"journal": {"title": "The Journal of prosthetic dentistry"}},
                "pubYear": "2023",
                "abstractText": "<p>Immediate dentin sealing was associated with higher retention.</p>",
                "pubTypeList": {"pubType": ["Journal Article", "systematic review"]},
                "isOpenAccess": "Y",
                "language": "eng",
                "citedByCount": 12,
                "meshHeadingList": {"meshHeading": [{"descriptorName": "Dentin"}]},
            },
            {
                "id": "44444444",
                "source": "MED",
                "pmid": "44444444",
                "title": "A prospective cohort of ceramic veneers",
                "pubYear": "2021",
                "pubTypeList": {"pubType": ["Journal Article"]},
            },
        ]
    }
}


def test_europepmc_parsing():
    records = EuropePmcEngine.parse_search_payload(EUROPEPMC_PAYLOAD)
    assert len(records) == 2

    first = records[0]
    assert first.pmid == "34567890"
    assert first.pmcid == "PMC9999999"
    # DOIs are normalised to lower case for reliable dedup matching.
    assert first.doi == "10.1016/j.prosdent.2023.01.001"
    assert first.publication_year == 2023
    assert first.open_access is True
    assert first.authors == ["Magne P", "Nunes L"]
    # Europe PMC's lower-case pubTypes are mapped to the NCBI vocabulary.
    assert "Systematic Review" in first.publication_types
    # Abstract markup is stripped.
    assert "<p>" not in (first.abstract or "")
    # The URL is built by us, so it is always on an allowlisted domain.
    assert first.url == "https://europepmc.org/article/MED/34567890"


def test_europepmc_missing_fields_stay_none():
    records = EuropePmcEngine.parse_search_payload(EUROPEPMC_PAYLOAD)
    second = records[1]
    assert second.doi is None
    assert second.pmcid is None
    assert second.abstract is None
    assert second.open_access is None


def test_europepmc_handles_garbage_payload():
    assert EuropePmcEngine.parse_search_payload(None) == []
    assert EuropePmcEngine.parse_search_payload({"resultList": {"result": "nope"}}) == []
    assert EuropePmcEngine.parse_search_payload({}) == []


@pytest.mark.anyio
async def test_europepmc_query_excludes_preprints():
    client = make_http_client(lambda r: httpx.Response(200, json={}))
    engine = EuropePmcEngine(client)
    query = engine.build_query("veneers", date_from=2020, date_to=2024)
    assert "NOT (SRC:PPR)" in query
    assert "FIRST_PDATE:[2020-01-01 TO 2024-12-31]" in query
    await client.aclose()


@pytest.mark.anyio
async def test_europepmc_search_uses_official_host():
    hosts: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        hosts.append(request.url.host)
        return httpx.Response(
            200, json=EUROPEPMC_PAYLOAD, headers={"content-type": "application/json"}
        )

    client = make_http_client(handler)
    engine = EuropePmcEngine(client)
    records = await engine.search("veneers", max_results=2)
    assert hosts == ["www.ebi.ac.uk"]
    assert len(records) == 2
    await client.aclose()


# ======================================================================
# Crossref
# ======================================================================
def test_crossref_parse_work_and_consistency():
    message = {
        "DOI": "10.1016/J.PROSDENT.2023.01.001",
        "title": ["Immediate dentin sealing for indirect restorations: a systematic review and meta-analysis"],
        "container-title": ["Journal of Prosthetic Dentistry"],
        "issued": {"date-parts": [[2023, 4]]},
        "author": [{"family": "Magne", "given": "Pascal"}],
        "type": "journal-article",
        "publisher": "Elsevier BV",
        "ISSN": ["0022-3913"],
    }
    result = CrossrefEngine.parse_work(
        message,
        expected_title="Immediate dentin sealing for indirect restorations: a systematic review and meta-analysis",
        expected_year=2023,
    )
    assert result.found is True
    assert result.doi == "10.1016/j.prosdent.2023.01.001"
    assert result.publication_year == 2023
    assert result.title_matches is True
    assert result.year_matches is True
    assert result.metadata_consistent is True
    assert result.authors == ["Magne Pascal"]


def test_crossref_detects_metadata_mismatch():
    result = CrossrefEngine.parse_work(
        {
            "DOI": "10.1/x",
            "title": ["Something completely different about orthodontics"],
            "issued": {"date-parts": [[2005]]},
        },
        expected_title="Immediate dentin sealing systematic review",
        expected_year=2023,
    )
    assert result.title_matches is False
    assert result.year_matches is False
    assert result.metadata_consistent is False


def test_crossref_year_drift_of_one_is_tolerated():
    result = CrossrefEngine.parse_work(
        {"DOI": "10.1/x", "title": ["A"], "issued": {"date-parts": [[2023]]}},
        expected_year=2024,
    )
    assert result.year_matches is True


def test_crossref_detects_retraction_relation():
    result = CrossrefEngine.parse_work(
        {
            "DOI": "10.1/retracted",
            "title": ["A study"],
            "update-to": [{"type": "retraction", "label": "Retraction"}],
        }
    )
    assert result.integrity_status == "retracted"
    assert result.is_retracted is True


def test_crossref_detects_correction_relation():
    result = CrossrefEngine.parse_work(
        {
            "DOI": "10.1/corrected",
            "title": ["A study"],
            "update-to": [{"type": "corrigendum", "label": "Corrigendum"}],
        }
    )
    assert result.integrity_status == "correction"
    assert result.is_retracted is False


@pytest.mark.anyio
async def test_crossref_invalid_doi_returns_not_found():
    client = make_http_client(lambda r: httpx.Response(200, json={}))
    engine = CrossrefEngine(client)
    result = await engine.validate_doi("this is not a doi")
    assert result.found is False
    await client.aclose()


# ======================================================================
# Identifier normalisation
# ======================================================================
@pytest.mark.parametrize(
    "raw,expected",
    [
        ("10.1016/j.dental.2019.02.002", "10.1016/j.dental.2019.02.002"),
        ("https://doi.org/10.1016/J.DENTAL.2019.02.002", "10.1016/j.dental.2019.02.002"),
        ("doi:10.1016/j.dental.2019.02.002", "10.1016/j.dental.2019.02.002"),
        ("http://dx.doi.org/10.1016/j.x", "10.1016/j.x"),
        ("  10.1016/j.x.  ", "10.1016/j.x"),
        ("not-a-doi", None),
        ("10.1016", None),
        ("", None),
        (None, None),
    ],
)
def test_normalize_doi(raw, expected):
    assert normalize_doi(raw) == expected


@pytest.mark.parametrize(
    "raw,expected",
    [("34567890", "34567890"), ("PMID: 34567890", "34567890"), ("0", None), ("", None)],
)
def test_normalize_pmid(raw, expected):
    assert normalize_pmid(raw) == expected


@pytest.mark.parametrize(
    "raw,expected",
    [("PMC123", "PMC123"), ("pmc123", "PMC123"), ("123", "PMC123"), ("", None)],
)
def test_normalize_pmcid(raw, expected):
    assert normalize_pmcid(raw) == expected
