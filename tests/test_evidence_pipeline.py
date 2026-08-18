"""Evidence pipeline tests: classification, firewalls, retraction, conflict
(PART 11, 12, 14, 15, 25, 31, 47)."""

from __future__ import annotations

import pytest

from app.evidence.classifier import EvidenceClassifier, classify_records
from app.evidence.conflict_detector import detect_conflict, detect_manufacturer_conflict
from app.evidence.retraction_check import RetractionChecker, check_records
from tests.conftest import make_record


# ======================================================================
# Study-design classification (PART 11)
# ======================================================================
@pytest.mark.parametrize(
    "pub_types,expected_class,expected_level",
    [
        (["Practice Guideline"], "A1", "GUIDELINE"),
        (["Consensus Development Conference"], "A1", "GUIDELINE"),
        (["Systematic Review"], "A2", "HIGH"),
        (["Meta-Analysis"], "A2", "HIGH"),
        (["Randomized Controlled Trial"], "A3", "HIGH"),
        (["Clinical Trial"], "B1", "MODERATE"),
        (["Observational Study"], "B1", "MODERATE"),
        (["Case Reports"], "C3", "LIMITED"),
        (["Review"], "NARRATIVE", "LIMITED"),
        (["Editorial"], "U", "UNCLASSIFIED"),
    ],
)
def test_publication_types_drive_classification(pub_types, expected_class, expected_level):
    record = make_record(
        title="A dental study of clinical outcomes in patients",
        abstract="Patients were treated and followed up.",
        publication_types=pub_types,
    )
    result = EvidenceClassifier().classify(record)
    assert result.evidence_class == expected_class
    assert result.evidence_level == expected_level


@pytest.mark.parametrize(
    "title,expected_class",
    [
        ("Clinical practice guideline for the management of periodontitis", "A1"),
        ("Ceramic veneers: a systematic review and meta-analysis", "A2"),
        ("A randomized controlled trial of two luting cements", "A3"),
        ("A prospective cohort study of 120 implants over 5 years", "B1"),
        ("Retrospective analysis of veneer failures in a private practice", "B2"),
        ("A matched case-control study of peri-implantitis risk", "B3"),
        ("Diagnostic accuracy of AI for proximal caries detection", "C1"),
        ("Case series of 8 patients treated with immediate loading", "C2"),
        ("A case report of a fractured zirconia crown", "C3"),
        ("Narrative review of adhesive systems", "NARRATIVE"),
    ],
)
def test_text_patterns_classify_when_pubtypes_missing(title, expected_class):
    record = make_record(title=title, abstract="", publication_types=[])
    assert EvidenceClassifier().classify(record).evidence_class == expected_class


def test_unclassifiable_record_is_not_guessed():
    record = make_record(
        title="Dental notes on a topic", abstract="", publication_types=[]
    )
    result = EvidenceClassifier().classify(record)
    assert result.evidence_class == "U"
    assert result.evidence_level == "UNCLASSIFIED"
    assert "left unclassified rather than guessed" in result.reason


def test_clinical_trial_refined_to_retrospective_cohort():
    """'Clinical Trial' + retrospective wording must not stay prospective."""
    record = make_record(
        title="Retrospective cohort of ceramic restorations",
        abstract="Records were retrospectively analysed.",
        publication_types=["Clinical Trial"],
    )
    assert EvidenceClassifier().classify(record).evidence_class == "B2"


# ======================================================================
# Laboratory evidence firewall (PART 12)
# ======================================================================
@pytest.mark.parametrize(
    "text",
    [
        "Shear bond strength of universal adhesives to zirconia",
        "Microtensile bond strength after thermocycling",
        "Finite element analysis of a molar restoration",
        "Fracture resistance of specimens stored in artificial saliva",
        "In vitro evaluation of a resin cement",
        "Biaxial flexural strength of lithium disilicate",
        "Surface roughness of disc-shaped specimens after polishing",
        "Cytotoxicity of a dental adhesive using an MTT assay",
        "Extracted human molars were prepared and restored",
        "Scanning electron microscopy of the adhesive interface",
    ],
)
def test_laboratory_studies_are_firewalled(text):
    record = make_record(title=text, abstract="", publication_types=["Comparative Study"])
    result = EvidenceClassifier().classify(record)
    assert result.evidence_class == "D1"
    assert result.evidence_level == "EARLY_PRECLINICAL"
    assert result.clinical_translation == "uncertain"
    assert result.laboratory_flagged is True


