"""Evidence ranking (PART 76).

The algorithm is deliberately simple and fully documented, because an
opaque ranker in a clinical tool is a liability. ``final_score`` is a
weighted sum of eight normalised components, each in ``[0, 1]``:

    design               study design strength (base_score / 100)
    directness           how many PICO elements the record actually covers
    specialty_relevance  match against the requested dental specialty
    recency              exponential decay, half-life varies by specialty
    journal_recognition  membership + tier in approved_journals.yaml
    query_relevance      token overlap between query and title/abstract
    guideline_bonus      flat bonus for A1 guideline records
    integrity_penalty    subtracted for corrections/errata

Weights live in ``evidence_rules.yaml`` and are echoed back to the client
in ``ranking_method`` so the Custom GPT (and the clinician) can see how
the ordering was produced.

Two behaviours are worth calling out:

* **Landmark protection.** Systematic reviews, meta-analyses and
  guidelines never fall below a recency floor. A 2009 Cochrane review is
  not demoted below a 2025 cross-sectional survey merely because of age.
* **Retracted records score ``-1000``** so they sort to the bottom and
  are then removed from the recommendable set by the orchestrator.
"""

from __future__ import annotations

import logging
import math
import re
from dataclasses import dataclass

from app.engines.base import RawRecord
from app.evidence.rules import EvidenceRules, JournalRegistry, get_evidence_rules, get_journal_registry
from app.utils.helpers import utc_now
from app.utils.normalize import normalize_title

logger = logging.getLogger(__name__)

__all__ = ["RankingContext", "EvidenceRanker", "rank_records", "describe_ranking"]

_TOKEN_RE = re.compile(r"[a-z0-9]+")
_STOPWORDS = frozenset(
    {
        "the", "a", "an", "of", "in", "on", "for", "and", "or", "with", "to",
        "is", "are", "was", "were", "be", "by", "at", "as", "from", "that",
        "this", "it", "its", "vs", "versus", "study", "studies", "effect",
        "effects", "using", "used", "use", "between", "after", "before",
    }
)


@dataclass
class RankingContext:
    """Everything the ranker needs beyond the record itself."""

    query: str = ""
    specialty: str | None = None
    population: str | None = None
    intervention: str | None = None
    comparator: str | None = None
    outcome: str | None = None
    current_year: int | None = None

    def pico_terms(self) -> list[tuple[str, str]]:
        return [
            (name, value)
            for name, value in (
                ("population", self.population),
                ("intervention", self.intervention),
                ("comparator", self.comparator),
                ("outcome", self.outcome),
            )
            if value
        ]


