"""Europe PMC engine (PART 8).

Uses the official Europe PMC RESTful Web Service::

    https://www.ebi.ac.uk/europepmc/webservices/rest/search

Three roles:
  1. an independent search provider (different ranking, wider coverage
     of European and open-access dental literature than PubMed alone);
  2. a metadata completer (PMCID, open-access status, DOI backfill);
  3. a route to legally available full text via PMC.

Critical rule (PART 8): a study appearing in both PubMed and Europe PMC
is ONE study. The deduplicator merges them and the ranker never rewards
a record for having more providers.
"""

from __future__ import annotations

import logging
from typing import Any

from app.engines.base import EngineError, RawRecord
from app.security.safe_http import (
    SafeHttpClient,
    UpstreamError,
    UpstreamRateLimited,
    UpstreamTimeout,
)
from app.security.url_validator import UrlValidationError
from app.settings import get_settings
from app.utils.normalize import (
    normalize_doi,
    normalize_pmcid,
    normalize_pmid,
    normalize_whitespace,
    safe_int,
    strip_html,
)

logger = logging.getLogger(__name__)

PROVIDER = "Europe PMC"
SOURCE_DOMAIN = "europepmc.org"
API_HOST = "www.ebi.ac.uk"
SEARCH_URL = f"https://{API_HOST}/europepmc/webservices/rest/search"

# Europe PMC pubType strings are lower-case and comma-separated; map the
# ones that carry classification signal onto NCBI-style labels so the
# classifier has a single vocabulary to reason about.
_PUBTYPE_NORMALISATION = {
    "journal article": "Journal Article",
    "research-article": "Journal Article",
    "review": "Review",
    "review-article": "Review",
    "systematic review": "Systematic Review",
    "meta-analysis": "Meta-Analysis",
    "randomized controlled trial": "Randomized Controlled Trial",
    "controlled clinical trial": "Controlled Clinical Trial",
    "clinical trial": "Clinical Trial",
    "case reports": "Case Reports",
    "case-report": "Case Reports",
    "practice guideline": "Practice Guideline",
    "guideline": "Guideline",
    "consensus development conference": "Consensus Development Conference",
    "observational study": "Observational Study",
    "multicenter study": "Multicenter Study",
    "comparative study": "Comparative Study",
    "editorial": "Editorial",
    "letter": "Letter",
    "comment": "Comment",
    "published erratum": "Published Erratum",
    "retracted publication": "Retracted Publication",
    "retraction of publication": "Retraction of Publication",
    "expression of concern": "Expression of Concern",
    "preprint": "Preprint",
}