def test_laboratory_firewall_beats_comparative_study_pubtype():
    """The critical case: an in-vitro comparison must not read as clinical."""
    record = make_record(
        title="Comparison of two cements: shear bond strength after thermocycling",
        abstract="Specimens were prepared. Bond strength was significantly higher.",
        publication_types=["Comparative Study", "Journal Article"],
    )
    result = EvidenceClassifier().classify(record)
    assert result.evidence_class == "D1"
    assert result.evidence_level != "HIGH"


def test_rct_mentioning_bond_strength_is_not_firewalled():
    """A real clinical trial keeps its class even if it discusses bench data."""
    record = make_record(
        title="A randomized controlled clinical trial of two adhesive protocols",
        abstract=(
            "Previous bond strength studies suggested a difference. "
            "Patients were randomized and followed for 3 years."
        ),
        publication_types=["Randomized Controlled Trial"],
    )
    result = EvidenceClassifier().classify(record)
    assert result.evidence_class == "A3"
    assert result.laboratory_flagged is False


def test_laboratory_records_get_translation_limitation():
    records = classify_records(
        [make_record(title="Shear bond strength of a new primer", publication_types=[])]
    )
    assert records[0].extra["laboratory_firewall"] is True
    assert "in-vitro or simulated conditions" in (records[0].limitations or "")


def test_animal_study_is_early_preclinical():
    record = make_record(
        title="Bone healing in a rabbit calvaria model", publication_types=[]
    )
    result = EvidenceClassifier().classify(record)
    assert result.evidence_class in {"D1", "D2"}
    assert result.evidence_level == "EARLY_PRECLINICAL"


# ======================================================================
# Manufacturer firewall (PART 25)
# ======================================================================
def test_manufacturer_domain_forced_to_manufacturer_information():
    record = make_record(
        provider="Manufacturer (official domain)",
        source_domain="ivoclar.com",
        title="IPS e.max: proven superior clinical performance and survival",
        abstract="Our product demonstrates superior long-term clinical results.",
        publication_types=["Randomized Controlled Trial"],  # even if it claimed this
        pmid=None,
    )
    result = EvidenceClassifier().classify(record)
    assert result.evidence_class == "M"
    assert result.evidence_level == "MANUFACTURER_INFORMATION"
    assert result.clinical_translation == "not_applicable"


def test_manufacturer_record_carries_explicit_limitation():
    records = classify_records(
        [
            make_record(
                source_domain="straumann.com",
                title="BLX surgical protocol",
                publication_types=[],
                pmid=None,
            )
        ]
    )
    assert "Not independent clinical evidence" in (records[0].limitations or "")


@pytest.mark.parametrize(
    "domain", ["ivoclar.com", "kuraraynoritake.com", "3shape.com", "straumann.com",
               "solventum.com", "zimvie.com"]
)
def test_all_manufacturer_domains_are_firewalled(domain):
    record = make_record(source_domain=domain, publication_types=["Meta-Analysis"], pmid=None)
    assert EvidenceClassifier().classify(record).evidence_level == "MANUFACTURER_INFORMATION"


# ======================================================================
# Regulatory separation (PART 31)
# ======================================================================
def test_regulator_domain_is_regulatory_not_evidence():
    record = make_record(
        provider="openFDA",
        source_domain="api.fda.gov",
        title="Device X — FDA 510(k) K251002",
        publication_types=["Randomized Controlled Trial"],
        pmid=None,
    )
    result = EvidenceClassifier().classify(record)
    assert result.evidence_class == "R"
    assert result.evidence_level == "REGULATORY"
    assert "not a measure of clinical effectiveness" in result.reason


