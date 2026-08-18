"""PubMed engine — NCBI E-utilities (PART 7).

Official API only; PubMed is never scraped.

Flow::

    ESearch (term -> PMID list)  ->  EFetch (PMIDs -> full XML metadata)

EFetch is preferred over ESummary because only EFetch returns abstracts,
MeSH headings and the complete ``PublicationTypeList`` that the evidence
classifier depends on.

Rate limits: NCBI permits 3 requests/second without an API key and 10
with one. We pace at 3/s and 9/s respectively, leaving headroom.
"""

from __future__ import annotations

import logging
import re
from typing import Any
from xml.etree import ElementTree as ET

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

PROVIDER = "PubMed"
SOURCE_DOMAIN = "pubmed.ncbi.nlm.nih.gov"

EUTILS_BASE = "https://eutils.ncbi.nlm.nih.gov/entrez/eutils"
ESEARCH_URL = f"{EUTILS_BASE}/esearch.fcgi"
EFETCH_URL = f"{EUTILS_BASE}/efetch.fcgi"

# Restricts every search to the dental/oral literature space unless the
# caller's own query already carries a strong topical anchor. This is a
# recall filter, not a source filter — the hard source control is the
# domain allowlist.
DENTAL_SCOPE_FILTER = (
    '("Dentistry"[MeSH Terms] OR "Stomatognathic Diseases"[MeSH Terms] '
    'OR "Dental Materials"[MeSH Terms] OR "Mouth"[MeSH Terms] '
    'OR dental[Title/Abstract] OR dentistry[Title/Abstract] '
    'OR tooth[Title/Abstract] OR teeth[Title/Abstract] '
    'OR oral[Title/Abstract] OR periodont*[Title/Abstract] '
    'OR endodont*[Title/Abstract] OR orthodont*[Title/Abstract] '
    'OR prosthodont*[Title/Abstract] OR implant*[Title/Abstract] '
    'OR ceramic*[Title/Abstract] OR enamel[Title/Abstract] '
    'OR dentin*[Title/Abstract])'
)

# StudyDesign -> PubMed publication-type / filter fragments.
DESIGN_FILTERS: dict[str, str] = {
    "guideline": '("Practice Guideline"[Publication Type] OR "Guideline"[Publication Type])',
    "consensus": '"Consensus Development Conference"[Publication Type]',
    "systematic_review": '("Systematic Review"[Publication Type] OR systematic review[Title])',
    "meta_analysis": '"Meta-Analysis"[Publication Type]',
    "randomized_controlled_trial": '"Randomized Controlled Trial"[Publication Type]',
    "clinical_trial": '"Clinical Trial"[Publication Type]',
    "cohort": '("Cohort Studies"[MeSH Terms] OR cohort[Title/Abstract])',
    "case_control": '"Case-Control Studies"[MeSH Terms]',
    "cross_sectional": '"Cross-Sectional Studies"[MeSH Terms]',
    "diagnostic_accuracy": '("Sensitivity and Specificity"[MeSH Terms] '
                           'OR diagnostic accuracy[Title/Abstract])',
    "laboratory": '("In Vitro Techniques"[MeSH Terms] OR in vitro[Title/Abstract])',
    "case_series": 'case series[Title/Abstract]',
    "case_report": '"Case Reports"[Publication Type]',
}

_MONTHS = {
    "jan": 1, "feb": 2, "mar": 3, "apr": 4, "may": 5, "jun": 6,
    "jul": 7, "aug": 8, "sep": 9, "oct": 10, "nov": 11, "dec": 12,
}
_YEAR_RE = re.compile(r"(1[6-9]\d{2}|20\d{2}|21\d{2})")


