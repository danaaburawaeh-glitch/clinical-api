"""Search orchestration (PART 10, PART 38, PART 57, PART 58).

The clinical evidence pipeline, in order::

    normalise query
      -> dental synonym expansion (+ PICO clause)
      -> parallel PubMed + Europe PMC (+ optional guideline) search
      -> Crossref metadata validation / integrity check
      -> normalise records
      -> deduplicate across providers
      -> retraction / correction check
      -> study-design classification (+ laboratory firewall)
      -> evidence ranking
      -> conflict detection
      -> structured response

Failure policy (PART 57): providers fail independently. If PubMed times
out but Europe PMC answers, the response is returned with
``partial_results = true``, the failure named in ``failed_sources`` and a
warning. Only a total failure produces an error.

Output policy (PART 67): this service returns *data*. It does not write
a clinical answer. ``evidence_summary`` is a structural, count-based
description that never states an effect size, p-value, confidence
interval, sample size or follow-up duration (PART 39).
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections import Counter
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable

from app.engines.base import EngineError, RawRecord
from app.engines.crossref import CrossrefEngine
from app.engines.europe_pmc import EuropePmcEngine
from app.engines.guideline_search import GuidelineSearchEngine
from app.engines.pubmed import PubMedEngine
from app.evidence.classifier import classify_records
from app.evidence.conflict_detector import detect_conflict
from app.evidence.deduplicator import deduplicate
from app.evidence.query_expander import (
    ExpansionResult,
    _to_europe_pmc_dialect,
    get_query_expander,
)
from app.evidence.ranker import RankingContext, describe_ranking, rank_records
from app.evidence.retraction_check import check_records
from app.evidence.rules import get_evidence_rules
from app.models.schemas import (
    ClinicalTranslation,
    ConflictReport,
    EvidenceLevel,
    EvidenceSearchRequest,
    FailedSource,
    SearchResponse,
    SearchResult,
)
from app.security.allowlist import SourceRegistry, get_source_registry
from app.security.safe_http import SafeHttpClient
from app.settings import get_settings
from app.security.url_validator import UrlValidationError, validate_url_sync
from app.services.logging_service import log_event
from app.utils.helpers import Stopwatch, utc_now, utc_now_iso
from app.utils.normalize import truncate

logger = logging.getLogger(__name__)

__all__ = ["EvidenceOrchestrator", "ProviderOutcome", "records_to_results"]

CROSSREF_VALIDATION_LIMIT = 8


@dataclass
class ProviderOutcome:
    """Result of one provider's contribution to a search."""

    provider: str
    records: list[RawRecord] = field(default_factory=list)
    error: str | None = None
    retryable: bool = True
    duration_ms: float = 0.0

    @property
    def succeeded(self) -> bool:
        return self.error is None


