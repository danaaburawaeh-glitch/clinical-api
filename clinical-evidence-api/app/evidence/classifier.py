"""Evidence classification (PART 11) and the laboratory firewall (PART 12).

What this module does
---------------------
Assigns each record an internal class (A1..D2, M, R, NARRATIVE, U) and
maps it to the API-facing ``evidence_level``.

What this module explicitly does NOT do
---------------------------------------
It does not perform GRADE, ROB2, ROBINS-I or AMSTAR-2. Those need the
full text and a human. Everything here is derived from title, abstract
and publication-type metadata, and the output is labelled
"Automated evidence prioritization" throughout.

Order of precedence (highest first):

1. **Source-category override** — a manufacturer domain is always ``M``
   and a regulator always ``R``. No amount of confident wording on a
   product page can promote it (PART 25).
2. **Laboratory firewall** — an in-vitro / bench study is forced to
   ``D1`` even when its publication types say "Comparative Study"
   (PART 12).
3. **Publication types** — the most reliable structured signal.
4. **Title/abstract patterns** — fallback when types are absent or
   uninformative, which is common for recent and non-MEDLINE records.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

from app.engines.base import RawRecord
from app.evidence.rules import EvidenceRules, get_evidence_rules
from app.security.allowlist import SourceCategory, SourceRegistry, get_source_registry

logger = logging.getLogger(__name__)

__all__ = ["Classification", "EvidenceClassifier", "classify_records"]

# Order matters: the first pattern class that matches wins, so stronger
# designs are tested before weaker ones.
_TEXT_PATTERN_PRIORITY = ("A1", "A2", "A3", "B1", "B2", "B3", "C1", "C2", "C3", "D2", "NARRATIVE")


@dataclass
class Classification:
    evidence_class: str
    evidence_level: str
    evidence_type: str
    clinical_translation: str | None
    reason: str
    laboratory_flagged: bool = False


class EvidenceClassifier:
    """Assign an evidence class to a record."""

    def __init__(
        self,
        rules: EvidenceRules | None = None,
        registry: SourceRegistry | None = None,
    ) -> None:
        self._rules = rules or get_evidence_rules()
        self._registry = registry or get_source_registry()

    # ------------------------------------------------------------------
    def classify(self, record: RawRecord) -> Classification:
        rules = self._rules
        text = record.searchable_text
        pub_types_lower = {t.lower() for t in record.publication_types}

        # -- 1. Source-category override --------------------------------
        entry = self._registry.match_host(record.source_domain)
        if entry is not None:
            if entry.category == SourceCategory.MANUFACTURER:
                return Classification(
                    evidence_class="M",
                    evidence_level="MANUFACTURER_INFORMATION",
                    evidence_type="manufacturer_document",
                    clinical_translation="not_applicable",
                    reason=(
                        "Source is an allowlisted manufacturer domain; manufacturer "
                        "documents are product information, not independent evidence."
                    ),
                )
            if entry.category in {SourceCategory.REGULATOR, SourceCategory.STANDARDS_BODY}:
                return Classification(
                    evidence_class="R",
                    evidence_level="REGULATORY",
                    evidence_type="regulatory_record",
                    clinical_translation="not_applicable",
                    reason=(
                        "Source is a regulatory or standards authority; regulatory "
                        "status is not a measure of clinical effectiveness."
                    ),
                )

        # -- 2. Laboratory firewall -------------------------------------
        lab_hit = self._laboratory_check(text)
        if lab_hit is not None:
            return Classification(
                evidence_class=rules.lab_forced_class,
                evidence_level=rules.api_level(rules.lab_forced_class),
                evidence_type=rules.design_name(rules.lab_forced_class),
                clinical_translation=rules.lab_translation,
                reason=(
                    f"Laboratory evidence firewall triggered ({lab_hit}). "
                    "Bench findings are not clinical outcomes; clinical "
                    "translation is uncertain."
                ),
                laboratory_flagged=True,
            )

        # -- 3. Publication types ---------------------------------------
        for types, cls_name in rules.pubtype_rules:
            if pub_types_lower & types:
                matched = sorted(pub_types_lower & types)[0]
                cls_name = self._refine_with_text(cls_name, text)
                return self._build(cls_name, f"PubMed publication type: {matched}")

        # -- 4. Title / abstract patterns -------------------------------
        for cls_name in _TEXT_PATTERN_PRIORITY:
            for pattern in rules.text_patterns.get(cls_name, []):
                if pattern.search(text):
                    return self._build(
                        cls_name, f"Text pattern match: /{pattern.pattern}/"
                    )

        # -- 5. Give up honestly ----------------------------------------
        return self._build(
            "U",
            "No reliable study-design signal in the available metadata; "
            "left unclassified rather than guessed.",
        )

    # ------------------------------------------------------------------
    def _laboratory_check(self, text: str) -> str | None:
        """Return the triggering marker, or ``None``.

        A record that matches a clinical override marker is exempt: a
        randomised clinical trial that happens to mention "bond strength"
        in its introduction is still a clinical trial.
        """
        rules = self._rules
        if not rules.lab_enabled or not text.strip():
            return None

        for override in rules.lab_overrides:
            if override.search(text):
                return None

        for marker in rules.lab_markers:
            match = marker.search(text)
            if match:
                return match.group(0)[:60]
        return None

    def _refine_with_text(self, cls_name: str, text: str) -> str:
        """Split coarse publication-type buckets using textual cues.

        PubMed's ``Clinical Trial`` / ``Observational Study`` types do not
        distinguish prospective from retrospective cohorts, and
        ``Comparative Study`` says nothing at all about design. This pass
        only ever moves a record *within* the observational band, never
        upward into A-classes.
        """
        if cls_name != "B1":
            return cls_name
        for candidate in ("B2", "B3", "C1"):
            for pattern in self._rules.text_patterns.get(candidate, []):
                if pattern.search(text):
                    return candidate
        return cls_name

    def _build(self, cls_name: str, reason: str) -> Classification:
        rules = self._rules
        translation = self._translation_for(cls_name)
        return Classification(
            evidence_class=cls_name,
            evidence_level=rules.api_level(cls_name),
            evidence_type=rules.design_name(cls_name),
            clinical_translation=translation,
            reason=reason,
        )

    @staticmethod
    def _translation_for(cls_name: str) -> str | None:
        if cls_name in {"D1", "D2"}:
            return "uncertain"
        if cls_name in {"A1", "A2", "A3", "B1", "B2"}:
            return "direct"
        if cls_name in {"B3", "C1", "C2", "C3"}:
            return "indirect"
        if cls_name in {"M", "R"}:
            return "not_applicable"
        return None


def classify_records(
    records: list[RawRecord],
    *,
    rules: EvidenceRules | None = None,
    registry: SourceRegistry | None = None,
) -> list[RawRecord]:
    """Classify every record in place and return the list."""
    classifier = EvidenceClassifier(rules=rules, registry=registry)
    for record in records:
        result = classifier.classify(record)
        record.evidence_class = result.evidence_class
        record.evidence_level = result.evidence_level
        record.evidence_type = result.evidence_type
        record.clinical_translation = result.clinical_translation
        record.extra["classification_reason"] = result.reason
        if result.laboratory_flagged:
            record.extra["laboratory_firewall"] = True
            record.limitations = _append_limitation(
                record.limitations,
                "Laboratory / preclinical study: results were obtained under "
                "in-vitro or simulated conditions and may not translate to "
                "clinical outcomes.",
            )
        if result.evidence_class == "M":
            record.limitations = _append_limitation(
                record.limitations,
                "Manufacturer-supplied information. Not independent clinical "
                "evidence and cannot establish comparative superiority.",
            )
        if result.evidence_class == "R":
            record.limitations = _append_limitation(
                record.limitations,
                "Regulatory record. Describes regulatory status only; it is not "
                "evidence of clinical effectiveness or superiority.",
            )
    return records


def _append_limitation(existing: str | None, addition: str) -> str:
    if not existing:
        return addition
    if addition in existing:
        return existing
    return f"{existing} {addition}"