def test_regulatory_record_gets_limitation():
    records = classify_records(
        [make_record(source_domain="fda.gov", publication_types=[], pmid=None)]
    )
    assert "not evidence of clinical effectiveness" in (records[0].limitations or "")


# ======================================================================
# Source reliability vs evidence strength (PART 72)
# ======================================================================
def test_case_report_on_pubmed_stays_low_level():
    """PubMed indexing does not upgrade a case report."""
    record = make_record(
        source_domain="pubmed.ncbi.nlm.nih.gov",
        title="A case report of an unusual restoration failure",
        publication_types=["Case Reports"],
    )
    result = EvidenceClassifier().classify(record)
    assert result.evidence_level == "LIMITED"
    assert result.evidence_class == "C3"


# ======================================================================
# Retraction / correction handling (PART 14)
# ======================================================================
def test_retracted_publication_type_flags_record():
    record = make_record(
        title="RETRACTED: A trial of something",
        publication_types=["Randomized Controlled Trial", "Retracted Publication"],
    )
    assessment = RetractionChecker().assess(record)
    assert assessment.status == "retracted"
    assert assessment.retraction_warning is True
    assert assessment.excluded_from_recommendation is True


def test_retraction_in_linkage_flags_record():
    record = make_record(
        title="A normal looking title",
        extra={"comments_corrections": ["RetractionIn"]},
    )
    assessment = RetractionChecker().assess(record)
    assert assessment.status == "retracted"


def test_expression_of_concern_is_distinct_from_retraction():
    record = make_record(
        title="A study", extra={"comments_corrections": ["ExpressionOfConcernIn"]}
    )
    assessment = RetractionChecker().assess(record)
    assert assessment.status == "expression_of_concern"
    assert assessment.retraction_warning is False
    assert assessment.excluded_from_recommendation is True


def test_erratum_is_not_treated_as_retraction():
    record = make_record(title="A study", extra={"comments_corrections": ["ErratumIn"]})
    assessment = RetractionChecker().assess(record)
    assert assessment.status == "erratum"
    assert assessment.retraction_warning is False
    assert assessment.excluded_from_recommendation is False


def test_correction_is_retained_with_notice():
    record = make_record(
        title="A study", extra={"comments_corrections": ["CorrectedandRepublishedIn"]}
    )
    assessment = RetractionChecker().assess(record)
    assert assessment.status == "correction"
    assert assessment.excluded_from_recommendation is False
    assert any("correction" in n.lower() for n in assessment.notes)


def test_crossref_integrity_signal_is_used():
    record = make_record(title="A study", extra={"crossref_integrity_status": "retracted"})
    assert RetractionChecker().assess(record).status == "retracted"


def test_clean_record_has_no_integrity_flag():
    assessment = RetractionChecker().assess(make_record())
    assert assessment.status is None
    assert assessment.retraction_warning is False


def test_highest_severity_wins():
    record = make_record(
        title="RETRACTED: A study",
        extra={"comments_corrections": ["ErratumIn", "RetractionIn"]},
    )
    assert RetractionChecker().assess(record).status == "retracted"


def test_check_records_annotates_in_place():
    records = check_records(
        [make_record(publication_types=["Retracted Publication"]), make_record()]
    )
    assert records[0].retraction_warning is True
    assert records[0].extra["excluded_from_recommendation"] is True
    assert records[1].retraction_warning is False


# ======================================================================
# Conflict detection (PART 15)
# ======================================================================
def test_conflict_detected_between_high_tier_records():
    records = [
        make_record(
            pmid="1",
            title="Systematic review of immediate dentin sealing",
            abstract="There was no significant difference between the groups.",
            publication_types=["Systematic Review"],
            publication_year=2015,
        ),
        make_record(
            pmid="2",
            title="Randomized controlled trial of immediate dentin sealing",
            abstract="The test group showed significantly higher retention (p<0.01).",
            publication_types=["Randomized Controlled Trial"],
            publication_year=2023,
            journal="Journal of Prosthodontics",
        ),
        make_record(
            pmid="3",
            title="Meta-analysis of dentin sealing protocols",
            abstract="No significant benefit was observed.",
            publication_types=["Meta-Analysis"],
            publication_year=2021,
        ),
    ]
    classify_records(records)
    check_records(records)
    finding = detect_conflict(records)

    assert finding.conflict_detected is True
    assert finding.status == "conflict_detected"
    assert "benefit" in (finding.disagreement or "")
    assert finding.stronger_evidence
    assert finding.possible_explanations


