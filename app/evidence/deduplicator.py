"""Deduplication (PART 13).

The same study routinely arrives from PubMed, Europe PMC and Crossref.
It is ONE study. Matching order, strongest first:

    1. PMID exact match
    2. normalised DOI exact match
    3. PMCID match
    4. normalised title similarity (with a year guard)

Merging preserves the most complete field values and records every
contributing provider in ``providers``. Crucially, ``providers`` is
*informational only* — :mod:`app.evidence.ranker` never reads it, so a
study does not gain rank by being indexed in more databases.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

from app.engines.base import RawRecord
from app.utils.normalize import normalize_title, title_similarity

logger = logging.getLogger(__name__)

__all__ = ["DeduplicationResult", "deduplicate"]

# Provider precedence when choosing which record becomes the "primary"
# representation. PubMed first because its metadata is the most
# consistently curated (MeSH, publication types).
_PROVIDER_PRIORITY = {"PubMed": 0, "Europe PMC": 1, "Crossref": 2}

TITLE_SIMILARITY_THRESHOLD = 0.88
TITLE_SIMILARITY_SHORT_THRESHOLD = 0.95  # stricter for very short titles
SHORT_TITLE_TOKENS = 6


@dataclass
class DeduplicationResult:
    records: list[RawRecord]
    merged_count: int


def deduplicate(records: list[RawRecord]) -> DeduplicationResult:
    """Collapse duplicate records across providers."""
    if not records:
        return DeduplicationResult([], 0)

    ordered = sorted(
        records, key=lambda r: _PROVIDER_PRIORITY.get(r.provider, 99)
    )

    kept: list[RawRecord] = []
    by_pmid: dict[str, RawRecord] = {}
    by_doi: dict[str, RawRecord] = {}
    by_pmcid: dict[str, RawRecord] = {}
    merged = 0

    for record in ordered:
        existing = _find_existing(record, by_pmid, by_doi, by_pmcid, kept)
        if existing is None:
            kept.append(record)
            _index(record, by_pmid, by_doi, by_pmcid)
            continue

        _merge_into(existing, record)
        # Newly-learned identifiers must be indexed so a third provider
        # carrying only the DOI still collapses onto the same record.
        _index(existing, by_pmid, by_doi, by_pmcid)
        merged += 1

    return DeduplicationResult(kept, merged)


def _index(
    record: RawRecord,
    by_pmid: dict[str, RawRecord],
    by_doi: dict[str, RawRecord],
    by_pmcid: dict[str, RawRecord],
) -> None:
    if record.pmid:
        by_pmid.setdefault(record.pmid, record)
    if record.doi:
        by_doi.setdefault(record.doi, record)
    if record.pmcid:
        by_pmcid.setdefault(record.pmcid, record)


def _find_existing(
    record: RawRecord,
    by_pmid: dict[str, RawRecord],
    by_doi: dict[str, RawRecord],
    by_pmcid: dict[str, RawRecord],
    kept: list[RawRecord],
) -> RawRecord | None:
    if record.pmid and record.pmid in by_pmid:
        return by_pmid[record.pmid]
    if record.doi and record.doi in by_doi:
        return by_doi[record.doi]
    if record.pmcid and record.pmcid in by_pmcid:
        return by_pmcid[record.pmcid]

    # Title fallback. Guarded three ways to avoid collapsing genuinely
    # distinct studies: a year sanity check, a token-count-sensitive
    # threshold, and a refusal to merge records whose DOIs both exist and
    # disagree.
    normalised = normalize_title(record.title)
    if not normalised:
        return None
    token_count = len(normalised.split())
    threshold = (
        TITLE_SIMILARITY_SHORT_THRESHOLD
        if token_count <= SHORT_TITLE_TOKENS
        else TITLE_SIMILARITY_THRESHOLD
    )

    for candidate in kept:
        if record.doi and candidate.doi and record.doi != candidate.doi:
            continue
        if record.pmid and candidate.pmid and record.pmid != candidate.pmid:
            continue
        if (
            record.publication_year
            and candidate.publication_year
            and abs(record.publication_year - candidate.publication_year) > 1
        ):
            continue
        if title_similarity(record.title, candidate.title) >= threshold:
            return candidate

    return None


def _merge_into(primary: RawRecord, other: RawRecord) -> None:
    """Fold ``other`` into ``primary``, preferring more complete values."""
    for provider in other.providers or [other.provider]:
        if provider not in primary.providers:
            primary.providers.append(provider)

    # Identifiers: fill gaps only. Never overwrite an existing identifier
    # with a different one — that would be silently rewriting provenance.
    if not primary.pmid and other.pmid:
        primary.pmid = other.pmid
    if not primary.doi and other.doi:
        primary.doi = other.doi
    if not primary.pmcid and other.pmcid:
        primary.pmcid = other.pmcid

    if not primary.journal and other.journal:
        primary.journal = other.journal
    if not primary.publication_year and other.publication_year:
        primary.publication_year = other.publication_year
    if not primary.language and other.language:
        primary.language = other.language

    # Prefer the longer abstract — structured PubMed abstracts are often
    # richer than the Europe PMC copy, but not always.
    if other.abstract and (
        not primary.abstract or len(other.abstract) > len(primary.abstract) * 1.15
    ):
        primary.abstract = other.abstract

    if len(other.authors) > len(primary.authors):
        primary.authors = other.authors

    for pub_type in other.publication_types:
        if pub_type not in primary.publication_types:
            primary.publication_types.append(pub_type)
    for term in other.mesh_terms:
        if term not in primary.mesh_terms:
            primary.mesh_terms.append(term)
    for keyword in other.keywords:
        if keyword not in primary.keywords:
            primary.keywords.append(keyword)

    if primary.open_access is None and other.open_access is not None:
        primary.open_access = other.open_access

    # Integrity signals are unioned: if any provider says "retracted",
    # the merged record is retracted.
    if other.retraction_warning:
        primary.retraction_warning = True
    for note in other.integrity_notes:
        if note not in primary.integrity_notes:
            primary.integrity_notes.append(note)

    other_hints = other.extra.get("comments_corrections") or []
    if other_hints:
        merged_hints = list(primary.extra.get("comments_corrections") or [])
        for hint in other_hints:
            if hint not in merged_hints:
                merged_hints.append(hint)
        primary.extra["comments_corrections"] = merged_hints

    # Keep alternate provider URLs for transparency without changing the
    # canonical URL (which must stay on the primary provider's domain).
    if other.url and other.url != primary.url:
        alternates = list(primary.extra.get("alternate_urls") or [])
        if other.url not in alternates:
            alternates.append(other.url)
        primary.extra["alternate_urls"] = alternates
