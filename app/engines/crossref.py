"""Crossref engine — metadata validation only (PART 9).

Crossref is used as a *validator*, never as a clinical authority. It
answers three questions:

  * does this DOI actually exist, and does its metadata agree with what
    PubMed / Europe PMC told us?
  * has an update been registered against it (retraction, correction,
    expression of concern)?
  * can a missing journal title / year / author list be filled in from
    an authoritative registry rather than guessed?

``forbidden_for: [clinical_recommendation, evidence_grading]`` in
sources.yaml is enforced structurally: this engine never emits a record
that enters the ranked result list on its own.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any

from app.engines.base import EngineError
from app.security.safe_http import (
    SafeHttpClient,
    UpstreamError,
    UpstreamRateLimited,
    UpstreamTimeout,
)
from app.security.url_validator import UrlValidationError
from app.settings import get_settings
from app.utils.normalize import normalize_doi, normalize_whitespace, safe_int, strip_html

logger = logging.getLogger(__name__)

PROVIDER = "Crossref"
SOURCE_DOMAIN = "crossref.org"
WORKS_URL = "https://api.crossref.org/works"

# Crossref "update-to" relationship types that carry integrity meaning.
_INTEGRITY_UPDATE_TYPES = {
    "retraction": "retracted",
    "withdrawal": "retracted",
    "removal": "retracted",
    "expression_of_concern": "expression_of_concern",
    "correction": "correction",
    "corrigendum": "correction",
    "erratum": "erratum",
    "addendum": "update",
    "new_edition": "update",
    "new_version": "update",
    "partial_retraction": "retracted",
}


@dataclass
class CrossrefValidation:
    """Result of validating one DOI against Crossref."""

    doi: str
    found: bool = False
    title: str | None = None
    journal: str | None = None
    publication_year: int | None = None
    authors: list[str] = field(default_factory=list)
    type: str | None = None
    publisher: str | None = None
    issn: list[str] = field(default_factory=list)
    title_matches: bool | None = None
    year_matches: bool | None = None
    integrity_status: str | None = None
    integrity_notes: list[str] = field(default_factory=list)
    is_retracted: bool = False
    metadata_consistent: bool | None = None


class CrossrefEngine:
    """Async client for the Crossref REST API."""

    def __init__(self, http: SafeHttpClient) -> None:
        self._http = http
        settings = get_settings()
        self._settings = settings
        http.register_rate_limiter("crossref", settings.crossref_rate_limit_per_second)

    # ------------------------------------------------------------------
    async def validate_doi(
        self,
        doi: str,
        *,
        expected_title: str | None = None,
        expected_year: int | None = None,
    ) -> CrossrefValidation:
        """Fetch and validate a single DOI."""
        normalised = normalize_doi(doi)
        if not normalised:
            return CrossrefValidation(doi=str(doi), found=False)

        payload = await self._request_json(f"{WORKS_URL}/{normalised}", None)
        message = (payload or {}).get("message")
        if not isinstance(message, dict):
            return CrossrefValidation(doi=normalised, found=False)

        return self.parse_work(
            message,
            expected_title=expected_title,
            expected_year=expected_year,
        )

    async def search_metadata(self, query: str, rows: int = 5) -> list[CrossrefValidation]:
        """Bibliographic search — used only for metadata completion."""
        params = {
            "query.bibliographic": normalize_whitespace(query),
            "rows": str(max(1, min(rows, 20))),
            "select": (
                "DOI,title,container-title,issued,author,type,publisher,ISSN,update-to"
            ),
        }
        if self._settings.crossref_mailto:
            params["mailto"] = self._settings.crossref_mailto

        payload = await self._request_json(WORKS_URL, params)
        items = ((payload or {}).get("message") or {}).get("items") or []
        results: list[CrossrefValidation] = []
        for item in items:
            if isinstance(item, dict):
                try:
                    results.append(self.parse_work(item))
                except Exception:  # noqa: BLE001
                    logger.exception("crossref_item_parse_failed")
        return results

    # ------------------------------------------------------------------
    @staticmethod
    def parse_work(
        message: dict,
        *,
        expected_title: str | None = None,
        expected_year: int | None = None,
    ) -> CrossrefValidation:
        from app.utils.normalize import title_similarity

        doi = normalize_doi(message.get("DOI")) or normalize_whitespace(message.get("DOI"))
        titles = message.get("title") or []
        title = strip_html(titles[0]) if titles else None

        containers = message.get("container-title") or []
        journal = normalize_whitespace(containers[0]) if containers else None

        year = CrossrefEngine._extract_year(message)

        authors: list[str] = []
        for author in message.get("author") or []:
            if not isinstance(author, dict):
                continue
            name = normalize_whitespace(author.get("name"))
            if not name:
                family = normalize_whitespace(author.get("family"))
                given = normalize_whitespace(author.get("given"))
                name = f"{family} {given}".strip()
            if name:
                authors.append(name)

        issns = [normalize_whitespace(i) for i in (message.get("ISSN") or []) if i]

        integrity_status, integrity_notes, is_retracted = CrossrefEngine._extract_integrity(
            message
        )

        title_matches = None
        if expected_title and title:
            title_matches = title_similarity(expected_title, title) >= 0.75

        year_matches = None
        if expected_year and year:
            # A one-year drift is normal between online-first and issue
            # publication; it is not a metadata inconsistency.
            year_matches = abs(expected_year - year) <= 1

        consistent: bool | None = None
        checks = [c for c in (title_matches, year_matches) if c is not None]
        if checks:
            consistent = all(checks)

        return CrossrefValidation(
            doi=doi or "",
            found=True,
            title=title,
            journal=journal,
            publication_year=year,
            authors=authors,
            type=normalize_whitespace(message.get("type")) or None,
            publisher=normalize_whitespace(message.get("publisher")) or None,
            issn=issns,
            title_matches=title_matches,
            year_matches=year_matches,
            integrity_status=integrity_status,
            integrity_notes=integrity_notes,
            is_retracted=is_retracted,
            metadata_consistent=consistent,
        )

    @staticmethod
    def _extract_year(message: dict) -> int | None:
        for key in ("issued", "published-print", "published-online", "created"):
            node = message.get(key)
            if isinstance(node, dict):
                parts = node.get("date-parts") or []
                if parts and isinstance(parts[0], list) and parts[0]:
                    year = safe_int(parts[0][0], 1500, 2200)
                    if year:
                        return year
        return None

    @staticmethod
    def _extract_integrity(message: dict) -> tuple[str | None, list[str], bool]:
        """Read Crossref ``update-to`` relations for integrity signals."""
        notes: list[str] = []
        statuses: list[str] = []

        for update in message.get("update-to") or []:
            if not isinstance(update, dict):
                continue
            raw_type = normalize_whitespace(update.get("type")).lower().replace("-", "_")
            mapped = _INTEGRITY_UPDATE_TYPES.get(raw_type)
            label = normalize_whitespace(update.get("label")) or raw_type
            if mapped:
                statuses.append(mapped)
                notes.append(f"Crossref update relation: {label}")

        # An article that *is* a retraction notice also matters.
        work_type = normalize_whitespace(message.get("type")).lower()
        if work_type in {"retraction", "correction"}:
            statuses.append("retracted" if work_type == "retraction" else "correction")
            notes.append(f"Crossref work type: {work_type}")

        if not statuses:
            return None, notes, False

        priority = ["retracted", "expression_of_concern", "correction", "erratum", "update"]
        for candidate in priority:
            if candidate in statuses:
                return candidate, notes, candidate == "retracted"
        return statuses[0], notes, False

    # ------------------------------------------------------------------
    async def _request_json(self, url: str, params: dict[str, str] | None) -> Any:
        headers = {}
        if self._settings.crossref_mailto:
            headers["Mailto"] = self._settings.crossref_mailto
        try:
            return await self._http.get_json(
                url, params=params, provider="crossref", headers=headers or None
            )
        except UpstreamTimeout as exc:
            raise EngineError(PROVIDER, "timeout", retryable=True) from exc
        except UpstreamRateLimited as exc:
            raise EngineError(PROVIDER, "rate limited", retryable=True) from exc
        except (UpstreamError, UrlValidationError, ValueError) as exc:
            raise EngineError(PROVIDER, str(exc), retryable=True) from exc
