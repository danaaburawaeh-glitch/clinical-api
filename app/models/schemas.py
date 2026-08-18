"""Pydantic request/response models.

These models are the contract the Custom GPT sees. Two properties are
enforced structurally rather than by convention:

  * Unknown metadata is ``None``, never a plausible-looking guess
    (PART 59, PART 68). Every optional identifier defaults to ``None``.
  * Evidence provenance is explicit: ``evidence_level``,
    ``clinical_translation``, ``full_text_reviewed``, ``verified_source``
    and ``source_category`` all travel with each record so the model
    cannot silently upgrade a manufacturer page into clinical evidence.
"""

from __future__ import annotations

from enum import Enum
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator


# ======================================================================
# Enumerations
# ======================================================================
class Specialty(str, Enum):
    PROSTHODONTICS = "prosthodontics"
    RESTORATIVE = "restorative"
    ESTHETIC_DENTISTRY = "esthetic_dentistry"
    IMPLANTOLOGY = "implantology"
    PERIODONTOLOGY = "periodontology"
    ENDODONTICS = "endodontics"
    ORTHODONTICS = "orthodontics"
    PEDIATRIC_DENTISTRY = "pediatric_dentistry"
    ORAL_SURGERY = "oral_surgery"
    ORAL_MEDICINE = "oral_medicine"
    ORAL_PATHOLOGY = "oral_pathology"
    RADIOLOGY = "radiology"
    TMD_OCCLUSION = "tmd_occlusion"
    DIGITAL_DENTISTRY = "digital_dentistry"
    DENTAL_AI = "dental_ai"
    BIOMATERIALS = "biomaterials"
    PREVENTIVE_DENTISTRY = "preventive_dentistry"
    GENERAL_DENTISTRY = "general_dentistry"
    OTHER = "other"


class QuestionType(str, Enum):
    THERAPY = "therapy"
    DIAGNOSIS = "diagnosis"
    PROGNOSIS = "prognosis"
    PREVENTION = "prevention"
    SAFETY = "safety"
    MATERIALS = "materials"
    TECHNIQUE = "technique"
    COMPARISON = "comparison"
    GUIDELINE = "guideline"
    SYSTEMATIC_REVIEW = "systematic_review"
    OTHER = "other"


class StudyDesign(str, Enum):
    GUIDELINE = "guideline"
    CONSENSUS = "consensus"
    SYSTEMATIC_REVIEW = "systematic_review"
    META_ANALYSIS = "meta_analysis"
    RANDOMIZED_CONTROLLED_TRIAL = "randomized_controlled_trial"
    CLINICAL_TRIAL = "clinical_trial"
    COHORT = "cohort"
    CASE_CONTROL = "case_control"
    CROSS_SECTIONAL = "cross_sectional"
    DIAGNOSTIC_ACCURACY = "diagnostic_accuracy"
    LABORATORY = "laboratory"
    CASE_SERIES = "case_series"
    CASE_REPORT = "case_report"


class EvidenceLevel(str, Enum):
    HIGH = "HIGH"
    MODERATE = "MODERATE"
    LIMITED = "LIMITED"
    EARLY_PRECLINICAL = "EARLY_PRECLINICAL"
    GUIDELINE = "GUIDELINE"
    REGULATORY = "REGULATORY"
    MANUFACTURER_INFORMATION = "MANUFACTURER_INFORMATION"
    UNCLASSIFIED = "UNCLASSIFIED"


class RegulatoryAuthority(str, Enum):
    FDA = "FDA"
    SFDA = "SFDA"
    MHRA = "MHRA"
    EU = "EU"
    HEALTH_CANADA = "Health_Canada"
    TGA = "TGA"
    ANY_APPROVED = "ANY_APPROVED"


class RegulatoryType(str, Enum):
    CLEARANCE = "clearance"
    APPROVAL = "approval"
    REGISTRATION = "registration"
    CLASSIFICATION = "classification"
    RECALL = "recall"
    SAFETY_ALERT = "safety_alert"
    ADVERSE_EVENT = "adverse_event"
    INDICATION = "indication"
    LABELING = "labeling"
    ANY = "any"


