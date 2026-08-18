"""Manufacturer document engine (PART 26, PART 25, PART 60).

Scope, enforced structurally rather than by prompt:

  * only domains whose ``category`` is ``manufacturer`` are ever queried;
  * every returned record is classified ``M`` /
    ``MANUFACTURER_INFORMATION`` by :mod:`app.evidence.classifier`;
  * every record carries an explicit limitation stating that
    manufacturer information cannot establish clinical superiority.

Document version handling (PART 60): a revision number, document number
or date is reported ONLY when it is literally present in the retrieved
page. ``current_document_verified`` is ``true`` only when the document
was reached from the manufacturer's own current product page in this
same request; otherwise it is ``false`` and a warning is emitted. Nothing
about versioning is ever inferred.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass

from app.engines.base import RawRecord
from app.engines.domain_retrieval import (
    DiscoveredLink,
    FetchStats,
    PageContent,
    discover_links,
    fetch_page,
)
from app.security.allowlist import SourceEntry, SourceRegistry, get_source_registry
from app.security.safe_http import SafeHttpClient
from app.settings import get_settings
from app.utils.helpers import utc_now_iso
from app.utils.normalize import normalize_whitespace, truncate

logger = logging.getLogger(__name__)

PROVIDER = "Manufacturer (official domain)"

# Keywords used to find the right document for each requested type.
DOCUMENT_KEYWORDS: dict[str, list[str]] = {
    "IFU": ["instructions for use", "ifu", "directions for use", "gebrauchsinformation",
            "user manual", "instruction"],
    "technical_manual": ["technical manual", "technical guide", "scientific documentation",
                         "technical data", "manual"],
    "safety_data": ["safety data sheet", "sds", "msds", "material safety"],
    "composition": ["composition", "ingredients", "chemical composition", "formulation"],
    "indication": ["indications", "indication", "intended use"],
    "contraindication": ["contraindications", "contraindication", "warnings"],
    "surface_treatment": ["surface treatment", "conditioning", "etching", "pretreatment",
                          "surface conditioning"],
    "cementation_protocol": ["cementation", "luting", "bonding protocol", "adhesive protocol",
                             "cementation protocol"],
    "compatibility": ["compatibility", "compatible", "combination"],
    "curing_protocol": ["curing", "light curing", "polymerisation", "polymerization",
                        "curing time"],
    "storage": ["storage", "shelf life", "store at", "expiry"],
    "product_specification": ["technical data", "specifications", "product information",
                              "properties"],
    "any": ["instructions for use", "ifu", "technical", "documentation", "downloads",
            "product information"],
}

# Paths that manufacturers commonly use for document libraries. These are
# tried in order; any that 404 is skipped silently.
CANDIDATE_PATHS: tuple[str, ...] = (
    "/",
    "/en/",
    "/en/downloads",
    "/downloads",
    "/en/download-center",
    "/download-center",
    "/en/products",
    "/products",
    "/en/support",
    "/support",
    "/en/documents",
    "/documents",
    "/instructions-for-use",
    "/en/instructions-for-use",
    "/ifu",
    "/en/ifu",
)

# Patterns for version metadata. Only matched literally — never inferred.
_VERSION_PATTERNS = (
    re.compile(r"\b(?:rev(?:ision)?\.?\s*[:#]?\s*)([A-Z0-9][A-Z0-9.\-]{0,12})\b", re.IGNORECASE),
    re.compile(r"\b(?:version|ver\.?)\s*[:#]?\s*(\d+(?:\.\d+)*)\b", re.IGNORECASE),
    re.compile(r"\bdocument\s*(?:no\.?|number)\s*[:#]?\s*([A-Z0-9\-/]{3,20})\b", re.IGNORECASE),
)
_DATE_PATTERNS = (
    re.compile(r"\b(20\d{2}-\d{2}-\d{2})\b"),
    re.compile(r"\b(\d{2}/20\d{2})\b"),
    re.compile(r"\b((?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)[a-z]*\s+20\d{2})\b",
               re.IGNORECASE),
    re.compile(r"\b(20\d{2}-\d{2})\b"),
)


@dataclass
class ManufacturerSearchOutcome:
    records: list[RawRecord]
    warnings: list[str]
    resolved_manufacturer: str | None
    searched_domains: list[str]


class ManufacturerSearchEngine:
    """Domain-restricted retrieval of official manufacturer documentation."""

    def __init__(
        self,
        http: SafeHttpClient,
        registry: SourceRegistry | None = None,
    ) -> None:
        self._http = http
        self._registry = registry or get_source_registry()
        settings = get_settings()
        http.register_rate_limiter(
            "manufacturer", settings.manufacturer_rate_limit_per_second
        )

    # ------------------------------------------------------------------
    async def search(
        self,
        query: str,
        *,
        manufacturer: str | None = None,
        product_name: str | None = None,
        document_type: str = "any",
        max_results: int = 5,
    ) -> ManufacturerSearchOutcome:
        """Search official manufacturer domains for product documentation."""
        warnings: list[str] = []

        entries = self._resolve_targets(query, manufacturer, warnings)
        if not entries:
            warnings.append(
                "No allowlisted manufacturer domain could be resolved from the "
                "request. Manufacturer search is restricted to official domains "
                "on the server-side allowlist; distributor, reseller and "
                "third-party sites are never used."
            )
            return ManufacturerSearchOutcome([], warnings, None, [])

        keywords = self._keywords(query, product_name, document_type)
        records: list[RawRecord] = []
        searched: list[str] = []
        stats = FetchStats()

        for entry in entries:
            searched.append(entry.domain)
            found = await self._search_domain(
                entry, keywords, query, product_name, document_type, max_results, stats
            )
            records.extend(found)
            if len(records) >= max_results:
                break

        if not records and stats.unreachable:
            warnings.append(
                f"The official domain(s) {', '.join(searched)} could not be reached "
                "during this request. No manufacturer document was actually "
                "checked — this is a connectivity failure, not evidence that the "
                "document does not exist."
            )
        elif not records:
            warnings.append(
                f"No matching document could be retrieved from the official "
                f"domain(s) {', '.join(searched)}. This does not mean the "
                "document does not exist — many manufacturers place IFUs behind "
                "region selectors, login walls or JavaScript-driven search that "
                "this gateway deliberately does not bypass."
            )

        return ManufacturerSearchOutcome(
            records=records[:max_results],
            warnings=warnings,
            resolved_manufacturer=entries[0].key if entries else None,
            searched_domains=searched,
        )

    # ------------------------------------------------------------------
    def _resolve_targets(
        self, query: str, manufacturer: str | None, warnings: list[str]
    ) -> list[SourceEntry]:
        """Resolve the request to one or more allowlisted manufacturers."""
        if manufacturer:
            entry = self._registry.find_manufacturer(manufacturer)
            if entry:
                return [entry]
            warnings.append(
                f"Manufacturer {manufacturer!r} is not on the allowlist. "
                "No search was performed against any non-allowlisted domain."
            )
            return []

        # Infer from the query text, e.g. "Ivoclar Monobond Etch & Prime".
        matches: list[SourceEntry] = []
        lowered = query.lower()
        for entry in self._registry.manufacturers():
            label = entry.domain.split(".")[0]
            if len(label) >= 4 and label in lowered:
                matches.append(entry)
        if matches:
            return matches[:2]

        # Fall back to the brand dictionary: "IPS e.max" -> Ivoclar.
        brand_owner = self._manufacturer_from_brand(lowered)
        if brand_owner:
            entry = self._registry.find_manufacturer(brand_owner)
            if entry:
                return [entry]
        return []

    @staticmethod
    def _manufacturer_from_brand(lowered_query: str) -> str | None:
        from app.evidence.query_expander import get_query_expander

        expander = get_query_expander()
        for brand in expander._brands.values():  # noqa: SLF001 - same package
            if any(p.search(lowered_query) for p in brand.patterns):
                return brand.manufacturer
        return None

    @staticmethod
    def _keywords(
        query: str, product_name: str | None, document_type: str
    ) -> list[str]:
        keywords = list(DOCUMENT_KEYWORDS.get(document_type, DOCUMENT_KEYWORDS["any"]))
        if product_name:
            keywords.insert(0, normalize_whitespace(product_name))
        # Query tokens longer than 3 characters carry the product name in
        # the common case where the caller did not fill product_name.
        for token in normalize_whitespace(query).split():
            if len(token) > 3 and token.lower() not in {"what", "official", "instructions"}:
                keywords.append(token)
        return keywords

    async def _search_domain(
        self,
        entry: SourceEntry,
        keywords: list[str],
        query: str,
        product_name: str | None,
        document_type: str,
        max_results: int,
        stats: FetchStats | None = None,
    ) -> list[RawRecord]:
        """Walk a manufacturer's own site for candidate documents."""
        records: list[RawRecord] = []
        visited_pages: list[PageContent] = []

        for path in CANDIDATE_PATHS:
            if len(visited_pages) >= 3:
                break
            url = f"https://www.{entry.domain}{path}"
            page = await fetch_page(
                self._http, url, entry, provider="manufacturer", stats=stats
            )
            if page is None:
                # Try the apex host if the www variant failed.
                page = await fetch_page(
                    self._http, f"https://{entry.domain}{path}", entry,
                    provider="manufacturer", stats=stats,
                )
            if page is not None:
                visited_pages.append(page)

        candidates: list[DiscoveredLink] = []
        for page in visited_pages:
            candidates.extend(discover_links(page, keywords, limit=8))
        candidates.sort(key=lambda link: link.score, reverse=True)

        for link in candidates[: max_results * 2]:
            if len(records) >= max_results:
                break
            record = await self._build_record(
                entry, link, query, product_name, document_type
            )
            if record is not None:
                records.append(record)

        return records

    async def _build_record(
        self,
        entry: SourceEntry,
        link: DiscoveredLink,
        query: str,
        product_name: str | None,
        document_type: str,
    ) -> RawRecord | None:
        """Fetch a candidate document and build a record from what is there."""
        page = await fetch_page(self._http, link.url, entry, provider="manufacturer")
        if page is None:
            return None

        # PART 69: the link text is not the answer. We fetched the actual
        # document from the official domain before returning anything.
        title = (
            page.title
            or link.text
            or f"{entry.key.replace('_', ' ').title()} document"
        )

        version = None
        doc_date = None
        if page.text:
            version = _first_match(_VERSION_PATTERNS, page.text)
            doc_date = _first_match(_DATE_PATTERNS, page.text)

        key_info = _extract_key_information(page.text, document_type) if page.text else None

        return RawRecord(
            provider=PROVIDER,
            source_domain=entry.domain,
            url=page.url,
            title=normalize_whitespace(title)[:300],
            manufacturer=entry.key.replace("_", " ").title(),
            product=normalize_whitespace(product_name) if product_name else None,
            document_type=document_type if document_type != "any" else (
                "IFU" if link.is_pdf else "product_information"
            ),
            document_version=version,
            document_date=doc_date,
            # Only true when we can literally see a revision marker AND we
            # reached the document from the manufacturer's own site in this
            # request. Anything less stays false (PART 60).
            current_document_verified=bool(version) if version else False,
            abstract=key_info,
            key_findings=None,
            retrieved_at=utc_now_iso(),
            full_text_reviewed=bool(page.text) and not page.is_pdf,
            extra={
                "is_pdf": page.is_pdf,
                "discovery_score": link.score,
                "discovered_via": link.text[:120] if link.text else None,
            },
        )


def _first_match(patterns: tuple[re.Pattern[str], ...], text: str) -> str | None:
    for pattern in patterns:
        match = pattern.search(text)
        if match:
            return normalize_whitespace(match.group(1))
    return None


def _extract_key_information(text: str, document_type: str) -> str | None:
    """Pull the passage around the requested topic, verbatim.

    Returns a literal excerpt from the official page — never a summary
    the model could mistake for a manufacturer statement it did not make.
    """
    if not text:
        return None
    keywords = DOCUMENT_KEYWORDS.get(document_type, [])
    lowered = text.lower()
    for keyword in keywords:
        index = lowered.find(keyword.lower())
        if index >= 0:
            start = max(0, index - 200)
            excerpt = text[start : index + 800]
            return truncate(normalize_whitespace(excerpt), 900)
    return truncate(normalize_whitespace(text), 600) or None
