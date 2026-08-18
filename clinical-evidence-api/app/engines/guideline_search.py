"""Professional guideline engine (PART 32).

Searches only allowlisted professional and guideline organisations
(ADA, AAE, AAP/perio.org, EFP, ITI, AAPD, NICE, WHO, CDC, ...).

Document typing is deliberate (PART 32): not every organisational PDF is
a "clinical practice guideline". The engine distinguishes

    clinical_practice_guideline
    systematic_review_based_guideline
    consensus_report
    position_statement
    practice_advisory
    best_practices
    policy_statement
    organisational_document   (default when nothing more specific matches)

and returns the organisation, title, year and URL exactly as found —
never a fabricated version number or publication date (PART 68).
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass

from app.engines.base import RawRecord
from app.engines.domain_retrieval import (
    DiscoveredLink,
    FetchStats,
    discover_links,
    fetch_page,
)
from app.security.allowlist import SourceCategory, SourceEntry, SourceRegistry, get_source_registry
from app.security.safe_http import SafeHttpClient
from app.utils.helpers import utc_now_iso
from app.utils.normalize import normalize_whitespace, safe_int, truncate

logger = logging.getLogger(__name__)

PROVIDER = "Approved Guidelines"

# Specialty -> preferred organisation keys from sources.yaml.
SPECIALTY_ORGANISATIONS: dict[str, tuple[str, ...]] = {
    "periodontology": ("aap_perio", "efp", "ada"),
    "implantology": ("iti", "eao", "osseo", "aap_perio"),
    "endodontics": ("aae", "ese", "ada"),
    "orthodontics": ("aao", "bos", "ada"),
    "pediatric_dentistry": ("aapd", "eapd", "ada"),
    "oral_surgery": ("aaoms", "ada"),
    "oral_medicine": ("aaom", "ada"),
    "oral_pathology": ("aaomp", "ada"),
    "radiology": ("aaomr", "ada", "iaea"),
    "prosthodontics": ("acp", "ada"),
    "esthetic_dentistry": ("aacd", "acp", "ada"),
    "restorative": ("ada", "acp"),
    "preventive_dentistry": ("ada", "cdc", "who", "aapd"),
    "general_dentistry": ("ada", "nice", "who", "cdc"),
    "digital_dentistry": ("ada", "iso"),
    "dental_ai": ("ada", "nist", "who"),
    "biomaterials": ("ada", "iso"),
    "tmd_occlusion": ("ada", "acp"),
}

DEFAULT_ORGANISATIONS = ("ada", "nice", "who", "cdc")

# Common document-library paths on professional-body sites.
CANDIDATE_PATHS: tuple[str, ...] = (
    "/",
    "/en",
    "/resources",
    "/guidelines",
    "/clinical-guidelines",
    "/practice",
    "/publications",
    "/about/position-statements",
    "/education",
)

GUIDELINE_KEYWORDS = (
    "clinical practice guideline",
    "guideline",
    "guidance",
    "consensus",
    "position statement",
    "position paper",
    "best practices",
    "policy statement",
    "practice advisory",
    "parameters of care",
    "recommendation",
)

_DOC_TYPE_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    (
        "clinical_practice_guideline",
        re.compile(r"clinical practice guideline|evidence[- ]based guideline|s3[- ]level guideline",
                   re.IGNORECASE),
    ),
    ("consensus_report", re.compile(r"consensus (report|statement|conference)", re.IGNORECASE)),
    ("position_statement", re.compile(r"position (statement|paper)", re.IGNORECASE)),
    ("practice_advisory", re.compile(r"practice advisory|advisory statement", re.IGNORECASE)),
    ("best_practices", re.compile(r"best practice", re.IGNORECASE)),
    ("policy_statement", re.compile(r"policy (statement|on)", re.IGNORECASE)),
    ("parameters_of_care", re.compile(r"parameters of care", re.IGNORECASE)),
    ("systematic_review_based_guideline",
     re.compile(r"guideline.{0,40}systematic review|systematic review.{0,40}guideline",
                re.IGNORECASE)),
    ("guidance_document", re.compile(r"\bguidance\b|\bguideline\b", re.IGNORECASE)),
)

_YEAR_RE = re.compile(r"\b(19[89]\d|20[0-4]\d)\b")


@dataclass
class GuidelineOutcome:
    records: list[RawRecord]
    warnings: list[str]
    searched_organisations: list[str]


class GuidelineSearchEngine:
    """Domain-restricted guideline retrieval from approved organisations."""

    def __init__(
        self, http: SafeHttpClient, registry: SourceRegistry | None = None
    ) -> None:
        self._http = http
        self._registry = registry or get_source_registry()

    # ------------------------------------------------------------------
    async def search(
        self,
        query: str,
        *,
        specialty: str | None = None,
        max_results: int = 5,
        max_organisations: int = 2,
    ) -> GuidelineOutcome:
        warnings: list[str] = []
        org_keys = SPECIALTY_ORGANISATIONS.get(specialty or "", DEFAULT_ORGANISATIONS)
        entries = [
            entry
            for entry in (self._registry.entries.get(key) for key in org_keys)
            if entry is not None
        ][:max_organisations]

        if not entries:
            warnings.append(
                "No allowlisted guideline organisation matched this specialty."
            )
            return GuidelineOutcome([], warnings, [])

        keywords = [normalize_whitespace(query), *GUIDELINE_KEYWORDS]
        records: list[RawRecord] = []
        searched: list[str] = []
        stats = FetchStats()

        for entry in entries:
            searched.append(entry.domain)
            records.extend(
                await self._search_organisation(
                    entry, keywords, query, max_results, stats
                )
            )
            if len(records) >= max_results:
                break

        if not records and stats.unreachable:
            warnings.append(
                f"None of {', '.join(searched)} could be reached during this "
                "request. No guideline source was actually consulted — treat this "
                "as a connectivity failure, not as an absence of guidelines."
            )
        elif not records:
            warnings.append(
                "No guideline document could be retrieved from "
                f"{', '.join(searched)}. Many professional bodies place guidelines "
                "behind member logins or JavaScript search interfaces, which this "
                "gateway does not bypass. Absence here is not evidence that no "
                "guideline exists."
            )

        return GuidelineOutcome(records[:max_results], warnings, searched)

    # ------------------------------------------------------------------
    async def _search_organisation(
        self,
        entry: SourceEntry,
        keywords: list[str],
        query: str,
        max_results: int,
        stats: FetchStats | None = None,
    ) -> list[RawRecord]:
        records: list[RawRecord] = []
        for path in CANDIDATE_PATHS:
            if len(records) >= max_results:
                break
            page = await fetch_page(
                self._http, f"https://www.{entry.domain}{path}", entry,
                provider="guideline", stats=stats,
            )
            if page is None:
                page = await fetch_page(
                    self._http, f"https://{entry.domain}{path}", entry,
                    provider="guideline", stats=stats,
                )
            if page is None:
                continue

            for link in discover_links(page, keywords, limit=5):
                if len(records) >= max_results:
                    break
                record = self._build_record(entry, link, query)
                if record is not None:
                    records.append(record)
        return records

    def _build_record(
        self, entry: SourceEntry, link: DiscoveredLink, query: str
    ) -> RawRecord | None:
        text = normalize_whitespace(link.text)
        if not text:
            return None

        doc_type = self._classify_document(f"{text} {link.url}")
        year_match = _YEAR_RE.search(f"{text} {link.url}")
        year = safe_int(year_match.group(1), 1980, 2100) if year_match else None

        organisation = entry.key.replace("_", " ").upper()

        return RawRecord(
            provider=PROVIDER,
            source_domain=entry.domain,
            url=link.url,
            title=truncate(text, 250),
            publication_year=year,
            organization=organisation,
            guideline_type=doc_type,
            document_type=doc_type,
            retrieved_at=utc_now_iso(),
            # Only true guidelines get guideline publication typing; a
            # position statement is not upgraded into a guideline.
            publication_types=(
                ["Practice Guideline"]
                if doc_type
                in {"clinical_practice_guideline", "systematic_review_based_guideline"}
                else ["Consensus Development Conference"]
                if doc_type == "consensus_report"
                else []
            ),
            abstract=None,
            limitations=(
                "Retrieved by domain-restricted navigation of the organisation's "
                "official website. Document version and publication date are "
                "reported only where they appear literally in the link text or "
                "URL; verify against the organisation's current publication."
            ),
            extra={"discovery_score": link.score, "query": query},
        )

    @staticmethod
    def _classify_document(text: str) -> str:
        for doc_type, pattern in _DOC_TYPE_PATTERNS:
            if pattern.search(text):
                return doc_type
        return "organisational_document"


def guideline_organisations(
    specialty: str | None, registry: SourceRegistry | None = None
) -> list[SourceEntry]:
    """Public helper: which approved bodies cover this specialty."""
    reg = registry or get_source_registry()
    keys = SPECIALTY_ORGANISATIONS.get(specialty or "", DEFAULT_ORGANISATIONS)
    entries = [reg.entries.get(k) for k in keys]
    return [
        e
        for e in entries
        if e is not None
        and e.category
        in {
            SourceCategory.PROFESSIONAL_ORGANIZATION,
            SourceCategory.GUIDELINE_BODY,
            SourceCategory.PUBLIC_HEALTH_AUTHORITY,
            SourceCategory.STANDARDS_BODY,
        }
    ]