class ManufacturerDocumentType(str, Enum):
    IFU = "IFU"
    TECHNICAL_MANUAL = "technical_manual"
    SAFETY_DATA = "safety_data"
    COMPOSITION = "composition"
    INDICATION = "indication"
    CONTRAINDICATION = "contraindication"
    SURFACE_TREATMENT = "surface_treatment"
    CEMENTATION_PROTOCOL = "cementation_protocol"
    COMPATIBILITY = "compatibility"
    CURING_PROTOCOL = "curing_protocol"
    STORAGE = "storage"
    PRODUCT_SPECIFICATION = "product_specification"
    ANY = "any"


class ClinicalTranslation(str, Enum):
    """How safely a finding transfers to the chair (PART 12)."""

    DIRECT = "direct"
    INDIRECT = "indirect"
    UNCERTAIN = "uncertain"
    NOT_APPLICABLE = "not_applicable"


# ======================================================================
# Requests
# ======================================================================
class EvidenceSearchRequest(BaseModel):
    model_config = ConfigDict(extra="ignore", str_strip_whitespace=True)

    query: str = Field(..., min_length=2, max_length=800)
    specialty: Specialty | None = None
    question_type: QuestionType | None = None

    # PICO (PART 17)
    population: str | None = Field(default=None, max_length=300)
    intervention: str | None = Field(default=None, max_length=300)
    comparator: str | None = Field(default=None, max_length=300)
    outcome: str | None = Field(default=None, max_length=300)

    date_from: int | None = Field(default=None, ge=1900, le=2100)
    date_to: int | None = Field(default=None, ge=1900, le=2100)

    study_designs: list[StudyDesign] | None = None
    include_guidelines: bool = True
    max_results: int = Field(default=10, ge=1, le=30)

    @field_validator("query")
    @classmethod
    def _query_not_blank(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("query must not be blank")
        return value.strip()

    def date_range(self) -> tuple[int | None, int | None]:
        """Return a sane (from, to) pair, swapping if the caller inverted them."""
        lo, hi = self.date_from, self.date_to
        if lo is not None and hi is not None and lo > hi:
            return hi, lo
        return lo, hi


class RegulatorySearchRequest(BaseModel):
    model_config = ConfigDict(extra="ignore", str_strip_whitespace=True)

    query: str = Field(..., min_length=2, max_length=400)
    authority: RegulatoryAuthority = RegulatoryAuthority.ANY_APPROVED
    product_name: str | None = Field(default=None, max_length=200)
    manufacturer: str | None = Field(default=None, max_length=200)
    regulatory_type: RegulatoryType = RegulatoryType.ANY
    identifier: str | None = Field(default=None, max_length=64)
    max_results: int = Field(default=10, ge=1, le=20)


class ManufacturerSearchRequest(BaseModel):
    model_config = ConfigDict(extra="ignore", str_strip_whitespace=True)

    query: str = Field(..., min_length=2, max_length=400)
    manufacturer: str | None = Field(default=None, max_length=120)
    product_name: str | None = Field(default=None, max_length=200)
    document_type: ManufacturerDocumentType = ManufacturerDocumentType.ANY
    max_results: int = Field(default=5, ge=1, le=15)


class SourceVerifyRequest(BaseModel):
    model_config = ConfigDict(extra="ignore", str_strip_whitespace=True)

    url: str = Field(..., min_length=3, max_length=2048)


# ======================================================================
# Responses
# ======================================================================
class SearchResult(BaseModel):
    """One evidence, regulatory or manufacturer record.

    Every optional field defaults to ``None``. A ``None`` means "this was
    not present in the retrieved data" — it is never filled by inference.
    """

    model_config = ConfigDict(extra="forbid")

    title: str
    authors: list[str] = Field(default_factory=list)
    publication_year: int | None = None
    journal: str | None = None

    source: str
    source_domain: str
    source_category: str | None = None
    trust_tier: str | None = None
    providers: list[str] = Field(default_factory=list)

    url: str | None = None
    doi: str | None = None
    pmid: str | None = None
    pmcid: str | None = None

    evidence_type: str | None = None
    evidence_class: str | None = None
    evidence_level: EvidenceLevel = EvidenceLevel.UNCLASSIFIED
    clinical_translation: ClinicalTranslation | None = None

    abstract: str | None = None
    key_findings: str | None = None
    limitations: str | None = None

    verified_source: bool = False
    journal_recognised: bool | None = None
    full_text_reviewed: bool = False
    open_access: bool | None = None
    language: str | None = None
    mesh_terms: list[str] = Field(default_factory=list)
    publication_types: list[str] = Field(default_factory=list)

    retraction_warning: bool = False
    integrity_status: str | None = None
    integrity_notes: list[str] = Field(default_factory=list)

    # Regulatory-specific
    regulatory_identifier: str | None = None
    regulatory_authority: str | None = None
    regulatory_pathway: str | None = None
    regulatory_status: str | None = None
    decision_date: str | None = None
    retrieved_at: str | None = None

    # Manufacturer-specific
    document_type: str | None = None
    manufacturer: str | None = None
    product: str | None = None
    document_version: str | None = None
    document_date: str | None = None
    current_document_verified: bool | None = None

    # Ranking transparency (PART 76)
    relevance_score: float | None = None
    ranking_explanation: str | None = None
    low_confidence_ranking: bool = False


class FailedSource(BaseModel):
    model_config = ConfigDict(extra="forbid")

    provider: str
    reason: str
    retryable: bool = True


class ConflictReport(BaseModel):
    """Evidence disagreement report (PART 15)."""

    model_config = ConfigDict(extra="forbid")

    conflict_detected: bool = False
    status: str = "no_conflict_detected"
    agreement: str | None = None
    disagreement: str | None = None
    stronger_evidence: str | None = None
    possible_explanations: list[str] = Field(default_factory=list)
    involved_records: list[str] = Field(default_factory=list)


class ManufacturerEvidenceConflict(BaseModel):
    """IFU vs independent-evidence divergence (PART 62)."""

    model_config = ConfigDict(extra="forbid")

    conflict_detected: bool = False
    manufacturer_position: str | None = None
    independent_evidence_position: str | None = None
    evidence_nature: str | None = None
    guidance: str | None = None


class SearchResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    query: str
    expanded_query: str | None = None
    result_count: int = 0
    searched_sources: list[str] = Field(default_factory=list)
    successful_sources: list[str] = Field(default_factory=list)
    failed_sources: list[FailedSource] = Field(default_factory=list)
    partial_results: bool = False

    results: list[SearchResult] = Field(default_factory=list)

    evidence_summary: str | None = None
    summary_requires_model_synthesis: bool = False
    evidence_conflict: ConflictReport | None = None
    manufacturer_evidence_conflict: ManufacturerEvidenceConflict | None = None

    warnings: list[str] = Field(default_factory=list)
    insufficient_evidence: bool = False
    excluded_count: int = 0
    duplicates_merged: int = 0

    retrieved_at: str | None = None
    cache_hit: bool = False
    request_id: str | None = None
    ranking_method: str | None = None
    debug: dict[str, Any] | None = None


class SourceVerifyResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    allowed: bool
    domain: str | None = None
    source_category: str = "unapproved"
    trust_tier: str | None = None
    allowed_use: str = "none"
    forbidden_use: str | None = None
    reason: str


class HealthComponent(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str
    status: Literal["ok", "degraded", "error", "unknown", "skipped"]
    detail: str | None = None
    latency_ms: float | None = None


class HealthResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    status: Literal["ok", "degraded", "error"] = "ok"
    version: str
    service: str
    environment: str
    allowlisted_domains: int = 0
    components: list[HealthComponent] = Field(default_factory=list)
    checked_at: str | None = None


class ErrorDetail(BaseModel):
    model_config = ConfigDict(extra="forbid")

    code: str
    message: str
    retryable: bool = False
    request_id: str | None = None


class ErrorResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    error: ErrorDetail
