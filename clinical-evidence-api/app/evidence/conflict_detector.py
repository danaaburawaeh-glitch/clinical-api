"""Conflict detection (PART 15) and manufacturer-vs-evidence conflict (PART 62).

The hard requirement here is honesty. Automated direction-of-effect
extraction from abstracts is genuinely unreliable, so this module:

  * only looks at high-tier records (A1/A2/A3/B1);
  * only reports a conflict when it finds *both* a clearly positive and
    a clearly negative directional statement;
  * never invents an effect size, p-value or explanation;
  * falls back to ``possible_conflict_requires_clinical_review`` whenever
    the signal is weak, mixed or unreadable.

Possible explanations are drawn from a fixed catalogue of study-design
reasons and are only emitted when the corresponding structural condition
is actually observed in the record set (e.g. differing evidence classes,
differing publication years, one record laboratory-flagged).
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

from app.engines.base import RawRecord
from app.evidence.rules import EvidenceRules, get_evidence_rules
from app.utils.normalize import truncate

logger = logging.getLogger(__name__)

__all__ = ["ConflictFinding", "detect_conflict", "detect_manufacturer_conflict"]

_CLASS_STRENGTH = {"A1": 5, "A2": 4, "A3": 3, "B1": 2, "B2": 2, "B3": 1}


@dataclass
class ConflictFinding:
    conflict_detected: bool
    status: str
    agreement: str | None = None
    disagreement: str | None = None
    stronger_evidence: str | None = None
    possible_explanations: list[str] | None = None
    involved_records: list[str] | None = None


def _direction(text: str, rules: EvidenceRules) -> str:
    """Classify a record's reported direction of effect.

    Returns ``"positive"``, ``"negative"``, ``"uncertain"`` or ``"unknown"``.
    """
    if not text or not text.strip():
        return "unknown"

    positive = sum(1 for p in rules.conflict_positive if p.search(text))
    negative = sum(1 for p in rules.conflict_negative if p.search(text))
    uncertain = sum(1 for p in rules.conflict_uncertain if p.search(text))

    if positive and negative:
        # Both present in the same abstract — typically "significant for
        # outcome A, no difference for outcome B". Not a between-study
        # conflict; flag as uncertain rather than pretending to resolve it.
        return "uncertain"
    if positive:
        return "positive"
    if negative:
        return "negative"
    if uncertain:
        return "uncertain"
    return "unknown"


def detect_conflict(
    records: list[RawRecord], rules: EvidenceRules | None = None
) -> ConflictFinding:
    """Detect disagreement between high-tier records."""
    active = rules or get_evidence_rules()
    cfg = active.conflict
    eligible_classes = set(cfg.get("eligible_classes", ["A1", "A2", "A3", "B1"]))
    min_records = int(cfg.get("min_records", 2))
    fallback = str(cfg.get("fallback_status", "possible_conflict_requires_clinical_review"))

    eligible = [
        r
        for r in records
        if r.evidence_class in eligible_classes
        and not r.retraction_warning
        and r.integrity_status != "expression_of_concern"
    ]

    if len(eligible) < min_records:
        return ConflictFinding(
            conflict_detected=False,
            status="insufficient_high_tier_records_for_conflict_analysis",
        )

    grouped: dict[str, list[RawRecord]] = {
        "positive": [], "negative": [], "uncertain": [], "unknown": []
    }
    for record in eligible:
        grouped[_direction(record.searchable_text, active)].append(record)

    positives = grouped["positive"]
    negatives = grouped["negative"]

    # No readable direction at all -> say so, do not claim agreement.
    if not positives and not negatives:
        return ConflictFinding(
            conflict_detected=False,
            status="direction_of_effect_not_machine_readable",
            agreement=None,
            disagreement=None,
            stronger_evidence=None,
            possible_explanations=[],
            involved_records=[_ref(r) for r in eligible[:6]],
        )

    if not (positives and negatives):
        # One-sided signal: consistent as far as the metadata shows, but
        # this is an abstract-level observation, not a synthesis.
        side = "reported a benefit" if positives else "reported no significant difference"
        group = positives or negatives
        return ConflictFinding(
            conflict_detected=False,
            status="no_conflict_detected",
            agreement=(
                f"{len(group)} of {len(eligible)} higher-tier record(s) {side} "
                "based on abstract-level wording."
            ),
            involved_records=[_ref(r) for r in group[:6]],
            possible_explanations=[],
        )

    # ---- A genuine two-sided disagreement -----------------------------
    strongest_pos = max(positives, key=lambda r: _CLASS_STRENGTH.get(r.evidence_class or "", 0))
    strongest_neg = max(negatives, key=lambda r: _CLASS_STRENGTH.get(r.evidence_class or "", 0))

    pos_strength = _CLASS_STRENGTH.get(strongest_pos.evidence_class or "", 0)
    neg_strength = _CLASS_STRENGTH.get(strongest_neg.evidence_class or "", 0)

    if pos_strength > neg_strength:
        stronger = (
            f"The higher-tier record ({active.class_label(strongest_pos.evidence_class)}) "
            "reports a benefit."
        )
    elif neg_strength > pos_strength:
        stronger = (
            f"The higher-tier record ({active.class_label(strongest_neg.evidence_class)}) "
            "reports no significant difference."
        )
    else:
        stronger = (
            "Both positions are supported by records of comparable design tier; "
            "no automated precedence can be assigned."
        )

    explanations = _build_explanations(positives, negatives, active)

    # If the strongest record on each side is only B1, or the eligible
    # set is very thin, defer to clinical review rather than asserting a
    # conflict we cannot substantiate.
    signal_is_thin = max(pos_strength, neg_strength) <= 2 or len(eligible) < 3

    disagreement = (
        f"{len(positives)} record(s) report a benefit while {len(negatives)} "
        "report no significant difference, based on abstract-level wording."
    )
    if signal_is_thin:
        disagreement += (
            " The automated signal is not strong enough to characterise this "
            "reliably; clinical review of the full texts is required."
        )

    return ConflictFinding(
        conflict_detected=not signal_is_thin,
        status=fallback if signal_is_thin else "conflict_detected",
        agreement=(
            "All included records address the same clinical question within the "
            "approved evidence sources."
        ),
        disagreement=disagreement,
        stronger_evidence=stronger,
        possible_explanations=explanations,
        involved_records=[_ref(r) for r in (positives[:3] + negatives[:3])],
    )


def _build_explanations(
    positives: list[RawRecord], negatives: list[RawRecord], rules: EvidenceRules
) -> list[str]:
    """Emit only explanations justified by observable structure."""
    explanations: list[str] = []

    pos_classes = {r.evidence_class for r in positives}
    neg_classes = {r.evidence_class for r in negatives}
    if pos_classes != neg_classes:
        explanations.append(
            "The records differ in study design "
            f"({', '.join(sorted(c for c in pos_classes if c))} versus "
            f"{', '.join(sorted(c for c in neg_classes if c))}), which commonly "
            "produces divergent conclusions."
        )

    years = [r.publication_year for r in positives + negatives if r.publication_year]
    if years and (max(years) - min(years)) >= 5:
        explanations.append(
            f"Publication years span {min(years)}–{max(years)}; materials, "
            "techniques and outcome definitions may have changed over that period."
        )

    if any(r.extra.get("laboratory_firewall") for r in positives + negatives):
        explanations.append(
            "At least one record is laboratory/preclinical; bench findings often "
            "diverge from clinical outcomes."
        )

    journals = {r.journal for r in positives + negatives if r.journal}
    if len(journals) > 1:
        explanations.append(
            "Records come from different journals and therefore likely different "
            "populations, operators and follow-up protocols."
        )

    if any(
        p.search(r.searchable_text)
        for r in positives + negatives
        for p in rules.conflict_uncertain
    ):
        explanations.append(
            "At least one record explicitly reports low certainty, high risk of "
            "bias or substantial heterogeneity."
        )

    if not explanations:
        explanations.append(
            "No structural explanation could be derived automatically; clinical "
            "review of the full texts is required."
        )
    return explanations


def _ref(record: RawRecord) -> str:
    """Short, non-fabricated reference label."""
    ident = record.pmid or record.doi or record.pmcid or "no-identifier"
    return f"{truncate(record.title, 90)} [{ident}]"


# ----------------------------------------------------------------------
# Manufacturer IFU vs independent evidence (PART 62)
# ----------------------------------------------------------------------
def detect_manufacturer_conflict(
    manufacturer_records: list[RawRecord],
    evidence_records: list[RawRecord],
) -> dict | None:
    """Describe a divergence between an IFU and independent evidence.

    The output never tells a clinician to ignore an IFU. It states both
    positions, labels the nature of the independent evidence, and hands
    the decision back to professional judgement.
    """
    if not manufacturer_records or not evidence_records:
        return None

    relevant = [
        r for r in evidence_records
        if r.evidence_class in {"A1", "A2", "A3", "B1", "D1"}
        and not r.retraction_warning
    ]
    if not relevant:
        return None

    lab_only = all(r.evidence_class == "D1" for r in relevant)
    top = max(relevant, key=lambda r: _CLASS_STRENGTH.get(r.evidence_class or "", 0))

    if lab_only:
        nature = (
            "The independent findings are laboratory/preclinical "
            "(EARLY_PRECLINICAL); clinical translation is uncertain."
        )
    else:
        nature = (
            "The independent findings include clinical evidence "
            f"({top.evidence_type or 'clinical study'})."
        )

    manufacturer_name = next(
        (r.manufacturer for r in manufacturer_records if r.manufacturer), "The manufacturer"
    )

    return {
        "conflict_detected": True,
        "manufacturer_position": (
            f"{manufacturer_name} instructions for use specify the protocol "
            "described in the returned manufacturer document(s)."
        ),
        "independent_evidence_position": (
            "Independent records in the approved evidence sources describe "
            "outcomes for an alternative protocol."
        ),
        "evidence_nature": nature,
        "guidance": (
            "Do not treat this as an instruction to disregard the official IFU. "
            "Clinical use should weigh the manufacturer's validated protocol, the "
            "strength and directness of the independent evidence, material-specific "
            "risks, regulatory labelling, and the clinician's professional judgement."
        ),
    }