class EuropePmcEngine:
    """Async client for the Europe PMC REST API."""

    def __init__(self, http: SafeHttpClient) -> None:
        self._http = http
        settings = get_settings()
        http.register_rate_limiter("europepmc", settings.europepmc_rate_limit_per_second)

    # ------------------------------------------------------------------
    async def search(
        self,
        term: str,
        *,
        max_results: int = 10,
        date_from: int | None = None,
        date_to: int | None = None,
        open_access_only: bool = False,
    ) -> list[RawRecord]:
        """Search Europe PMC and return parsed records."""
        query = self.build_query(
            term, date_from=date_from, date_to=date_to, open_access_only=open_access_only
        )
        params = {
            "query": query,
            "format": "json",
            "resultType": "core",
            "pageSize": str(max(1, min(max_results, 100))),
            "sort": "CITED desc" if max_results > 20 else "",
        }
        params = {k: v for k, v in params.items() if v}

        payload = await self._request_json(params)
        return self.parse_search_payload(payload)

    def build_query(
        self,
        term: str,
        *,
        date_from: int | None = None,
        date_to: int | None = None,
        open_access_only: bool = False,
    ) -> str:
        core = normalize_whitespace(term)
        if not core:
            raise EngineError(PROVIDER, "empty search term", retryable=False)

        clauses = [f"({core})"]
        if date_from or date_to:
            lo = date_from or 1900
            hi = date_to or 3000
            clauses.append(f"(FIRST_PDATE:[{lo}-01-01 TO {hi}-12-31])")
        if open_access_only:
            clauses.append("(OPEN_ACCESS:y)")
        # Exclude preprints from evidence search by default: they are not
        # peer reviewed and must not be ranked beside published trials.
        clauses.append("NOT (SRC:PPR)")
        return " AND ".join(clauses)

    async def fetch_by_doi(self, doi: str) -> RawRecord | None:
        """Look up a single record by DOI (metadata completion)."""
        normalised = normalize_doi(doi)
        if not normalised:
            return None
        payload = await self._request_json(
            {"query": f'DOI:"{normalised}"', "format": "json",
             "resultType": "core", "pageSize": "1"}
        )
        records = self.parse_search_payload(payload)
        return records[0] if records else None

    async def fetch_by_pmid(self, pmid: str) -> RawRecord | None:
        """Look up a single record by PMID (metadata completion)."""
        normalised = normalize_pmid(pmid)
        if not normalised:
            return None
        payload = await self._request_json(
            {"query": f"EXT_ID:{normalised} AND SRC:MED", "format": "json",
             "resultType": "core", "pageSize": "1"}
        )
        records = self.parse_search_payload(payload)
        return records[0] if records else None

    # ------------------------------------------------------------------
    # Parsing
    # ------------------------------------------------------------------
    @staticmethod
    def parse_search_payload(payload: Any) -> list[RawRecord]:
        if not isinstance(payload, dict):
            return []
        result_list = payload.get("resultList") or {}
        results = result_list.get("result") or []
        if not isinstance(results, list):
            return []

        records: list[RawRecord] = []
        for item in results:
            if not isinstance(item, dict):
                continue
            try:
                record = EuropePmcEngine._parse_result(item)
            except Exception:  # noqa: BLE001
                logger.exception("europepmc_result_parse_failed")
                continue
            if record is not None:
                records.append(record)
        return records

    @staticmethod
    def _parse_result(item: dict) -> RawRecord | None:
        title = strip_html(item.get("title")) or "[No title provided by source]"

        pmid = normalize_pmid(item.get("pmid"))
        pmcid = normalize_pmcid(item.get("pmcid"))
        doi = normalize_doi(item.get("doi"))

        authors = EuropePmcEngine._parse_authors(item)
        journal = EuropePmcEngine._parse_journal(item)
        year = safe_int(item.get("pubYear"), 1500, 2200)
        abstract = strip_html(item.get("abstractText")) or None
        language = normalize_whitespace(item.get("language")) or None

        pub_types = EuropePmcEngine._parse_pub_types(item)

        open_access = None
        if item.get("isOpenAccess") is not None:
            open_access = str(item.get("isOpenAccess")).upper() == "Y"

        keywords = [
            normalize_whitespace(k)
            for k in (item.get("keywordList") or {}).get("keyword", []) or []
            if normalize_whitespace(k)
        ]

        mesh_terms = [
            normalize_whitespace(h.get("descriptorName"))
            for h in (item.get("meshHeadingList") or {}).get("meshHeading", []) or []
            if isinstance(h, dict) and normalize_whitespace(h.get("descriptorName"))
        ]

        url = EuropePmcEngine._build_url(item, pmid, pmcid)

        # Europe PMC surfaces the retraction relationship directly.
        integrity_hints: list[str] = []
        comment_list = (item.get("commentCorrectionList") or {}).get(
            "commentCorrection", []
        ) or []
        for entry in comment_list:
            if isinstance(entry, dict) and entry.get("type"):
                integrity_hints.append(str(entry["type"]))

        return RawRecord(
            provider=PROVIDER,
            source_domain=SOURCE_DOMAIN,
            url=url,
            title=title,
            authors=authors,
            journal=journal,
            publication_year=year,
            abstract=abstract,
            language=language,
            pmid=pmid,
            pmcid=pmcid,
            doi=doi,
            publication_types=pub_types,
            mesh_terms=mesh_terms,
            keywords=keywords,
            open_access=open_access,
            extra={
                "europepmc_source": item.get("source"),
                "europepmc_id": item.get("id"),
                "comments_corrections": integrity_hints,
                "cited_by_count": safe_int(item.get("citedByCount")),
            },
        )

    @staticmethod
    def _parse_authors(item: dict) -> list[str]:
        authors: list[str] = []
        author_list = (item.get("authorList") or {}).get("author", []) or []
        for author in author_list:
            if not isinstance(author, dict):
                continue
            name = (
                normalize_whitespace(author.get("fullName"))
                or normalize_whitespace(author.get("collectiveName"))
            )
            if not name:
                last = normalize_whitespace(author.get("lastName"))
                initials = normalize_whitespace(author.get("initials"))
                name = f"{last} {initials}".strip()
            if name:
                authors.append(name)
        if not authors:
            raw = normalize_whitespace(item.get("authorString"))
            if raw:
                authors = [a.strip() for a in raw.rstrip(".").split(",") if a.strip()]
        return authors

    @staticmethod
    def _parse_journal(item: dict) -> str | None:
        info = item.get("journalInfo") or {}
        journal = info.get("journal") or {}
        title = normalize_whitespace(journal.get("title")) or normalize_whitespace(
            journal.get("medlineAbbreviation")
        )
        if not title:
            book = item.get("bookOrReportDetails")
            if isinstance(book, dict):
                title = normalize_whitespace(book.get("publisher"))
        return title or None

    @staticmethod
    def _parse_pub_types(item: dict) -> list[str]:
        raw_types: list[str] = []
        pub_type_list = item.get("pubTypeList") or {}
        values = pub_type_list.get("pubType", []) or []
        if isinstance(values, str):
            values = [values]
        for value in values:
            text = normalize_whitespace(str(value)).lower()
            if not text:
                continue
            raw_types.append(_PUBTYPE_NORMALISATION.get(text, text.title()))
        return raw_types

    @staticmethod
    def _build_url(item: dict, pmid: str | None, pmcid: str | None) -> str | None:
        """Build a URL that is guaranteed to sit on an allowlisted domain.

        We deliberately do NOT trust ``fullTextUrlList`` entries, which
        frequently point at publisher or aggregator domains outside the
        allowlist. Building the URL ourselves keeps every emitted link
        inside the approved set by construction.
        """
        source = normalize_whitespace(item.get("source"))
        ext_id = normalize_whitespace(item.get("id"))
        if source and ext_id:
            return f"https://{SOURCE_DOMAIN}/article/{source}/{ext_id}"
        if pmcid:
            return f"https://{SOURCE_DOMAIN}/article/PMC/{pmcid}"
        if pmid:
            return f"https://{SOURCE_DOMAIN}/article/MED/{pmid}"
        return None

    # ------------------------------------------------------------------
    async def _request_json(self, params: dict[str, str]) -> Any:
        try:
            return await self._http.get_json(SEARCH_URL, params=params, provider="europepmc")
        except UpstreamTimeout as exc:
            raise EngineError(PROVIDER, "timeout", retryable=True) from exc
        except UpstreamRateLimited as exc:
            raise EngineError(PROVIDER, "rate limited", retryable=True) from exc
        except (UpstreamError, UrlValidationError, ValueError) as exc:
            raise EngineError(PROVIDER, str(exc), retryable=True) from exc
