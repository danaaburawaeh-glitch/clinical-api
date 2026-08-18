"""SFDA engine (PART 30).

Position stated plainly: at the time of writing, the Saudi Food and Drug
Authority does not publish a stable, documented, public REST API for
medical-device registration lookup equivalent to openFDA. Rather than
deleting the capability (PART 85) or silently substituting a third-party
site (PART 86), this engine:

  1. defines a clean :class:`SfdaEngine` interface that the regulatory
     router already calls;
  2. implements domain-restricted retrieval against ``sfda.gov.sa``
     only, over HTTPS, through the SSRF guard and redirect validator;
  3. returns a clearly-labelled record whose ``regulatory_status`` is
     ``None`` unless a status was literally read from the official page;
  4. always emits a warning telling the caller that SFDA registration
     status must be confirmed directly with SFDA;
  5. exposes ``configure_api`` so that, the day an official API becomes
     available, only this file changes.

A ``TODO`` marks the single genuinely blocked piece: authenticated
access to the SFDA device registry, which requires credentials the
service does not have.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

from app.engines.base import RawRecord
from app.engines.domain_retrieval import FetchStats, discover_links, fetch_page
from app.security.allowlist import SourceEntry, SourceRegistry, get_source_registry
from app.security.safe_http import SafeHttpClient
from app.utils.helpers import utc_now_iso
from app.utils.normalize import normalize_whitespace, truncate

logger = logging.getLogger(__name__)

PROVIDER = "SFDA"
SOURCE_DOMAIN = "sfda.gov.sa"

# Official entry points. Only paths on the allowlisted domain are used.
CANDIDATE_PATHS: tuple[str, ...] = (
    "/en",
    "/en/medicaldevices",
    "/en/medical-devices",
    "/en/regulations",
    "/en/warnings",
    "/en/recalls",
    "/en/news",
)

SEARCH_KEYWORDS_BY_TYPE: dict[str, list[str]] = {
    "registration": ["registration", "registered", "licence", "license", "authorization"],
    "recall": ["recall", "withdrawal", "field safety"],
    "safety_alert": ["safety alert", "warning", "alert", "field safety notice"],
    "classification": ["classification", "class"],
    "any": ["medical device", "registration", "alert", "recall", "guidance"],
}


@dataclass
class SfdaOutcome:
    records: list[RawRecord]
    warnings: list[str]


class SfdaEngine:
    """Domain-restricted SFDA retrieval with an API-ready seam."""

    #: Set by :meth:`configure_api` once an official endpoint exists.
    _api_base_url: str | None = None

    def __init__(
        self, http: SafeHttpClient, registry: SourceRegistry | None = None
    ) -> None:
        self._http = http
        self._registry = registry or get_source_registry()

    # ------------------------------------------------------------------
    @classmethod
    def configure_api(cls, base_url: str | None) -> None:
        """Register an official SFDA API base URL.

        The URL must be on the allowlisted ``sfda.gov.sa`` domain; the
        normal validation pipeline enforces that at request time.
        """
        cls._api_base_url = base_url

    # ------------------------------------------------------------------
    async def search(
        self,
        query: str,
        *,
        product_name: str | None = None,
        manufacturer: str | None = None,
        regulatory_type: str = "any",
        max_results: int = 5,
    ) -> SfdaOutcome:
        """Search official SFDA content."""
        warnings: list[str] = [
            "SFDA does not currently expose a public machine-readable device "
            "registration API. This gateway therefore performs domain-restricted "
            "retrieval from sfda.gov.sa only. Registration status returned here "
            "must be confirmed directly with SFDA before it is relied upon "
            "clinically or commercially."
        ]

        entry = self._registry.match_host(SOURCE_DOMAIN)
        if entry is None:
            warnings.append("sfda.gov.sa is not present in the source allowlist.")
            return SfdaOutcome([], warnings)

        if self._api_base_url:
            # TODO(sfda-api): implement structured parsing when SFDA
            # publishes a documented public endpoint. Requires an agreed
            # response schema and, for the device registry, credentials.
            logger.info("sfda_api_configured_but_unimplemented")

        keywords = list(SEARCH_KEYWORDS_BY_TYPE.get(regulatory_type,
                                                    SEARCH_KEYWORDS_BY_TYPE["any"]))
        for extra in (product_name, manufacturer, query):
            if extra:
                keywords.insert(0, normalize_whitespace(extra))

        records: list[RawRecord] = []
        stats = FetchStats()
        for path in CANDIDATE_PATHS:
            if len(records) >= max_results:
                break
            page = await fetch_page(
                self._http, f"https://www.{SOURCE_DOMAIN}{path}", entry,
                provider="sfda", stats=stats,
            )
            if page is None:
                page = await fetch_page(
                    self._http, f"https://{SOURCE_DOMAIN}{path}", entry,
                    provider="sfda", stats=stats,
                )
            if page is None:
                continue

            for link in discover_links(page, keywords, limit=4):
                if len(records) >= max_results:
                    break
                records.append(self._build_record(link.url, link.text, entry, query))

        if not records and stats.unreachable:
            warnings.append(
                "sfda.gov.sa could not be reached during this request. NOTHING was "
                "checked against SFDA — this is a connectivity failure, not an "
                "absence of registration."
            )
        elif not records:
            warnings.append(
                "No SFDA page matching this query could be retrieved from the "
                "official domain. Absence of a retrieved record is NOT evidence "
                "that a product is unregistered."
            )

        return SfdaOutcome(records[:max_results], warnings)

    # ------------------------------------------------------------------
    def _build_record(
        self, url: str, text: str, entry: SourceEntry, query: str
    ) -> RawRecord:
        return RawRecord(
            provider=PROVIDER,
            source_domain=entry.domain,
            url=url,
            title=truncate(normalize_whitespace(text) or f"SFDA: {query}", 200),
            regulatory_authority="SFDA",
            # Deliberately None: no status is asserted unless one was read
            # literally from an official page (PART 59, PART 68).
            regulatory_status=None,
            regulatory_identifier=None,
            regulatory_pathway="Saudi FDA official publication",
            decision_date=None,
            retrieved_at=utc_now_iso(),
            document_type="regulatory_page",
            limitations=(
                "Retrieved from the official SFDA website by domain-restricted "
                "navigation, not from a structured registration API. Registration "
                "status, identifiers and dates are not asserted and must be "
                "verified directly with SFDA."
            ),
        )
