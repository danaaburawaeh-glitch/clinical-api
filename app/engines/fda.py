"""FDA website engine and the extensible regulator registry (PART 28, PART 31).

:class:`OpenFdaEngine` covers everything openFDA exposes as JSON. This
module covers the rest:

  * ``fda.gov`` / ``accessdata.fda.gov`` pages (safety communications,
    guidance documents) via domain-restricted retrieval;
  * a :class:`RegulatorAdapter` protocol plus a registry, so MHRA
    (gov.uk), the EU (ec.europa.eu), Health Canada (canada.ca) and TGA
    (tga.gov.au) are wired in as first-class, extensible adapters rather
    than as a hard-coded ``if`` chain (PART 28).

Every record produced here carries the regulatory/clinical distinction
required by PART 31: regulatory status is never rendered as evidence of
effectiveness or superiority.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Protocol

from app.engines.base import RawRecord
from app.engines.base import EngineError
from app.engines.domain_retrieval import FetchStats, discover_links, fetch_page
from app.security.allowlist import SourceEntry, SourceRegistry, get_source_registry
from app.security.safe_http import SafeHttpClient
from app.utils.helpers import utc_now_iso
from app.utils.normalize import normalize_whitespace, truncate

logger = logging.getLogger(__name__)

__all__ = [
    "RegulatorAdapter",
    "GenericRegulatorEngine",
    "FdaWebEngine",
    "REGULATOR_ADAPTERS",
    "adapters_for_authority",
    "REGULATORY_INTERPRETATION_NOTE",
]

REGULATORY_INTERPRETATION_NOTE = (
    "Regulatory status describes what a regulator permitted to be marketed "
    "and under what conditions. It is not a measure of clinical "
    "effectiveness and never establishes superiority over another product. "
    "FDA 'clearance' (510(k), substantial equivalence to a predicate) and "
    "FDA 'approval' (PMA, premarket review of safety and effectiveness) are "
    "different determinations and must not be used interchangeably. CE "
    "marking and SFDA registration likewise attest to conformity or "
    "registration, not comparative efficacy."
)


@dataclass(frozen=True)
class RegulatorSpec:
    """Declarative description of a regulator's public web presence."""

    authority: str
    domain: str
    display_name: str
    paths: tuple[str, ...]
    keywords: tuple[str, ...]
    pathway_label: str


class RegulatorAdapter(Protocol):
    """Interface every regulator engine implements."""

    authority: str

    async def search(
        self,
        query: str,
        *,
        product_name: str | None = None,
        manufacturer: str | None = None,
        regulatory_type: str = "any",
        max_results: int = 5,
    ) -> list[RawRecord]:
        ...


class GenericRegulatorEngine:
    """Domain-restricted retrieval for a regulator described by a spec."""

    def __init__(
        self,
        spec: RegulatorSpec,
        http: SafeHttpClient,
        registry: SourceRegistry | None = None,
    ) -> None:
        self.spec = spec
        self.authority = spec.authority
        self._http = http
        self._registry = registry or get_source_registry()

    async def search(
        self,
        query: str,
        *,
        product_name: str | None = None,
        manufacturer: str | None = None,
        regulatory_type: str = "any",
        max_results: int = 5,
    ) -> list[RawRecord]:
        entry: SourceEntry | None = self._registry.match_host(self.spec.domain)
        if entry is None:
            logger.warning(
                "regulator_domain_not_allowlisted", extra={"domain": self.spec.domain}
            )
            return []

        keywords = [k for k in (product_name, manufacturer, query) if k]
        keywords.extend(self.spec.keywords)
        if regulatory_type != "any":
            keywords.insert(0, regulatory_type.replace("_", " "))

        records: list[RawRecord] = []
        stats = FetchStats()
        for path in self.spec.paths:
            if len(records) >= max_results:
                break
            page = await fetch_page(
                self._http,
                f"https://www.{self.spec.domain}{path}",
                entry,
                provider="regulator",
                stats=stats,
            )
            if page is None:
                page = await fetch_page(
                    self._http,
                    f"https://{self.spec.domain}{path}",
                    entry,
                    provider="regulator",
                    stats=stats,
                )
            if page is None:
                continue

            for link in discover_links(page, keywords, limit=4):
                if len(records) >= max_results:
                    break
                records.append(
                    RawRecord(
                        provider=self.spec.display_name,
                        source_domain=entry.domain,
                        url=link.url,
                        title=truncate(
                            normalize_whitespace(link.text)
                            or f"{self.spec.display_name}: {query}",
                            200,
                        ),
                        regulatory_authority=self.spec.authority,
                        regulatory_pathway=self.spec.pathway_label,
                        regulatory_status=None,
                        regulatory_identifier=None,
                        decision_date=None,
                        retrieved_at=utc_now_iso(),
                        document_type="regulatory_page",
                        limitations=REGULATORY_INTERPRETATION_NOTE,
                    )
                )

        # Unreachable is not the same as "nothing there" (PART 57).
        if not records and stats.unreachable:
            raise EngineError(
                self.spec.display_name,
                f"{self.spec.domain} could not be reached; regulatory status was "
                "not checked against this authority",
                retryable=True,
            )
        return records


