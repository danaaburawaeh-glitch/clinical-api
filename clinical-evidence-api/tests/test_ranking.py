"""Ranking, query-expansion and cache tests (PART 16, 33, 34, 76)."""

from __future__ import annotations

import pytest

from app.evidence.classifier import classify_records
from app.evidence.query_expander import get_query_expander
from app.evidence.ranker import EvidenceRanker, RankingContext, describe_ranking, rank_records
from app.evidence.retraction_check import check_records
from app.evidence.rules import get_evidence_rules, get_journal_registry
from tests.conftest import make_record

CURRENT_YEAR = 2026


def _ctx(**kwargs) -> RankingContext:
    kwargs.setdefault("current_year", CURRENT_YEAR)
    return RankingContext(**kwargs)


def _prepared(records):
    classify_records(records)
    check_records(records)
    return records


# ======================================================================
# Design hierarchy
# ======================================================================
def test_stronger_designs_rank_above_weaker_ones():
    records = _prepared(
        [
            make_record(pmid="1", title="A case report of veneer fracture",
                        publication_types=["Case Reports"], publication_year=2025),
            make_record(pmid="2", title="Systematic review of veneer survival",
                        publication_types=["Systematic Review"], publication_year=2025),
            make_record(pmid="3", title="Randomized controlled trial of veneer cements",
                        publication_types=["Randomized Controlled Trial"], publication_year=2025),
        ]
    )
    ranked = rank_records(records, _ctx(query="veneer survival"))
    order = [r.pmid for r in ranked]
    assert order.index("2") < order.index("3") < order.index("1")


def test_laboratory_study_ranks_below_clinical_evidence():
    records = _prepared(
        [
            make_record(pmid="lab", title="Shear bond strength after thermocycling",
                        publication_year=2026),
            make_record(pmid="clin", title="Prospective cohort of bonded restorations",
                        publication_types=["Observational Study"], publication_year=2018),
        ]
    )
    ranked = rank_records(records, _ctx(query="bonded restorations"))
    assert ranked[0].pmid == "clin"


def test_manufacturer_record_cannot_outrank_clinical_evidence():
    records = _prepared(
        [
            make_record(
                pmid=None, source_domain="ivoclar.com",
                title="Superior clinical performance of our cement",
                publication_year=2026, journal=None,
            ),
            make_record(
                pmid="1", title="Systematic review of resin cements",
                publication_types=["Systematic Review"], publication_year=2019,
                journal="Dental Materials",
            ),
        ]
    )
    ranked = rank_records(records, _ctx(query="resin cement"))
    assert ranked[0].evidence_class == "A2"


# ======================================================================
# Retraction penalty
# ======================================================================
def test_retracted_record_sinks_to_the_bottom():
    records = _prepared(
        [
            make_record(pmid="r", title="RETRACTED: A trial of veneers",
                        publication_types=["Randomized Controlled Trial",
                                           "Retracted Publication"],
                        publication_year=2026),
            make_record(pmid="ok", title="A case report of veneers",
                        publication_types=["Case Reports"], publication_year=2005),
        ]
    )
    ranked = rank_records(records, _ctx(query="veneers"))
    assert ranked[-1].pmid == "r"
    assert ranked[-1].relevance_score < -500


def test_expression_of_concern_is_penalised_less_than_retraction():
    retracted = _prepared([make_record(pmid="a", publication_types=["Retracted Publication"])])
    concern = _prepared(
        [make_record(pmid="b", extra={"comments_corrections": ["ExpressionOfConcernIn"]})]
    )
    rank_records(retracted, _ctx(query="x"))
    rank_records(concern, _ctx(query="x"))
    assert retracted[0].relevance_score < concern[0].relevance_score


# ======================================================================
# Recency and landmark protection (PART 33)
# ======================================================================
def test_recent_record_outranks_old_one_of_same_design():
    records = _prepared(
        [
            make_record(pmid="old", title="Prospective cohort of implants",
                        publication_types=["Observational Study"], publication_year=1998),
            make_record(pmid="new", title="Prospective cohort of implants",
                        publication_types=["Observational Study"], publication_year=2025),
        ]
    )
    ranked = rank_records(records, _ctx(query="implants cohort"))
    assert ranked[0].pmid == "new"


def test_landmark_systematic_review_is_not_buried_by_age():
    """An old meta-analysis must still beat a recent cross-sectional survey."""
    records = _prepared(
        [
            make_record(pmid="landmark",
                        title="Meta-analysis of implant survival",
                        publication_types=["Meta-Analysis"], publication_year=2005,
                        journal="Clinical Oral Implants Research"),
            make_record(pmid="recent",
                        title="Cross-sectional survey of implant practice",
                        publication_types=[], publication_year=2026,
                        abstract="A cross-sectional questionnaire study."),
        ]
    )
    ranked = rank_records(records, _ctx(query="implant survival"))
    assert ranked[0].pmid == "landmark"


