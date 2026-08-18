"""Retraction / correction safety (PART 14).

Signals consumed, in order of authority:

  1. PubMed ``PublicationType`` values ("Retracted Publication",
     "Expression of Concern", "Published Erratum", ...)
  2. PubMed ``CommentsCorrections/@RefType`` linkage
     ("RetractionIn", "ErratumIn", "ExpressionOfConcernIn", ...)
  3. Europe PMC ``commentCorrectionList`` types
  4. Crossref ``update-to`` relations
  5. Title-text patterns ("RETRACTED:", "Retraction notice", ...)

The four states are kept distinct and are NOT treated alike:

  retracted              -> excluded from recommendations, flagged
  expression_of_concern  -> excluded from recommendations, flagged
  correction / erratum   -> retained, annotated
  update                 -> retained, annotated
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

from app.engines.base import RawRecord
from app.evidence.rules import EvidenceRules, get_evidence_rules

logger = logging.getLogger(__name__)

__all__ = ["IntegrityAssessment", "RetractionChecker", "check_records"]

# PubMed CommentsCorrections RefTypes / Europe PMC types -> internal state.
# "...In" means the notice is published elsewhere and points AT this
# article, i.e. THIS article is the one retracted/corrected.
_REF_TYPE_MAP: dict[str, str] = {
    "retractionin": "retracted",
    "retractionof": "retraction_notice",
    "expressionofconcernin": "expression_of_concern",
    "expressionofconcernfor": "expression_of_concern_notice",
    "erratumin": "erratum",
    "erratumfor": "erratum_notice",
    "correctedandrepublishedin": "correction",
    "correctedandrepublishedfrom": "correction",
    "updatein": "update",
    "republishedin": "update",
    "retracted": "retracted",
    "expression of concern": "expression_of_concern",
    "correction": "correction",
    "erratum": "erratum",
}

_SEVERITY = {
    "retracted": 4,
    "expression_of_concern": 3,
    "correction": 2,
    "erratum": 1,
    "update": 1,
    "retraction_notice": 0,
    "expression_of_concern_notice": 0,
    "erratum_notice": 0,
}

_EXCLUDES = {"retracted", "expression_of_concern"}

_MESSAGES = {
    "retracted": (
        "This publication is flagged as RETRACTED. It must not be used to "
        "support a clinical recommendation."
    ),
    "expression_of_concern": (
        "An expression of concern has been issued about this publication. "
        "Treat its findings as unreliable pending resolution."
    ),
    "correction": (
        "A correction has been published for this article. Verify specific "
        "figures against the corrected version before citing them."
    ),
    "erratum": "An erratum has been published for this article.",
    "update": "An updated version of this article exists.",
    "retraction_notice": (
        "This record is itself a retraction notice about another article, "
        "not a retracted study."
    ),
    "expression_of_concern_notice": (
        "This record is itself an expression-of-concern notice about another "
        "article."
    ),
    "erratum_notice": "This record is itself an erratum for another article.",
}


@dataclass
class IntegrityAssessment:
    status: str | None
    severity: int
    excluded_from_recommendation: bool
    retraction_warning: bool
    notes: list[str]


class RetractionChecker:
    """Assess publication-integrity status for a record."""

    def __init__(self, rules: EvidenceRules | None = None) -> None:
        self._rules = rules or get_evidence_rules()

    def assess(self, record: RawRecord) -> IntegrityAssessment:
        findings: list[tuple[str, str]] = []  # (status, note)

        # 1. Publication types (both PubMed and normalised Europe PMC).
        pub_types_lower = {t.lower() for t in record.publication_types}
        for rule in self._rules.integrity_rules:
            if rule.publication_types & pub_types_lower:
                matched = sorted(rule.publication_types & pub_types_lower)[0]
                status = self._rule_to_status(rule.name)
                findings.append((status, f"Publication type: {matched}"))

        # "Retraction of Publication" means this record IS the notice.
        if "retraction of publication" in pub_types_lower:
            findings.append(("retraction_notice", "Publication type: retraction notice"))

        # 2/3. CommentsCorrections linkage from PubMed and Europe PMC.
        for hint in record.extra.get("comments_corrections") or []:
            key = str(hint).replace(" ", "").replace("-", "").lower()
            status = _REF_TYPE_MAP.get(key) or _REF_TYPE_MAP.get(str(hint).lower())
            if status:
                findings.append((status, f"Linked notice: {hint}"))

        # 4. Crossref update relations (populated by the orchestrator).
        crossref_status = record.extra.get("crossref_integrity_status")
        if crossref_status:
            findings.append((str(crossref_status), "Crossref update relation"))

        # 5. Title / abstract patterns.
        text = record.searchable_text
        for rule in self._rules.integrity_rules:
            for pattern in rule.patterns:
                if pattern.search(text):
                    status = self._rule_to_status(rule.name)
                    findings.append((status, f"Text marker: /{pattern.pattern}/"))
                    break

        if not findings:
            return IntegrityAssessment(None, 0, False, False, [])

        # Highest-severity finding wins.
        findings.sort(key=lambda f: _SEVERITY.get(f[0], 0), reverse=True)
        top_status = findings[0][0]
        severity = _SEVERITY.get(top_status, 0)

        notes: list[str] = []
        message = _MESSAGES.get(top_status)
        if message:
            notes.append(message)
        seen: set[str] = set()
        for _, note in findings:
            if note not in seen:
                notes.append(note)
                seen.add(note)

        excluded = top_status in _EXCLUDES
        return IntegrityAssessment(
            status=top_status,
            severity=severity,
            excluded_from_recommendation=excluded,
            retraction_warning=top_status == "retracted",
            notes=notes,
        )

    @staticmethod
    def _rule_to_status(rule_name: str) -> str:
        return {
            "retracted": "retracted",
            "expression_of_concern": "expression_of_concern",
            "correction": "correction",
            "erratum": "erratum",
            "update": "update",
        }.get(rule_name, rule_name)


def check_records(
    records: list[RawRecord], rules: EvidenceRules | None = None
) -> list[RawRecord]:
    """Annotate every record with its integrity status."""
    checker = RetractionChecker(rules)
    for record in records:
        assessment = checker.assess(record)
        record.integrity_status = assessment.status
        record.retraction_warning = assessment.retraction_warning
        for note in assessment.notes:
            if note not in record.integrity_notes:
                record.integrity_notes.append(note)
        record.extra["integrity_severity"] = assessment.severity
        record.extra["excluded_from_recommendation"] = (
            assessment.excluded_from_recommendation
        )
    return records