def test_thin_signal_defers_to_clinical_review():
    records = [
        make_record(
            pmid="1",
            title="Prospective cohort of veneers",
            abstract="Significantly higher survival was observed.",
            publication_types=["Observational Study"],
        ),
        make_record(
            pmid="2",
            title="Prospective cohort of veneers B",
            abstract="No significant difference was found.",
            publication_types=["Observational Study"],
        ),
    ]
    classify_records(records)
    finding = detect_conflict(records)
    assert finding.conflict_detected is False
    assert finding.status == "possible_conflict_requires_clinical_review"


def test_no_conflict_when_records_agree():
    records = [
        make_record(
            pmid=str(i),
            title=f"Systematic review {i} of veneer survival",
            abstract="No significant difference between the materials was found.",
            publication_types=["Systematic Review"],
        )
        for i in range(3)
    ]
    classify_records(records)
    finding = detect_conflict(records)
    assert finding.conflict_detected is False
    assert finding.status == "no_conflict_detected"
    assert finding.agreement


def test_unreadable_direction_is_reported_honestly():
    """No hallucinated conflict when abstracts say nothing directional."""
    records = [
        make_record(
            pmid=str(i),
            title=f"Systematic review {i}",
            abstract="This review summarises the available literature on the topic.",
            publication_types=["Systematic Review"],
        )
        for i in range(3)
    ]
    classify_records(records)
    finding = detect_conflict(records)
    assert finding.conflict_detected is False
    assert finding.status == "direction_of_effect_not_machine_readable"


def test_retracted_records_excluded_from_conflict_analysis():
    records = [
        make_record(
            pmid="1",
            title="RETRACTED: trial showing significantly higher survival",
            abstract="Significantly higher survival was observed.",
            publication_types=["Randomized Controlled Trial", "Retracted Publication"],
        ),
        make_record(
            pmid="2",
            title="Systematic review",
            abstract="No significant difference was found.",
            publication_types=["Systematic Review"],
        ),
    ]
    classify_records(records)
    check_records(records)
    finding = detect_conflict(records)
    assert finding.conflict_detected is False


def test_too_few_records_for_conflict_analysis():
    records = [make_record(publication_types=["Systematic Review"])]
    classify_records(records)
    finding = detect_conflict(records)
    assert finding.status == "insufficient_high_tier_records_for_conflict_analysis"


# ======================================================================
# Manufacturer vs evidence conflict (PART 62)
# ======================================================================
def test_manufacturer_conflict_never_says_ignore_the_ifu():
    manufacturer = [
        make_record(
            source_domain="ivoclar.com",
            manufacturer="Ivoclar",
            title="Monobond Etch & Prime IFU",
            publication_types=[],
            pmid=None,
        )
    ]
    evidence = [
        make_record(
            title="Shear bond strength after alternative conditioning",
            abstract="Higher bond strength was found for protocol B.",
            publication_types=[],
        )
    ]
    classify_records(manufacturer)
    classify_records(evidence)

    conflict = detect_manufacturer_conflict(manufacturer, evidence)
    assert conflict is not None
    assert conflict["conflict_detected"] is True
    assert "EARLY_PRECLINICAL" in conflict["evidence_nature"]
    guidance = conflict["guidance"].lower()
    assert "do not treat this as an instruction to disregard the official ifu" in guidance
    assert "professional judgement" in guidance
    # It must never tell a clinician to override the IFU.
    assert "ignore the ifu" not in guidance


def test_manufacturer_conflict_none_without_both_sides():
    assert detect_manufacturer_conflict([], [make_record()]) is None
    assert detect_manufacturer_conflict([make_record()], []) is None