class EvidenceOrchestrator:
    """Runs the full clinical-evidence pipeline for one request."""

    def __init__(
        self,
        http: SafeHttpClient,
        *,
        registry: SourceRegistry | None = None,
    ) -> None:
        self._http = http
        self._registry = registry or get_source_registry()
        self._pubmed = PubMedEngine(http)
        self._europe_pmc = EuropePmcEngine(http)
        self._crossref = CrossrefEngine(http)
        self._guidelines = GuidelineSearchEngine(http, self._registry)
        self._rules = get_evidence_rules()
        self._expander = get_query_expander()
        self._settings = get_settings()

    # ------------------------------------------------------------------
    async def search(
        self, request: EvidenceSearchRequest, *, request_id: str | None = None
    ) -> SearchResponse:
        """Execute the evidence pipeline and build the API response."""
        pipeline_started = time.perf_counter()
        date_from, date_to = request.date_range()
        specialty = request.specialty.value if request.specialty else None

        # ---- 1. Query normalisation + dental expansion -----------------
        expansion = self._expander.expand(
            request.query,
            specialty=specialty,
            population=request.population,
            intervention=request.intervention,
            comparator=request.comparator,
            outcome=request.outcome,
        )

        designs = [d.value for d in request.study_designs] if request.study_designs else None

        # ---- 2. Parallel provider search -------------------------------
        tasks: dict[str, Callable[[], Awaitable[list[RawRecord]]]] = {
            "PubMed": lambda: self._pubmed.search(
                expansion.expanded_query,
                max_results=min(request.max_results * 2, 40),
                date_from=date_from,
                date_to=date_to,
                study_designs=designs,
            ),
            "Europe PMC": lambda: self._europe_pmc.search(
                expansion.europe_pmc_query or expansion.expanded_query,
                max_results=min(request.max_results * 2, 40),
                date_from=date_from,
                date_to=date_to,
            ),
        }

        wants_guidelines = request.include_guidelines and (
            request.question_type is None
            or request.question_type.value in {"guideline", "therapy", "prevention", "other"}
            or (designs is not None and "guideline" in designs)
        )
        if wants_guidelines:
            tasks["Approved Guidelines"] = lambda: self._guideline_records(
                request.query, specialty
            )

        outcomes = await self._run_providers(tasks)

        records: list[RawRecord] = []
        for outcome in outcomes:
            records.extend(outcome.records)

        # ---- 2b. Relaxation ladder -------------------------------------
        # The strict query ANDs the user's own sentence with every concept
        # group and every PICO clause, which can be so narrow that a real,
        # well-indexed literature returns zero hits. Reporting that as
        # "no evidence" would be a false negative — the most dangerous
        # failure this service can produce. Widen and retry instead, and
        # record which relaxation actually produced the results.
        relaxation_used: str | None = None
        relaxation_skipped: bool = False
        if not records and any(o.succeeded for o in outcomes):
            budget = self._settings.relaxation_budget_seconds
            for label, relaxed in expansion.fallback_queries:
                if (time.perf_counter() - pipeline_started) >= budget:
                    # Out of time. Returning an honest empty result beats
                    # a client-side timeout, which surfaces as "technical
                    # failure" and tells the clinician nothing.
                    relaxation_skipped = True
                    break
                retry_tasks: dict[str, Callable[[], Awaitable[list[RawRecord]]]] = {
                    "PubMed": lambda q=relaxed: self._pubmed.search(
                        q,
                        max_results=min(request.max_results * 2, 40),
                        date_from=date_from,
                        date_to=date_to,
                        study_designs=designs,
                    ),
                    "Europe PMC": lambda q=relaxed: self._europe_pmc.search(
                        _to_europe_pmc_dialect(q),
                        max_results=min(request.max_results * 2, 40),
                        date_from=date_from,
                        date_to=date_to,
                    ),
                }
                retry_outcomes = await self._run_providers(retry_tasks)
                retry_records: list[RawRecord] = []
                for outcome in retry_outcomes:
                    retry_records.extend(outcome.records)
                if retry_records:
                    records = retry_records
                    outcomes = retry_outcomes
                    relaxation_used = label
                    break

        searched = [o.provider for o in outcomes]
        successful = [o.provider for o in outcomes if o.succeeded]
        failed = [
            FailedSource(provider=o.provider, reason=o.error or "unknown", retryable=o.retryable)
            for o in outcomes
            if not o.succeeded
        ]

        # Total failure -> report honestly, never substitute another source.
        if not successful:
            return self._empty_response(
                request,
                expansion,
                searched,
                successful,
                failed,
                request_id,
                warnings=[
                    "All approved evidence providers failed for this request. "
                    "No substitute sources were consulted."
                ],
            )

        # ---- 3. Deduplicate --------------------------------------------
        dedup = deduplicate(records)
        records = dedup.records

        # ---- 4. Crossref metadata validation ---------------------------
        crossref_outcome = await self._validate_with_crossref(records)
        if crossref_outcome is not None:
            searched.append("Crossref")
            if crossref_outcome.succeeded:
                successful.append("Crossref")
            else:
                failed.append(
                    FailedSource(
                        provider="Crossref",
                        reason=crossref_outcome.error or "unknown",
                        retryable=crossref_outcome.retryable,
                    )
                )

        # ---- 5. Integrity, classification, ranking ---------------------
        check_records(records, self._rules)
        classify_records(records, rules=self._rules, registry=self._registry)

        ctx = RankingContext(
            query=request.query,
            specialty=specialty,
            population=request.population,
            intervention=request.intervention,
            comparator=request.comparator,
            outcome=request.outcome,
            current_year=utc_now().year,
        )
        rank_records(records, ctx, rules=self._rules)

        # ---- 6. Exclusions ---------------------------------------------
        recommendable, excluded = self._split_excluded(records)
        final_records = recommendable[: request.max_results]

        # A retracted record is surfaced only to explain its exclusion, and
        # only if we would otherwise return almost nothing (PART 14).
        if len(final_records) < 2 and excluded:
            final_records = final_records + excluded[: 2 - len(final_records)]

        # ---- 7. Conflict detection -------------------------------------
        conflict = detect_conflict(recommendable, self._rules)

        # ---- 8. Build response -----------------------------------------
        warnings = self._build_warnings(final_records, excluded, failed, expansion)
        if relaxation_skipped:
            warnings.append(
                "The precise query returned no records and the search could "
                "not be widened within the time budget. This is NOT evidence "
                "of absence — re-run with a shorter, more focused query."
            )
        if relaxation_used:
            # Never widen silently: the caller must know the strict query
            # found nothing and these records come from a looser search.
            warnings.append(
                "The precise query returned no records, so the search was "
                f"widened ({relaxation_used}). These results match the "
                "clinical concepts but may be less specific to the exact "
                "question as phrased; check relevance before citing."
            )
        summary, defer = self._build_summary(recommendable)

        results = records_to_results(final_records, self._registry)

        return SearchResponse(
            query=request.query,
            expanded_query=expansion.expanded_query,
            result_count=len(results),
            searched_sources=_unique(searched),
            successful_sources=_unique(successful),
            failed_sources=failed,
            partial_results=bool(failed) and bool(successful),
            results=results,
            evidence_summary=summary,
            summary_requires_model_synthesis=defer,
            evidence_conflict=ConflictReport(
                conflict_detected=conflict.conflict_detected,
                status=conflict.status,
                agreement=conflict.agreement,
                disagreement=conflict.disagreement,
                stronger_evidence=conflict.stronger_evidence,
                possible_explanations=conflict.possible_explanations or [],
                involved_records=conflict.involved_records or [],
            ),
            warnings=warnings,
            insufficient_evidence=self._is_insufficient(recommendable),
            excluded_count=len(excluded),
            duplicates_merged=dedup.merged_count,
            retrieved_at=utc_now_iso(),
            request_id=request_id,
            ranking_method=describe_ranking(self._rules),
        )

    # ------------------------------------------------------------------
    # Provider execution
    # ------------------------------------------------------------------
    async def _run_providers(
        self, tasks: dict[str, Callable[[], Awaitable[list[RawRecord]]]]
    ) -> list[ProviderOutcome]:
        """Run every provider concurrently; isolate failures (PART 57)."""

        async def run(name: str, factory: Callable[[], Awaitable[list[RawRecord]]]):
            with Stopwatch() as watch:
                try:
                    records = await factory()
                    return ProviderOutcome(name, records, duration_ms=watch.elapsed_ms)
                except EngineError as exc:
                    logger.info(
                        "provider_failed",
                        extra={"provider": name, "reason": exc.reason},
                    )
                    return ProviderOutcome(
                        name, [], error=exc.reason, retryable=exc.retryable,
                        duration_ms=watch.elapsed_ms,
                    )
                except asyncio.CancelledError:
                    raise
                except Exception as exc:  # noqa: BLE001
                    logger.exception("provider_unexpected_error", extra={"provider": name})
                    return ProviderOutcome(
                        name, [], error=f"unexpected error: {exc.__class__.__name__}",
                        retryable=True, duration_ms=watch.elapsed_ms,
                    )

        results = await asyncio.gather(
            *(run(name, factory) for name, factory in tasks.items())
        )
        for outcome in results:
            log_event(
                logger,
                "provider_outcome",
                provider=outcome.provider,
                success=outcome.succeeded,
                result_count=len(outcome.records),
                duration_ms=outcome.duration_ms,
            )
        return list(results)

    async def _guideline_records(
        self, query: str, specialty: str | None
    ) -> list[RawRecord]:
        outcome = await self._guidelines.search(query, specialty=specialty, max_results=3)
        return outcome.records

    # ------------------------------------------------------------------
    # Crossref validation
    # ------------------------------------------------------------------
    async def _validate_with_crossref(
        self, records: list[RawRecord]
    ) -> ProviderOutcome | None:
        """Validate DOIs and pull integrity metadata (PART 9)."""
        candidates = [r for r in records if r.doi][:CROSSREF_VALIDATION_LIMIT]
        if not candidates:
            return None

        with Stopwatch() as watch:
            async def validate(record: RawRecord):
                return record, await self._crossref.validate_doi(
                    record.doi or "",
                    expected_title=record.title,
                    expected_year=record.publication_year,
                )

            try:
                pairs = await asyncio.gather(
                    *(validate(r) for r in candidates), return_exceptions=True
                )
            except asyncio.CancelledError:
                raise

        errors = 0
        for item in pairs:
            if isinstance(item, BaseException):
                errors += 1
                continue
            record, validation = item
            if not validation.found:
                record.extra["crossref_doi_verified"] = False
                continue

            record.extra["crossref_doi_verified"] = True
            record.extra["crossref_metadata_consistent"] = validation.metadata_consistent
            if validation.integrity_status:
                record.extra["crossref_integrity_status"] = validation.integrity_status
                for note in validation.integrity_notes:
                    if note not in record.integrity_notes:
                        record.integrity_notes.append(note)

            # Fill only genuine gaps; never overwrite provider metadata.
            if not record.journal and validation.journal:
                record.journal = validation.journal
            if not record.publication_year and validation.publication_year:
                record.publication_year = validation.publication_year
            if not record.authors and validation.authors:
                record.authors = validation.authors

            if validation.metadata_consistent is False:
                record.integrity_notes.append(
                    "Crossref metadata does not match the indexing record "
                    "(title or year mismatch); verify the citation before use."
                )

        if errors and errors == len(candidates):
            return ProviderOutcome(
                "Crossref", [], error="all DOI validations failed",
                retryable=True, duration_ms=watch.elapsed_ms,
            )
        return ProviderOutcome("Crossref", [], duration_ms=watch.elapsed_ms)

    # ------------------------------------------------------------------
    # Post-processing
    # ------------------------------------------------------------------
    @staticmethod
    def _split_excluded(
        records: list[RawRecord],
    ) -> tuple[list[RawRecord], list[RawRecord]]:
        """Separate recommendable records from excluded ones."""
        recommendable: list[RawRecord] = []
        excluded: list[RawRecord] = []
        for record in records:
            if record.extra.get("excluded_from_recommendation"):
                excluded.append(record)
            else:
                recommendable.append(record)
        return recommendable, excluded

    def _is_insufficient(self, records: list[RawRecord]) -> bool:
        """True when nothing usable was found within approved sources."""
        if not records:
            return True
        usable = [
            r
            for r in records
            if r.evidence_class in {"A1", "A2", "A3", "B1", "B2", "B3", "C1"}
        ]
        return not usable

    def _build_warnings(
        self,
        records: list[RawRecord],
        excluded: list[RawRecord],
        failed: list[FailedSource],
        expansion: ExpansionResult,
    ) -> list[str]:
        warnings: list[str] = []

        for failure in failed:
            warnings.append(
                f"{failure.provider} temporarily unavailable ({failure.reason}); "
                "results are partial. No unapproved source was substituted."
            )

        if excluded:
            retracted = sum(1 for r in excluded if r.retraction_warning)
            concern = len(excluded) - retracted
            if retracted:
                warnings.append(
                    f"{retracted} record(s) were excluded from the recommendable set "
                    "because they are retracted."
                )
            if concern:
                warnings.append(
                    f"{concern} record(s) were excluded because an expression of "
                    "concern has been issued about them."
                )

        lab_count = sum(1 for r in records if r.extra.get("laboratory_firewall"))
        if lab_count:
            warnings.append(
                f"{lab_count} of the returned record(s) are laboratory/preclinical "
                "(EARLY_PRECLINICAL). Bench findings such as bond strength or "
                "thermocycling do not establish clinical superiority."
            )

        manufacturer_count = sum(1 for r in records if r.evidence_class == "M")
        if manufacturer_count:
            warnings.append(
                f"{manufacturer_count} record(s) are manufacturer information and "
                "must be reported as manufacturer claims, not as evidence."
            )

        unrecognised = sum(
            1 for r in records if r.journal and r.journal_recognised is False
        )
        if unrecognised:
            warnings.append(
                f"{unrecognised} record(s) come from journals not present in the "
                "approved dental journal registry; they were ranked lower but not "
                "removed. Assess their provenance before citing."
            )

        no_abstract = sum(1 for r in records if not r.abstract)
        if no_abstract:
            warnings.append(
                f"{no_abstract} record(s) have no abstract available. Their content "
                "was not read; only bibliographic metadata is reported."
            )

        if expansion.notes:
            warnings.extend(expansion.notes)

        return warnings

    def _build_summary(self, records: list[RawRecord]) -> tuple[str | None, bool]:
        """Build a strictly structural evidence summary (PART 39).

        Reports only what can be counted: how many records of each design
        tier were found, how many are laboratory, how many are recent.
        It never states an effect size, p-value, CI, sample size or
        follow-up duration, because none of those were read.
        """
        cfg = self._rules.summary_cfg
        if not cfg.get("enabled", True):
            return None, True

        if not records:
            return None, True

        defer_cfg = cfg.get("defer_to_model_when", {})
        min_total = int(defer_cfg.get("total_results_below", 2))
        if len(records) < min_total:
            return None, True

        counts = Counter(r.evidence_class for r in records if r.evidence_class)
        strongest = next(
            (c for c in ("A1", "A2", "A3", "B1", "B2", "B3", "C1", "C2", "C3", "D1", "D2")
             if counts.get(c)),
            None,
        )

        no_class_above = defer_cfg.get("no_class_above")
        if no_class_above and strongest and _class_rank(strongest) > _class_rank(no_class_above):
            defer = True
        else:
            defer = False

        parts: list[str] = []
        descriptions = [
            ("A1", "clinical practice guideline"),
            ("A2", "systematic review / meta-analysis"),
            ("A3", "randomised controlled trial"),
            ("B1", "prospective cohort study"),
            ("B2", "retrospective cohort study"),
            ("B3", "case-control study"),
            ("C1", "cross-sectional / diagnostic accuracy study"),
            ("C2", "case series"),
            ("C3", "case report"),
            ("D1", "laboratory / in-vitro study"),
            ("D2", "animal / preclinical study"),
            ("NARRATIVE", "narrative review"),
            ("M", "manufacturer document"),
            ("R", "regulatory record"),
            ("U", "unclassified record"),
        ]
        for cls_name, label in descriptions:
            count = counts.get(cls_name, 0)
            if count:
                parts.append(f"{count} {label}{'s' if count > 1 else ''}")

        years = [r.publication_year for r in records if r.publication_year]
        year_text = (
            f" Publication years range from {min(years)} to {max(years)}."
            if years
            else ""
        )

        lab_count = counts.get("D1", 0) + counts.get("D2", 0)
        lab_text = (
            f" {lab_count} record(s) are laboratory/preclinical and their clinical "
            "translation is uncertain."
            if lab_count
            else ""
        )

        summary = (
            f"Retrieved {len(records)} record(s) from approved sources: "
            + ", ".join(parts)
            + "."
            + year_text
            + lab_text
            + " This is a structural description of the retrieved record set, not a "
            "clinical synthesis: no effect size, p-value, confidence interval, "
            "sample size or follow-up duration has been extracted."
        )

        max_len = int(cfg.get("max_length_chars", 900))
        return truncate(summary, max_len), defer

    def _empty_response(
        self,
        request: EvidenceSearchRequest,
        expansion: ExpansionResult,
        searched: list[str],
        successful: list[str],
        failed: list[FailedSource],
        request_id: str | None,
        warnings: list[str] | None = None,
    ) -> SearchResponse:
        return SearchResponse(
            query=request.query,
            expanded_query=expansion.expanded_query,
            result_count=0,
            searched_sources=_unique(searched),
            successful_sources=_unique(successful),
            failed_sources=failed,
            partial_results=bool(successful),
            results=[],
            evidence_summary=None,
            summary_requires_model_synthesis=True,
            warnings=warnings
            or ["Insufficient high-quality evidence within approved sources."],
            insufficient_evidence=True,
            retrieved_at=utc_now_iso(),
            request_id=request_id,
            ranking_method=describe_ranking(self._rules),
        )


