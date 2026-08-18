"""Deduplication tests (PART 13, PART 47)."""

from __future__ import annotations

from app.evidence.deduplicator import deduplicate
from app.utils.normalize import normalize_title, title_similarity
from tests.conftest import make_record


def test_pmid_exact_match_merges():
    records = [
        make_record(provider="PubMed", pmid="34567890", doi=None),
        make_record(
            provider="Europe PMC",
            source_domain="europepmc.org",
            pmid="34567890",
            doi="10.1/x",
            pmcid="PMC1",
        ),
    ]
    result = deduplicate(records)
    assert len(result.records) == 1
    assert result.merged_count == 1
    merged = result.records[0]
    assert set(merged.providers) == {"PubMed", "Europe PMC"}
    # Gaps are filled from the second provider.
    assert merged.doi == "10.1/x"
    assert merged.pmcid == "PMC1"


def test_doi_match_merges_when_pmid_absent():
    records = [
        make_record(provider="PubMed", pmid=None, doi="10.1016/j.dental.2019.02.002"),
        make_record(
            provider="Europe PMC",
            source_domain="europepmc.org",
            pmid=None,
            doi="10.1016/j.dental.2019.02.002",
            title="Slightly different rendering of the title",
        ),
    ]
    result = deduplicate(records)
    assert len(result.records) == 1


def test_pmcid_match_merges():
    records = [
        make_record(provider="PubMed", pmid=None, doi=None, pmcid="PMC777"),
        make_record(
            provider="Europe PMC", source_domain="europepmc.org",
            pmid=None, doi=None, pmcid="PMC777", title="Other title entirely here",
        ),
    ]
    assert len(deduplicate(records).records) == 1


def test_title_similarity_merges_near_identical_titles():
    records = [
        make_record(
            provider="PubMed",
            pmid=None,
            doi=None,
            title="Immediate Dentin Sealing for Indirect Restorations: A Systematic Review.",
            publication_year=2023,
        ),
        make_record(
            provider="Europe PMC",
            source_domain="europepmc.org",
            pmid=None,
            doi=None,
            title="Immediate dentin sealing for indirect restorations - a systematic review",
            publication_year=2023,
        ),
    ]
    result = deduplicate(records)
    assert len(result.records) == 1
    assert result.merged_count == 1


def test_different_studies_are_not_merged():
    records = [
        make_record(pmid="1", doi=None, title="Ceramic veneers survival: a 10-year study"),
        make_record(
            pmid="2", doi=None, title="Zirconia crowns fracture: a laboratory analysis"
        ),
    ]
    result = deduplicate(records)
    assert len(result.records) == 2
    assert result.merged_count == 0


def test_conflicting_dois_prevent_title_merge():
    """Same-ish title but demonstrably different DOIs = different papers."""
    records = [
        make_record(pmid=None, doi="10.1/aaa", title="Veneer survival a systematic review"),
        make_record(
            pmid=None,
            doi="10.1/bbb",
            source_domain="europepmc.org",
            provider="Europe PMC",
            title="Veneer survival a systematic review",
        ),
    ]
    assert len(deduplicate(records).records) == 2


def test_distant_years_prevent_title_merge():
    records = [
        make_record(pmid=None, doi=None, title="Veneer survival systematic review",
                    publication_year=2005),
        make_record(pmid=None, doi=None, title="Veneer survival systematic review",
                    publication_year=2023, provider="Europe PMC",
                    source_domain="europepmc.org"),
    ]
    assert len(deduplicate(records).records) == 2


def test_short_titles_use_a_stricter_threshold():
    records = [
        make_record(pmid=None, doi=None, title="Dental implants"),
        make_record(pmid=None, doi=None, title="Dental implants survival",
                    provider="Europe PMC", source_domain="europepmc.org"),
    ]
    # Not merged: with few tokens, a single extra word is a large difference.
    assert len(deduplicate(records).records) == 2


def test_three_way_merge_via_transitive_identifiers():
    """PubMed(PMID) + EuropePMC(PMID+DOI) + Crossref(DOI) collapse to one."""
    records = [
        make_record(provider="PubMed", pmid="99", doi=None),
        make_record(
            provider="Europe PMC", source_domain="europepmc.org", pmid="99", doi="10.1/z"
        ),
        make_record(
            provider="Crossref", source_domain="crossref.org", pmid=None, doi="10.1/z",
            title="Completely different title text here",
        ),
    ]
    result = deduplicate(records)
    assert len(result.records) == 1
    assert result.merged_count == 2
    assert len(result.records[0].providers) == 3


def test_pubmed_is_the_primary_record():
    records = [
        make_record(provider="Europe PMC", source_domain="europepmc.org", pmid="5"),
        make_record(provider="PubMed", source_domain="pubmed.ncbi.nlm.nih.gov", pmid="5"),
    ]
    result = deduplicate(records)
    assert result.records[0].provider == "PubMed"
    assert result.records[0].source_domain == "pubmed.ncbi.nlm.nih.gov"


def test_merge_prefers_richer_abstract_and_author_list():
    records = [
        make_record(provider="PubMed", pmid="7", abstract="Short.", authors=["A B"]),
        make_record(
            provider="Europe PMC",
            source_domain="europepmc.org",
            pmid="7",
            abstract="A considerably longer and more complete abstract text " * 3,
            authors=["A B", "C D", "E F"],
        ),
    ]
    merged = deduplicate(records).records[0]
    assert len(merged.abstract) > 50
    assert len(merged.authors) == 3


def test_merge_never_overwrites_an_existing_identifier():
    records = [
        make_record(provider="PubMed", pmid="7", doi="10.1/original"),
        make_record(
            provider="Europe PMC", source_domain="europepmc.org", pmid="7",
            doi="10.1/different",
        ),
    ]
    merged = deduplicate(records).records[0]
    assert merged.doi == "10.1/original"


def test_merge_unions_integrity_flags():
    records = [
        make_record(provider="PubMed", pmid="7"),
        make_record(
            provider="Europe PMC", source_domain="europepmc.org", pmid="7",
            retraction_warning=True, integrity_notes=["Retracted upstream"],
        ),
    ]
    merged = deduplicate(records).records[0]
    assert merged.retraction_warning is True
    assert "Retracted upstream" in merged.integrity_notes


def test_merge_keeps_alternate_urls_without_changing_canonical():
    records = [
        make_record(provider="PubMed", pmid="7", url="https://pubmed.ncbi.nlm.nih.gov/7/"),
        make_record(
            provider="Europe PMC", source_domain="europepmc.org", pmid="7",
            url="https://europepmc.org/article/MED/7",
        ),
    ]
    merged = deduplicate(records).records[0]
    assert merged.url == "https://pubmed.ncbi.nlm.nih.gov/7/"
    assert "https://europepmc.org/article/MED/7" in merged.extra["alternate_urls"]


def test_empty_input():
    result = deduplicate([])
    assert result.records == []
    assert result.merged_count == 0


# ----------------------------------------------------------------------
# Title normalisation helpers
# ----------------------------------------------------------------------
def test_normalize_title_strips_punctuation_and_stopwords():
    assert normalize_title("The Effect of A Thing: On Teeth!") == "effect thing teeth"


def test_normalize_title_handles_accents_and_markup():
    assert normalize_title("Évaluation <i>in vivo</i>") == "evaluation vivo"


def test_title_similarity_bounds():
    assert title_similarity("veneer survival", "veneer survival") == 1.0
    assert title_similarity("veneer survival", "") == 0.0
    assert 0.0 < title_similarity("veneer survival study", "veneer survival") < 1.0