class EvidenceRanker:
    def __init__(
        self,
        rules: EvidenceRules | None = None,
        journals: JournalRegistry | None = None,
    ) -> None:
        self._rules = rules or get_evidence_rules()
        self._journals = journals or get_journal_registry()

    # ------------------------------------------------------------------
    def score(self, record: RawRecord, ctx: RankingContext) -> tuple[float, str]:
        """Return ``(score, human_readable_explanation)``."""
        rules = self._rules
        weights = rules.ranking.get("weights", {})
        penalties = rules.ranking.get("penalties", {})

        components: dict[str, float] = {}

        components["design"] = rules.base_score(record.evidence_class) / 100.0
        components["directness"] = self._directness(record, ctx)
        components["specialty_relevance"] = self._specialty_relevance(record, ctx)
        components["recency"] = self._recency(record, ctx)
        components["journal_recognition"] = self._journals.relevance_weight(
            record.journal, ctx.specialty
        )
        components["query_relevance"] = self._query_relevance(record, ctx)
        components["guideline_bonus"] = 1.0 if record.evidence_class == "A1" else 0.0
        components["integrity_penalty"] = self._integrity_penalty(record)

        score = 0.0
        for name, value in components.items():
            weight = float(weights.get(name, 0.0))
            if name == "integrity_penalty":
                score -= weight * value * 100
            else:
                score += weight * value * 100

        # Hard penalties.
        if record.integrity_status == "retracted":
            score += float(penalties.get("retracted", -1000))
        elif record.integrity_status == "expression_of_concern":
            score += float(penalties.get("expression_of_concern", -400))
        elif record.integrity_status in {"correction", "erratum"}:
            score += float(penalties.get("correction", -5))

        if not record.abstract:
            score += float(penalties.get("no_abstract", -6))
            if record.language and not record.language.lower().startswith("eng"):
                score += float(penalties.get("non_english_no_abstract", -8))

        explanation = self._explain(components, weights, record)
        return round(score, 2), explanation

    # ------------------------------------------------------------------
    # Components
    # ------------------------------------------------------------------
    def _directness(self, record: RawRecord, ctx: RankingContext) -> float:
        """Fraction of supplied PICO elements traceable in the record text."""
        pico = ctx.pico_terms()
        if not pico:
            # Without PICO we cannot measure directness; return a neutral
            # mid value rather than rewarding or punishing the record.
            return 0.5
        text = _tokenise(record.searchable_text)
        if not text:
            return 0.0
        hits = 0
        for _, value in pico:
            terms = _tokenise(value)
            if terms and (terms & text):
                hits += 1
        return hits / len(pico)

    def _specialty_relevance(self, record: RawRecord, ctx: RankingContext) -> float:
        if not ctx.specialty or ctx.specialty == "other":
            return 0.5
        entry = self._journals.lookup(record.journal)
        if entry and entry.specialty == ctx.specialty:
            return 1.0
        # Specialty keyword presence in MeSH/keywords/title.
        needle = set(ctx.specialty.replace("_", " ").split())
        haystack = _tokenise(
            " ".join(
                [record.title or "", " ".join(record.mesh_terms), " ".join(record.keywords)]
            )
        )
        if needle & haystack:
            return 0.8
        return 0.4  # never zero: a valid study is not excluded by specialty label

    def _recency(self, record: RawRecord, ctx: RankingContext) -> float:
        cfg = self._rules.ranking.get("recency", {})
        current = ctx.current_year or utc_now().year
        year = record.publication_year
        if not year:
            return 0.4

        fast = set(cfg.get("fast_moving_specialties", []))
        half_life = float(
            cfg.get("fast_moving_half_life_years", 3)
            if ctx.specialty in fast
            else cfg.get("default_half_life_years", 10)
        )
        half_life = max(0.5, half_life)

        age = max(0, current - year)
        value = math.pow(0.5, age / half_life)

        # Landmark protection.
        landmark_classes = set(cfg.get("landmark_classes", ["A1", "A2"]))
        if record.evidence_class in landmark_classes:
            value = max(value, float(cfg.get("landmark_floor", 0.55)))

        return min(1.0, value)

    def _query_relevance(self, record: RawRecord, ctx: RankingContext) -> float:
        query_tokens = _tokenise(ctx.query)
        if not query_tokens:
            return 0.5
        title_tokens = _tokenise(record.title)
        abstract_tokens = _tokenise(record.abstract or "")

        title_overlap = (
            len(query_tokens & title_tokens) / len(query_tokens) if title_tokens else 0.0
        )
        abstract_overlap = (
            len(query_tokens & abstract_tokens) / len(query_tokens)
            if abstract_tokens
            else 0.0
        )
        # A title match is a much stronger signal than an abstract match.
        return min(1.0, 0.7 * title_overlap + 0.3 * abstract_overlap)

    @staticmethod
    def _integrity_penalty(record: RawRecord) -> float:
        severity = int(record.extra.get("integrity_severity", 0) or 0)
        if severity <= 0:
            return 0.0
        return min(1.0, severity / 4.0)

    # ------------------------------------------------------------------
    def _explain(
        self, components: dict[str, float], weights: dict, record: RawRecord
    ) -> str:
        parts = [
            f"{name}={value:.2f}×{float(weights.get(name, 0.0)):.2f}"
            for name, value in components.items()
            if float(weights.get(name, 0.0)) > 0
        ]
        label = self._rules.class_label(record.evidence_class)
        return f"{label} | " + ", ".join(parts)


def _tokenise(text: str | None) -> set[str]:
    if not text:
        return set()
    normalised = normalize_title(text)
    return {t for t in _TOKEN_RE.findall(normalised) if t and t not in _STOPWORDS and len(t) > 2}


def rank_records(
    records: list[RawRecord],
    ctx: RankingContext,
    *,
    rules: EvidenceRules | None = None,
    journals: JournalRegistry | None = None,
) -> list[RawRecord]:
    """Score, annotate and sort records in descending order of score."""
    active_rules = rules or get_evidence_rules()
    ranker = EvidenceRanker(active_rules, journals)
    registry = journals or get_journal_registry()
    threshold = float(active_rules.ranking.get("low_confidence_threshold", 25))

    for record in records:
        score, explanation = ranker.score(record, ctx)
        record.relevance_score = score
        record.ranking_explanation = explanation
        record.low_confidence_ranking = score <= threshold
        record.journal_recognised = (
            registry.is_recognised(record.journal) if record.journal else None
        )

    records.sort(
        key=lambda r: (
            r.relevance_score if r.relevance_score is not None else -9999,
            r.publication_year or 0,
        ),
        reverse=True,
    )
    return records


def describe_ranking(rules: EvidenceRules | None = None) -> str:
    """One-line, client-visible description of the ranking algorithm."""
    active = rules or get_evidence_rules()
    weights = active.ranking.get("weights", {})
    formatted = ", ".join(f"{k}:{v}" for k, v in weights.items())
    return (
        "Automated evidence prioritization (non-GRADE). Weighted sum of "
        f"[{formatted}] with landmark protection for guidelines and "
        "systematic reviews; retracted records are demoted and excluded."
    )