# ----------------------------------------------------------------------
# Record -> API result
# ----------------------------------------------------------------------
def records_to_results(
    records: list[RawRecord], registry: SourceRegistry | None = None
) -> list[SearchResult]:
    """Serialise records, re-validating every emitted URL.

    This is the last line of the source firewall: even if an engine were
    compromised or buggy, a URL that is not on the allowlist is stripped
    here and the record is marked ``verified_source = false`` rather than
    being emitted with an unapproved link.
    """
    reg = registry or get_source_registry()
    results: list[SearchResult] = []

    for record in records:
        entry = reg.match_host(record.source_domain)
        url: str | None = None
        verified = False

        if record.url:
            try:
                validated = validate_url_sync(record.url, registry=reg)
                url = validated.url
                verified = True
                entry = entry or validated.entry
            except UrlValidationError as exc:
                logger.warning(
                    "emitted_url_rejected",
                    extra={"reason": exc.reason, "provider": record.provider},
                )
                url = None
                verified = False
        elif entry is not None:
            verified = True

        evidence_level = _to_evidence_level(record.evidence_level)
        translation = _to_translation(record.clinical_translation)

        results.append(
            SearchResult(
                title=record.title or "[No title provided by source]",
                authors=record.authors,
                publication_year=record.publication_year,
                journal=record.journal,
                source=record.provider,
                source_domain=record.source_domain,
                source_category=entry.category if entry else None,
                trust_tier=entry.trust_tier if entry else None,
                providers=record.providers,
                url=url,
                doi=record.doi,
                pmid=record.pmid,
                pmcid=record.pmcid,
                evidence_type=record.evidence_type,
                evidence_class=record.evidence_class,
                evidence_level=evidence_level,
                clinical_translation=translation,
                abstract=record.abstract,
                key_findings=record.key_findings,
                limitations=record.limitations,
                verified_source=verified,
                journal_recognised=record.journal_recognised,
                full_text_reviewed=record.full_text_reviewed,
                open_access=record.open_access,
                language=record.language,
                mesh_terms=record.mesh_terms[:20],
                publication_types=record.publication_types[:15],
                retraction_warning=record.retraction_warning,
                integrity_status=record.integrity_status,
                integrity_notes=record.integrity_notes[:6],
                regulatory_identifier=record.regulatory_identifier,
                regulatory_authority=record.regulatory_authority,
                regulatory_pathway=record.regulatory_pathway,
                regulatory_status=record.regulatory_status,
                decision_date=record.decision_date,
                retrieved_at=record.retrieved_at,
                document_type=record.document_type,
                manufacturer=record.manufacturer,
                product=record.product,
                document_version=record.document_version,
                document_date=record.document_date,
                current_document_verified=record.current_document_verified,
                relevance_score=record.relevance_score,
                ranking_explanation=record.ranking_explanation,
                low_confidence_ranking=record.low_confidence_ranking,
            )
        )

    return results


def _to_evidence_level(value: str | None) -> EvidenceLevel:
    try:
        return EvidenceLevel(value) if value else EvidenceLevel.UNCLASSIFIED
    except ValueError:
        return EvidenceLevel.UNCLASSIFIED


def _to_translation(value: str | None) -> ClinicalTranslation | None:
    if not value:
        return None
    try:
        return ClinicalTranslation(value)
    except ValueError:
        return None


def _class_rank(cls_name: str) -> int:
    order = ["A1", "A2", "A3", "B1", "B2", "B3", "C1", "C2", "C3", "NARRATIVE",
             "D1", "D2", "M", "R", "U"]
    return order.index(cls_name) if cls_name in order else len(order)


def _unique(values: list[str]) -> list[str]:
    seen: set[str] = set()
    output: list[str] = []
    for value in values:
        if value not in seen:
            seen.add(value)
            output.append(value)
    return output