class FdaWebEngine(GenericRegulatorEngine):
    """FDA website retrieval, complementing the structured openFDA engine."""

    def __init__(self, http: SafeHttpClient, registry: SourceRegistry | None = None) -> None:
        super().__init__(_FDA_SPEC, http, registry)


# ----------------------------------------------------------------------
# Regulator registry — adding a regulator is a data change (PART 80)
# ----------------------------------------------------------------------
_FDA_SPEC = RegulatorSpec(
    authority="FDA",
    domain="fda.gov",
    display_name="FDA (website)",
    paths=(
        "/medical-devices",
        "/medical-devices/medical-device-safety",
        "/medical-devices/dental-devices",
        "/safety/recalls-market-withdrawals-safety-alerts",
    ),
    keywords=("device", "safety communication", "guidance", "dental"),
    pathway_label="FDA published guidance or safety communication",
)

_MHRA_SPEC = RegulatorSpec(
    authority="MHRA",
    domain="gov.uk",
    display_name="MHRA (GOV.UK)",
    paths=(
        "/government/organisations/medicines-and-healthcare-products-regulatory-agency",
        "/drug-device-alerts",
        "/guidance/medical-devices-conformity-assessment-and-the-ukca-mark",
    ),
    keywords=("medical device", "field safety notice", "alert", "dental"),
    pathway_label="MHRA guidance, alert or field safety notice",
)

_EU_SPEC = RegulatorSpec(
    authority="EU",
    domain="ec.europa.eu",
    display_name="European Commission",
    paths=(
        "/health/medical-devices-sector_en",
        "/health/medical-devices-topics-interest_en",
    ),
    keywords=("medical device", "MDR", "EUDAMED", "guidance", "dental"),
    pathway_label="EU MDR guidance / EUDAMED reference",
)

_HEALTH_CANADA_SPEC = RegulatorSpec(
    authority="Health_Canada",
    domain="canada.ca",
    display_name="Health Canada",
    paths=(
        "/en/health-canada/services/drugs-health-products/medical-devices.html",
        "/en/health-canada/services/drugs-health-products/medeffect-canada.html",
    ),
    keywords=("medical device", "licence", "recall", "advisory", "dental"),
    pathway_label="Health Canada medical device licence / advisory",
)

_TGA_SPEC = RegulatorSpec(
    authority="TGA",
    domain="tga.gov.au",
    display_name="TGA",
    paths=(
        "/products/medical-devices",
        "/safety/safety-alerts",
    ),
    keywords=("medical device", "ARTG", "recall", "safety alert", "dental"),
    pathway_label="TGA ARTG inclusion / safety alert",
)

REGULATOR_SPECS: dict[str, RegulatorSpec] = {
    "FDA": _FDA_SPEC,
    "MHRA": _MHRA_SPEC,
    "EU": _EU_SPEC,
    "Health_Canada": _HEALTH_CANADA_SPEC,
    "TGA": _TGA_SPEC,
}

REGULATOR_ADAPTERS = tuple(REGULATOR_SPECS)


def adapters_for_authority(
    authority: str,
    http: SafeHttpClient,
    registry: SourceRegistry | None = None,
) -> list[GenericRegulatorEngine]:
    """Return the web adapters that should be queried for an authority."""
    if authority == "ANY_APPROVED":
        # Keep the sweep narrow: openFDA already covers FDA structurally,
        # so the web adapters add MHRA/EU breadth without a fan-out storm.
        wanted = ["MHRA", "EU"]
    elif authority in REGULATOR_SPECS:
        wanted = [authority]
    else:
        wanted = []

    return [
        GenericRegulatorEngine(REGULATOR_SPECS[name], http, registry) for name in wanted
    ]
