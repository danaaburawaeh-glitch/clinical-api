"""Domain-restricted retrieval (PART 30, PART 69, PART 85).

Some authoritative sources — SFDA, most manufacturers, several
professional bodies — do not publish a usable public API. Deleting those
capabilities was not an option (PART 85), and using a general web search
engine or a distributor site was explicitly forbidden (PART 86).

The compromise implemented here:

    1. Candidate URLs are generated ONLY from allowlisted official
       domains, using each organisation's documented site-search or
       document paths. No third-party search engine is consulted.
    2. Every candidate passes the full validation pipeline before it is
       fetched, and every redirect is re-validated.
    3. Retrieved HTML is parsed with the standard library only, and only
       for links and visible text. Nothing is executed.
    4. A search-result snippet is never the answer: a candidate document
       link is discovered, then fetched from the official domain, then
       parsed (PART 69).
    5. Anything that cannot be verified is returned as ``None`` with an
       explicit warning, never as a plausible guess.

This is honest "best available safe method" retrieval, and its
limitations are documented in the README rather than hidden.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from html.parser import HTMLParser
from urllib.parse import urljoin, urlparse

from app.security.allowlist import SourceEntry
from app.security.safe_http import (
    MIME_HTML,
    MIME_PDF,
    MIME_TEXT,
    SafeHttpClient,
    UpstreamError,
)
from app.security.url_validator import UrlValidationError, validate_url_sync
from app.utils.normalize import normalize_whitespace, truncate

logger = logging.getLogger(__name__)

__all__ = [
    "DiscoveredLink",
    "PageContent",
    "FetchStats",
    "fetch_page",
    "discover_links",
    "score_link",
]

_PDF_RE = re.compile(r"\.pdf($|\?)", re.IGNORECASE)


@dataclass
class DiscoveredLink:
    url: str
    text: str
    is_pdf: bool = False
    score: float = 0.0


@dataclass
class FetchStats:
    """Tally of fetch outcomes for one engine run.

    This exists to keep a critical distinction visible: "we reached the
    official site and it had nothing" is a very different answer from
    "we could not reach the official site at all". Without it, a network
    outage would be reported to the model as an absence of evidence.
    """

    attempted: int = 0
    succeeded: int = 0
    transport_failures: int = 0
    blocked: int = 0

    @property
    def all_failed(self) -> bool:
        return self.attempted > 0 and self.succeeded == 0

    @property
    def unreachable(self) -> bool:
        """True when every failure was a transport failure, not a 404."""
        return self.all_failed and self.transport_failures > 0


@dataclass
class PageContent:
    """Parsed content of one officially-hosted page."""

    url: str
    title: str | None = None
    text: str = ""
    links: list[DiscoveredLink] = field(default_factory=list)
    content_type: str = ""
    is_pdf: bool = False
    meta: dict[str, str] = field(default_factory=dict)


class _LinkAndTextParser(HTMLParser):
    """Extract links, visible text and useful meta tags.

    Deliberately minimal: no external parser dependency, no JavaScript,
    no CSS. Script and style contents are discarded.
    """

    _SKIP = {"script", "style", "noscript", "svg", "head"}

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.links: list[tuple[str, str]] = []
        self.title: str | None = None
        self.meta: dict[str, str] = {}
        self._text_parts: list[str] = []
        self._skip_depth = 0
        self._in_title = False
        self._current_href: str | None = None
        self._current_link_text: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        attr = {k.lower(): (v or "") for k, v in attrs}
        if tag in self._SKIP:
            self._skip_depth += 1
            return
        if tag == "title":
            self._in_title = True
        elif tag == "a" and attr.get("href"):
            self._current_href = attr["href"]
            self._current_link_text = []
        elif tag == "meta":
            name = (attr.get("name") or attr.get("property") or "").lower()
            if name and attr.get("content"):
                self.meta[name] = attr["content"][:400]

    def handle_endtag(self, tag: str) -> None:
        if tag in self._SKIP:
            self._skip_depth = max(0, self._skip_depth - 1)
            return
        if tag == "title":
            self._in_title = False
        elif tag == "a" and self._current_href is not None:
            self.links.append(
                (self._current_href, normalize_whitespace(" ".join(self._current_link_text)))
            )
            self._current_href = None
            self._current_link_text = []

    def handle_data(self, data: str) -> None:
        if self._skip_depth:
            return
        text = data.strip()
        if not text:
            return
        if self._in_title and self.title is None:
            self.title = normalize_whitespace(text)
        if self._current_href is not None:
            self._current_link_text.append(text)
        self._text_parts.append(text)

    @property
    def text(self) -> str:
        return normalize_whitespace(" ".join(self._text_parts))


async def fetch_page(
    http: SafeHttpClient,
    url: str,
    entry: SourceEntry,
    *,
    provider: str = "manufacturer",
    max_bytes: int | None = None,
    stats: FetchStats | None = None,
) -> PageContent | None:
    """Fetch and parse one page from an allowlisted official domain.

    Returns ``None`` (never raises) when the page cannot be retrieved, so
    that a single dead link does not fail an entire search. Pass ``stats``
    to record *why* it failed — the caller needs that to distinguish "no
    such document" from "the site was unreachable".
    """
    if stats is not None:
        stats.attempted += 1

    try:
        validate_url_sync(url, required_category=entry.category)
    except UrlValidationError as exc:
        logger.info("domain_retrieval_rejected", extra={"reason": exc.reason})
        if stats is not None:
            stats.blocked += 1
        return None

    try:
        response = await http.request(
            "GET",
            url,
            accept_mime=MIME_HTML | MIME_TEXT | MIME_PDF,
            provider=provider,
            required_category=entry.category,
            max_bytes=max_bytes,
        )
    except UrlValidationError as exc:
        logger.info("domain_retrieval_redirect_blocked", extra={"reason": exc.reason})
        if stats is not None:
            stats.blocked += 1
        return None
    except UpstreamError as exc:
        logger.info(
            "domain_retrieval_fetch_failed",
            extra={"host": urlparse(url).hostname, "reason": str(exc)[:200]},
        )
        if stats is not None:
            # A 404 is a legitimate "not here"; anything else (timeout,
            # DNS, TLS, 5xx) means we never got to look.
            if exc.status_code == 404:
                stats.succeeded += 0
            else:
                stats.transport_failures += 1
        return None

    if stats is not None:
        stats.succeeded += 1

    if response.content_type in MIME_PDF:
        # We do not extract text from PDFs in v1 (see README limitations).
        # The document is confirmed to exist on the official domain, and
        # its URL is returned — but nothing is invented about its content.
        return PageContent(
            url=response.final_url,
            title=None,
            text="",
            content_type=response.content_type,
            is_pdf=True,
        )

    parser = _LinkAndTextParser()
    try:
        parser.feed(response.text)
        parser.close()
    except Exception:  # noqa: BLE001 - malformed HTML must not crash a search
        logger.info("domain_retrieval_parse_failed", extra={"url": truncate(url, 120)})
        return PageContent(url=response.final_url, content_type=response.content_type)

    links: list[DiscoveredLink] = []
    seen: set[str] = set()
    for href, text in parser.links:
        absolute = urljoin(response.final_url, href)
        if absolute in seen:
            continue
        seen.add(absolute)
        try:
            validate_url_sync(absolute, required_category=entry.category)
        except UrlValidationError:
            continue  # silently drop off-domain links — that is the point
        links.append(
            DiscoveredLink(url=absolute, text=text, is_pdf=bool(_PDF_RE.search(absolute)))
        )

    return PageContent(
        url=response.final_url,
        title=parser.title,
        text=parser.text,
        links=links,
        content_type=response.content_type,
        meta=parser.meta,
    )


def discover_links(
    page: PageContent, keywords: list[str], *, limit: int = 10
) -> list[DiscoveredLink]:
    """Rank on-domain links by keyword relevance."""
    scored: list[DiscoveredLink] = []
    for link in page.links:
        link.score = score_link(link, keywords)
        if link.score > 0:
            scored.append(link)
    scored.sort(key=lambda link: link.score, reverse=True)
    return scored[:limit]


def score_link(link: DiscoveredLink, keywords: list[str]) -> float:
    """Score a candidate link by keyword hits in its text and URL."""
    haystack = f"{link.text} {link.url}".lower()
    score = 0.0
    for keyword in keywords:
        needle = keyword.lower().strip()
        if not needle:
            continue
        if needle in link.text.lower():
            score += 2.0
        elif needle in haystack:
            score += 1.0
    if link.is_pdf:
        score += 1.5  # IFUs and technical manuals are overwhelmingly PDFs
    return score