class PubMedEngine:
    """Async client for NCBI E-utilities."""

    def __init__(self, http: SafeHttpClient) -> None:
        self._http = http
        settings = get_settings()
        self._settings = settings
        rate = (
            settings.pubmed_rate_limit_per_second_with_key
            if settings.ncbi_api_key
            else settings.pubmed_rate_limit_per_second
        )
        http.register_rate_limiter("pubmed", rate)

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------
    async def search(
        self,
        term: str,
        *,
        max_results: int = 10,
        date_from: int | None = None,
        date_to: int | None = None,
        study_designs: list[str] | None = None,
        apply_dental_scope: bool = True,
    ) -> list[RawRecord]:
        """Search PubMed and return fully parsed records."""
        query = self.build_query(
            term,
            date_from=date_from,
            date_to=date_to,
            study_designs=study_designs,
            apply_dental_scope=apply_dental_scope,
        )
        pmids = await self.esearch(query, retmax=max_results)
        if not pmids:
            return []
        return await self.efetch(pmids)

    def build_query(
        self,
        term: str,
        *,
        date_from: int | None = None,
        date_to: int | None = None,
        study_designs: list[str] | None = None,
        apply_dental_scope: bool = True,
    ) -> str:
        """Assemble a PubMed boolean query string."""
        clauses: list[str] = []

        core = normalize_whitespace(term)
        if not core:
            raise EngineError(PROVIDER, "empty search term", retryable=False)
        clauses.append(f"({core})")

        if apply_dental_scope:
            clauses.append(DENTAL_SCOPE_FILTER)

        if study_designs:
            fragments = [DESIGN_FILTERS[d] for d in study_designs if d in DESIGN_FILTERS]
            if fragments:
                clauses.append("(" + " OR ".join(fragments) + ")")

        if date_from or date_to:
            lo = date_from or 1900
            hi = date_to or 3000
            clauses.append(f'("{lo}"[Date - Publication] : "{hi}"[Date - Publication])')

        return " AND ".join(clauses)

    async def esearch(self, query: str, retmax: int = 10) -> list[str]:
        """Run ESearch and return a PMID list."""
        params = self._base_params()
        params.update(
            {
                "db": "pubmed",
                "term": query,
                "retmode": "json",
                "retmax": str(max(1, min(retmax, 100))),
                "sort": "relevance",
            }
        )
        payload = await self._request_json(ESEARCH_URL, params)
        result = (payload or {}).get("esearchresult") or {}

        if "ERROR" in result:
            raise EngineError(PROVIDER, f"ESearch error: {result['ERROR']}", retryable=False)

        idlist = result.get("idlist") or []
        return [pid for pid in (normalize_pmid(i) for i in idlist) if pid]

    async def efetch(self, pmids: list[str]) -> list[RawRecord]:
        """Run EFetch for a PMID list and parse the returned XML."""
        if not pmids:
            return []
        params = self._base_params()
        params.update({"db": "pubmed", "id": ",".join(pmids), "retmode": "xml"})
        xml_text = await self._request_xml(EFETCH_URL, params)
        return self.parse_efetch_xml(xml_text)

    # ------------------------------------------------------------------
    # Parsing
    # ------------------------------------------------------------------
    @staticmethod
    def parse_efetch_xml(xml_text: str) -> list[RawRecord]:
        """Parse a PubmedArticleSet document into :class:`RawRecord` objects.

        Written defensively: any single malformed article is skipped
        rather than failing the whole batch.
        """
        if not xml_text or not xml_text.strip():
            return []
        try:
            root = ET.fromstring(xml_text)
        except ET.ParseError as exc:
            raise EngineError(PROVIDER, f"malformed EFetch XML: {exc}", retryable=True) from exc

        records: list[RawRecord] = []
        for article in root.iter("PubmedArticle"):
            try:
                record = PubMedEngine._parse_article(article)
            except Exception:  # noqa: BLE001 - never let one record kill the batch
                logger.exception("pubmed_article_parse_failed")
                continue
            if record is not None:
                records.append(record)

        # Books/chapters indexed in PubMed appear under a different root.
        for book in root.iter("PubmedBookArticle"):
            try:
                record = PubMedEngine._parse_book(book)
            except Exception:  # noqa: BLE001
                logger.exception("pubmed_book_parse_failed")
                continue
            if record is not None:
                records.append(record)

        return records

    @staticmethod
    def _parse_article(article: ET.Element) -> RawRecord | None:
        medline = article.find("MedlineCitation")
        if medline is None:
            return None

        pmid = normalize_pmid(_text(medline.find("PMID")))
        art = medline.find("Article")
        if art is None:
            return None

        title = _collect_text(art.find("ArticleTitle"))
        if not title:
            title = "[No title provided by source]"

        journal_el = art.find("Journal")
        journal = None
        year: int | None = None
        if journal_el is not None:
            journal = (
                _text(journal_el.find("Title"))
                or _text(journal_el.find("ISOAbbreviation"))
            )
            year = PubMedEngine._extract_year(journal_el)

        if year is None:
            year = PubMedEngine._extract_year_from_pubdate(article)

        abstract = PubMedEngine._extract_abstract(art)
        authors = PubMedEngine._extract_authors(art)

        pub_types = [
            normalize_whitespace(pt.text)
            for pt in art.findall("./PublicationTypeList/PublicationType")
            if pt.text
        ]

        mesh_terms = [
            normalize_whitespace(d.text)
            for d in medline.findall("./MeshHeadingList/MeshHeading/DescriptorName")
            if d.text
        ]

        keywords = [
            normalize_whitespace(k.text)
            for k in medline.findall("./KeywordList/Keyword")
            if k.text
        ]

        language = _text(art.find("Language"))

        doi = None
        pmcid = None
        for aid in article.findall("./PubmedData/ArticleIdList/ArticleId"):
            id_type = (aid.get("IdType") or "").lower()
            value = normalize_whitespace(aid.text)
            if id_type == "doi" and doi is None:
                doi = normalize_doi(value)
            elif id_type == "pmc" and pmcid is None:
                pmcid = normalize_pmcid(value)
        if doi is None:
            for elocation in art.findall("ELocationID"):
                if (elocation.get("EIdType") or "").lower() == "doi":
                    doi = normalize_doi(elocation.text)
                    if doi:
                        break

        # PubMed also encodes retraction linkage in CommentsCorrections.
        integrity_notes: list[str] = []
        for cc in medline.findall("./CommentsCorrectionsList/CommentsCorrections"):
            ref_type = cc.get("RefType") or ""
            if ref_type:
                integrity_notes.append(ref_type)

        return RawRecord(
            provider=PROVIDER,
            source_domain=SOURCE_DOMAIN,
            url=f"https://{SOURCE_DOMAIN}/{pmid}/" if pmid else None,
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
            extra={"comments_corrections": integrity_notes},
        )

    @staticmethod
    def _parse_book(book: ET.Element) -> RawRecord | None:
        doc = book.find("BookDocument")
        if doc is None:
            return None
        pmid = normalize_pmid(_text(doc.find("PMID")))
        title = _collect_text(doc.find("ArticleTitle")) or _collect_text(
            doc.find("./Book/BookTitle")
        )
        if not title:
            return None
        year = safe_int(_text(doc.find("./Book/PubDate/Year")), 1500, 2200)
        abstract = PubMedEngine._extract_abstract(doc)
        pub_types = [
            normalize_whitespace(pt.text)
            for pt in doc.findall("./PublicationTypeList/PublicationType")
            if pt.text
        ]
        return RawRecord(
            provider=PROVIDER,
            source_domain=SOURCE_DOMAIN,
            url=f"https://{SOURCE_DOMAIN}/{pmid}/" if pmid else None,
            title=title,
            publication_year=year,
            abstract=abstract,
            pmid=pmid,
            publication_types=pub_types or ["Book"],
        )

    @staticmethod
    def _extract_abstract(parent: ET.Element) -> str | None:
        """Concatenate structured abstract sections, preserving labels."""
        node = parent.find("Abstract")
        if node is None:
            return None
        pieces: list[str] = []
        for section in node.findall("AbstractText"):
            text = _collect_text(section)
            if not text:
                continue
            label = section.get("Label")
            pieces.append(f"{label.strip()}: {text}" if label else text)
        combined = strip_html(" ".join(pieces))
        return combined or None

    @staticmethod
    def _extract_authors(art: ET.Element) -> list[str]:
        authors: list[str] = []
        for author in art.findall("./AuthorList/Author"):
            collective = _text(author.find("CollectiveName"))
            if collective:
                authors.append(collective)
                continue
            last = _text(author.find("LastName"))
            initials = _text(author.find("Initials"))
            fore = _text(author.find("ForeName"))
            if last and initials:
                authors.append(f"{last} {initials}")
            elif last and fore:
                authors.append(f"{last} {fore}")
            elif last:
                authors.append(last)
        return authors

    @staticmethod
    def _extract_year(journal_el: ET.Element) -> int | None:
        pubdate = journal_el.find("./JournalIssue/PubDate")
        if pubdate is None:
            return None
        year = safe_int(_text(pubdate.find("Year")), 1500, 2200)
        if year:
            return year
        medline_date = _text(pubdate.find("MedlineDate"))
        if medline_date:
            match = _YEAR_RE.search(medline_date)
            if match:
                return safe_int(match.group(1), 1500, 2200)
        return None

    @staticmethod
    def _extract_year_from_pubdate(article: ET.Element) -> int | None:
        for path in ("./PubmedData/History/PubMedPubDate[@PubStatus='pubmed']/Year",
                     "./PubmedData/History/PubMedPubDate/Year"):
            node = article.find(path)
            if node is not None:
                year = safe_int(node.text, 1500, 2200)
                if year:
                    return year
        return None

    # ------------------------------------------------------------------
    # Transport
    # ------------------------------------------------------------------
    def _base_params(self) -> dict[str, str]:
        params = {"tool": self._settings.ncbi_tool_name}
        if self._settings.ncbi_contact_email:
            params["email"] = self._settings.ncbi_contact_email
        if self._settings.ncbi_api_key:
            params["api_key"] = self._settings.ncbi_api_key
        return params

    async def _request_json(self, url: str, params: dict[str, str]) -> Any:
        try:
            return await self._http.get_json(url, params=params, provider="pubmed")
        except UpstreamTimeout as exc:
            raise EngineError(PROVIDER, "timeout", retryable=True) from exc
        except UpstreamRateLimited as exc:
            raise EngineError(PROVIDER, "rate limited by NCBI", retryable=True) from exc
        except (UpstreamError, UrlValidationError, ValueError) as exc:
            raise EngineError(PROVIDER, str(exc), retryable=True) from exc

    async def _request_xml(self, url: str, params: dict[str, str]) -> str:
        try:
            return await self._http.get_xml(url, params=params, provider="pubmed")
        except UpstreamTimeout as exc:
            raise EngineError(PROVIDER, "timeout", retryable=True) from exc
        except UpstreamRateLimited as exc:
            raise EngineError(PROVIDER, "rate limited by NCBI", retryable=True) from exc
        except (UpstreamError, UrlValidationError) as exc:
            raise EngineError(PROVIDER, str(exc), retryable=True) from exc


# ----------------------------------------------------------------------
# XML helpers
# ----------------------------------------------------------------------
def _text(element: ET.Element | None) -> str | None:
    if element is None:
        return None
    value = normalize_whitespace(element.text or "")
    return value or None


def _collect_text(element: ET.Element | None) -> str:
    """Return the full text of an element including nested markup."""
    if element is None:
        return ""
    return normalize_whitespace("".join(element.itertext()))