def test_fast_moving_specialty_uses_shorter_half_life():
    ranker = EvidenceRanker()
    old = make_record(publication_year=2018)
    classify_records([old])

    ai_score = ranker._recency(old, _ctx(specialty="dental_ai"))
    general_score = ranker._recency(old, _ctx(specialty="prosthodontics"))
    assert ai_score < general_score


# ======================================================================
# Journal recognition and query relevance
# ======================================================================
def test_recognised_journal_scores_higher():
    records = _prepared(
        [
            make_record(pmid="known", journal="Journal of Prosthetic Dentistry",
                        title="Veneer survival study",
                        publication_types=["Observational Study"], publication_year=2022),
            make_record(pmid="unknown", journal="Some Unlisted Dental Gazette",
                        title="Veneer survival study",
                        publication_types=["Observational Study"], publication_year=2022),
        ]
    )
    ranked = rank_records(records, _ctx(query="veneer survival"))
    assert ranked[0].pmid == "known"
    assert ranked[0].journal_recognised is True
    assert ranked[1].journal_recognised is False


def test_unrecognised_journal_is_ranked_lower_but_not_removed():
    records = _prepared(
        [make_record(journal="Predatory Dental Letters", title="A study",
                     publication_types=["Systematic Review"])]
    )
    ranked = rank_records(records, _ctx(query="a study"))
    assert len(ranked) == 1  # still returned
    assert ranked[0].journal_recognised is False


def test_query_relevance_favours_title_matches():
    records = _prepared(
        [
            make_record(pmid="match",
                        title="Immediate dentin sealing and bond durability",
                        publication_types=["Observational Study"]),
            make_record(pmid="nomatch",
                        title="Orthodontic bracket bonding in adolescents",
                        publication_types=["Observational Study"]),
        ]
    )
    ranked = rank_records(records, _ctx(query="immediate dentin sealing"))
    assert ranked[0].pmid == "match"


def test_directness_uses_pico_terms():
    ranker = EvidenceRanker()
    record = make_record(
        title="Immediate dentin sealing versus delayed sealing",
        abstract="Outcomes included debonding and survival in patients.",
    )
    classify_records([record])
    full = ranker._directness(
        record,
        _ctx(population="patients", intervention="immediate dentin sealing",
             comparator="delayed sealing", outcome="debonding"),
    )
    none = ranker._directness(
        record, _ctx(population="edentulous jaw", intervention="sinus lift",
                     comparator="graft", outcome="perforation")
    )
    assert full == 1.0
    assert none == 0.0


def test_low_confidence_flag_is_set():
    records = _prepared(
        [make_record(title="Untitled note", abstract=None, publication_types=["Editorial"],
                     publication_year=1990, journal=None)]
    )
    rank_records(records, _ctx(query="something entirely unrelated"))
    assert records[0].low_confidence_ranking is True


def test_ranking_explanation_is_present_and_transparent():
    records = _prepared([make_record(publication_types=["Systematic Review"])])
    rank_records(records, _ctx(query="x"))
    explanation = records[0].ranking_explanation
    assert explanation
    assert "design=" in explanation
    assert "recency=" in explanation


def test_describe_ranking_is_honest_about_not_being_grade():
    description = describe_ranking()
    assert "non-GRADE" in description
    assert "Automated evidence prioritization" in description


def test_missing_year_does_not_crash_ranking():
    records = _prepared([make_record(publication_year=None)])
    rank_records(records, _ctx(query="x"))
    assert records[0].relevance_score is not None


# ======================================================================
# Query expansion (PART 16)
# ======================================================================
def test_expansion_preserves_the_original_query():
    result = get_query_expander().expand("lithium disilicate veneer bonding")
    assert "(lithium disilicate veneer bonding)" in result.expanded_query


def test_brand_expands_to_generic():
    result = get_query_expander().expand("IPS e.max etching protocol")
    assert "ips_emax" in result.matched_brands
    assert "lithium_disilicate" in result.matched_concepts


def test_generic_never_narrows_to_brand():
    """The critical asymmetry from PART 16."""
    result = get_query_expander().expand("lithium disilicate survival rate")
    assert result.matched_brands == []
    assert "e.max" not in result.expanded_query.lower()


def test_never_auto_expand_terms_are_not_injected():
    result = get_query_expander().expand("clinical survival of ceramic veneers")
    lowered = result.expanded_query.lower()
    for forbidden in ("bond strength", "thermocycling", "in vitro"):
        assert forbidden not in lowered


def test_expansion_respects_concept_cap():
    rules = get_evidence_rules()
    result = get_query_expander().expand(
        "veneers crowns zirconia bonding implants orthodontic aligners caries fluoride"
    )
    assert len(result.matched_concepts) <= int(rules.expansion.get("max_concepts", 4))


