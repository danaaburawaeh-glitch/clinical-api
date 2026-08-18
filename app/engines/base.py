"""Shared engine types.

``RawRecord`` is the internal, provider-neutral representation. Engines
produce it; the evidence pipeline consumes it; only the API layer turns
it into a :class:`~app.models.schemas.SearchResult`.

Keeping a separate internal type matters for one reason: a field that is
unknown stays ``None`` all the way through the pipeline instead of being
defaulted to something printable at parse time (PART 59, PART 68).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass
class RawRecord:
    """A single bibliographic / regulatory / manufacturer record."""

    # Provenance
    provider: str                      # "PubMed", "Europe PMC", "Crossref", ...
    source_domain: str                 # allowlisted domain the record points at
    url: str | None = None

    # Bibliographic core
    title: str = ""
    authors: list[str] = field(default_factory=list)
    journal: str | None = None
    publication_year: int | None = None
    abstract: str | None = None
    language: str | None = None

    # Identifiers — None means "absent upstream", never a guess
    pmid: str | None = None
    pmcid: str | None = None
    doi: str | None = None

    # Classification inputs
    publication_types: list[str] = field(default_factory=list)
    mesh_terms: list[str] = field(default_factory=list)
    keywords: list[str] = field(default_factory=list)

    # Access
    open_access: bool | None = None
    full_text_reviewed: bool = False

    # Regulatory fields
    regulatory_identifier: str | None = None
    regulatory_authority: str | None = None
    regulatory_pathway: str | None = None
    regulatory_status: str | None = None
    decision_date: str | None = None
    retrieved_at: str | None = None

    # Manufacturer fields
    manufacturer: str | None = None
    product: str | None = None
    document_type: str | None = None
    document_version: str | None = None
    document_date: str | None = None
    current_document_verified: bool | None = None

    # Guideline fields
    organization: str | None = None
    guideline_type: str | None = None

    # Pipeline annotations (filled later)
    providers: list[str] = field(default_factory=list)
    evidence_class: str | None = None
    evidence_level: str | None = None
    evidence_type: str | None = None
    clinical_translation: str | None = None
    integrity_status: str | None = None
    integrity_notes: list[str] = field(default_factory=list)
    retraction_warning: bool = False
    journal_recognised: bool | None = None
    relevance_score: float | None = None
    ranking_explanation: str | None = None
    low_confidence_ranking: bool = False
    key_findings: str | None = None
    limitations: str | None = None
    extra: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not self.providers:
            self.providers = [self.provider]

    @property
    def searchable_text(self) -> str:
        """Title + abstract, used by the pattern classifiers."""
        return f"{self.title or ''}\n{self.abstract or ''}"

    def dedup_keys(self) -> tuple[str | None, str | None, str | None]:
        return self.pmid, self.doi, self.pmcid


class EngineError(Exception):
    """Raised by an engine when it cannot complete its search."""

    def __init__(self, provider: str, reason: str, retryable: bool = True) -> None:
        super().__init__(f"{provider}: {reason}")
        self.provider = provider
        self.reason = reason
        self.retryable = retryable