def test_pico_clause_ors_intervention_and_comparator():
    result = get_query_expander().expand(
        "dentin sealing",
        intervention="immediate dentin sealing",
        comparator="delayed dentin sealing",
        outcome="survival",
    )
    assert result.pico_clause is not None
    assert " OR " in result.pico_clause
    assert "survival" in result.pico_clause


def test_pico_splits_multi_term_outcome():
    result = get_query_expander().expand(
        "veneers", outcome="survival / debonding / postoperative sensitivity"
    )
    assert result.pico_clause.count("[Title/Abstract]") >= 3


def test_unmatched_query_is_left_alone_with_a_note():
    result = get_query_expander().expand("some entirely unrelated topic xyzzy")
    assert result.matched_concepts == []
    assert any("unchanged" in note for note in result.notes)


def test_word_boundary_prevents_false_matches():
    """'debonding' must not trigger the 'bonding' concept on its own."""
    expander = get_query_expander()
    concept = expander._concepts["bonding"]
    assert not any(p.search("debonding of a crown") for p in concept.patterns)


# ======================================================================
# Journal registry
# ======================================================================
def test_journal_registry_matches_abbreviations():
    registry = get_journal_registry()
    assert registry.is_recognised("J Prosthet Dent") is True
    assert registry.is_recognised("The Journal of Prosthetic Dentistry") is True
    assert registry.is_recognised("Clin Oral Implants Res") is True
    assert registry.is_recognised("Journal of Totally Made Up Dentistry") is False


def test_journal_tier_affects_weight():
    registry = get_journal_registry()
    core = registry.relevance_weight("Journal of Dental Research")
    unknown = registry.relevance_weight("Unknown Journal")
    assert core > unknown == 0.0


def test_specialty_match_boosts_journal_weight():
    registry = get_journal_registry()
    # Uses a "major"-tier journal: "core" journals already sit at the 1.0
    # ceiling, so the specialty boost would be invisible there.
    with_specialty = registry.relevance_weight("Dental Traumatology", "endodontics")
    without = registry.relevance_weight("Dental Traumatology", "orthodontics")
    assert with_specialty > without
    # And the ceiling genuinely holds for core journals.
    assert registry.relevance_weight("Journal of Endodontics", "endodontics") == 1.0


def test_no_issn_is_none_not_fabricated():
    registry = get_journal_registry()
    for entry in registry.entries:
        assert entry.issn is None or entry.issn.replace("-", "").replace("X", "").isdigit()


# ======================================================================
# Cache
# ======================================================================
@pytest.mark.anyio
async def test_sqlite_cache_roundtrip(tmp_path):
    from app.services.cache import SqliteCache, cache_key

    cache = SqliteCache(tmp_path / "cache.db")
    key = cache_key("evidence_search", {"query": "veneers", "max_results": 10})

    assert await cache.get(key) is None
    await cache.set(key, "evidence_search", {"result_count": 3}, ttl_seconds=60)

    entry = await cache.get(key)
    assert entry is not None
    assert entry.payload == {"result_count": 3}
    assert entry.endpoint == "evidence_search"


@pytest.mark.anyio
async def test_cache_respects_ttl(tmp_path):
    from app.services.cache import SqliteCache

    cache = SqliteCache(tmp_path / "cache.db")
    await cache.set("k", "endpoint", {"a": 1}, ttl_seconds=-1)
    assert await cache.get("k") is None


@pytest.mark.anyio
async def test_cache_purges_expired(tmp_path):
    from app.services.cache import SqliteCache

    cache = SqliteCache(tmp_path / "cache.db")
    await cache.set("live", "e", {"a": 1}, ttl_seconds=600)
    assert await cache.get("live") is not None
    assert await cache.purge_expired() >= 0


def test_cache_key_is_order_independent():
    from app.services.cache import cache_key

    a = cache_key("e", {"query": "x", "max_results": 5})
    b = cache_key("e", {"max_results": 5, "query": "x"})
    assert a == b


def test_cache_key_ignores_empty_values():
    from app.services.cache import cache_key

    assert cache_key("e", {"query": "x"}) == cache_key(
        "e", {"query": "x", "specialty": None, "designs": []}
    )


def test_cache_key_distinguishes_different_queries():
    from app.services.cache import cache_key

    assert cache_key("e", {"query": "a"}) != cache_key("e", {"query": "b"})


@pytest.mark.anyio
async def test_null_cache_is_inert():
    from app.services.cache import NullCache

    cache = NullCache()
    await cache.set("k", "e", {"a": 1}, 60)
    assert await cache.get("k") is None


def test_ttls_are_configured_per_endpoint():
    rules = get_evidence_rules()
    assert rules.ttl("regulatory_search") < rules.ttl("evidence_search")
    assert rules.ttl("evidence_search") <= rules.ttl("crossref_metadata")
