"""Normalisation helpers for identifiers, hostnames and free text.

Correct normalisation is a security control here, not a convenience:
hostname normalisation feeds the allowlist, and DOI/title normalisation
feeds deduplication.
"""

from __future__ import annotations

import html
import re
import unicodedata
from urllib.parse import urlsplit

__all__ = [
    "normalize_hostname",
    "normalize_doi",
    "normalize_pmid",
    "normalize_pmcid",
    "normalize_title",
    "normalize_whitespace",
    "strip_html",
    "title_similarity",
    "safe_int",
    "truncate",
]

_WS_RE = re.compile(r"\s+")
_TAG_RE = re.compile(r"<[^>]+>")
_NON_ALNUM_RE = re.compile(r"[^a-z0-9 ]+")
_DOI_PREFIX_RE = re.compile(
    r"^(?:https?://)?(?:dx\.)?(?:doi\.org/|doi:\s*|info:doi/)", re.IGNORECASE
)
_PMID_RE = re.compile(r"(\d{1,9})")
_PMCID_RE = re.compile(r"PMC(\d+)", re.IGNORECASE)

# Words removed before computing title similarity. Kept deliberately
# short: aggressive stop-word removal creates false duplicate matches.
_TITLE_STOPWORDS = frozenset(
    {"a", "an", "the", "of", "on", "in", "for", "and", "with", "to", "at", "by"}
)


def normalize_whitespace(value: str | None) -> str:
    """Collapse all whitespace runs to a single space and strip."""
    if not value:
        return ""
    return _WS_RE.sub(" ", value).strip()


def strip_html(value: str | None) -> str:
    """Remove HTML/XML tags and unescape entities.

    Used on abstracts, which frequently contain inline markup such as
    ``<i>`` or ``<sub>`` in both PubMed and Europe PMC payloads.
    """
    if not value:
        return ""
    text = _TAG_RE.sub(" ", value)
    text = html.unescape(text)
    return normalize_whitespace(text)


def normalize_hostname(url_or_host: str | None) -> str:
    """Return the lower-cased, punycode-normalised hostname of a URL.

    Handles the tricks that naive ``in``-based allowlists fall for:
    trailing dots (``pubmed.ncbi.nlm.nih.gov.``), uppercase, userinfo
    (``https://pubmed.ncbi.nlm.nih.gov@evil.com``), percent-encoding and
    unicode homoglyph/IDN forms.

    Returns an empty string when no hostname can be determined.
    """
    if not url_or_host:
        return ""

    candidate = url_or_host.strip()
    if not candidate:
        return ""

    # Bare host (no scheme, no slash) -> parse via a synthetic scheme so
    # that urlsplit populates .hostname consistently.
    if "://" not in candidate:
        candidate = "//" + candidate.lstrip("/")

    try:
        parts = urlsplit(candidate)
        host = parts.hostname or ""
    except ValueError:
        return ""

    if not host:
        return ""

    # A legitimate hostname never contains a percent sign. Rejecting it
    # outright is safer than decoding: decoding would make the validator
    # reason about a different string from the one the HTTP client
    # actually resolves.
    if "%" in host:
        return ""

    # NFKC folds unicode look-alikes into their canonical form so that a
    # homoglyph domain cannot masquerade as an approved one.
    host = unicodedata.normalize("NFKC", host)
    host = host.strip().strip(".").lower()

    if not host:
        return ""

    # IDN -> punycode. If the label set is not encodable, the host is
    # not a legitimate domain and we refuse it.
    if any(ord(ch) > 127 for ch in host):
        try:
            host = host.encode("idna").decode("ascii").lower()
        except (UnicodeError, UnicodeDecodeError):
            return ""

    if "/" in host or "\\" in host or " " in host:
        return ""

    return host


def normalize_doi(doi: str | None) -> str | None:
    """Return a bare, lower-cased DOI (``10.xxxx/yyyy``) or ``None``.

    Never invents a DOI. If the input does not look like a DOI, ``None``
    is returned so the caller emits ``null`` rather than a guess.
    """
    if not doi:
        return None
    value = normalize_whitespace(str(doi))
    value = _DOI_PREFIX_RE.sub("", value)
    value = value.strip().strip(".").rstrip("/")
    value = value.lower()
    if not value.startswith("10."):
        return None
    if "/" not in value:
        return None
    # Strip trailing punctuation that commonly leaks in from text.
    value = value.rstrip(").,;:")
    return value or None


def normalize_pmid(pmid: str | int | None) -> str | None:
    """Return a bare numeric PMID string, or ``None``."""
    if pmid is None:
        return None
    text = str(pmid).strip()
    if not text:
        return None
    match = _PMID_RE.search(text)
    if not match:
        return None
    value = match.group(1).lstrip("0") or "0"
    return value if value != "0" else None


def normalize_pmcid(pmcid: str | None) -> str | None:
    """Return a canonical ``PMC#######`` identifier, or ``None``."""
    if not pmcid:
        return None
    match = _PMCID_RE.search(str(pmcid))
    if match:
        return f"PMC{match.group(1)}"
    digits = str(pmcid).strip()
    if digits.isdigit():
        return f"PMC{digits}"
    return None


def normalize_title(title: str | None) -> str:
    """Aggressively normalise a title for duplicate detection only.

    The result is never displayed to a user; it exists purely so that
    ``"Immediate Dentin Sealing: A Systematic Review."`` and
    ``"Immediate dentin sealing - a systematic review"`` collapse to the
    same key.
    """
    if not title:
        return ""
    text = strip_html(title)
    text = unicodedata.normalize("NFKD", text)
    text = "".join(ch for ch in text if not unicodedata.combining(ch))
    text = text.lower()
    text = _NON_ALNUM_RE.sub(" ", text)
    tokens = [t for t in text.split() if t and t not in _TITLE_STOPWORDS]
    return " ".join(tokens)


def title_similarity(a: str | None, b: str | None) -> float:
    """Token-level Jaccard similarity of two normalised titles (0..1).

    Jaccard is used rather than an edit-distance ratio because journal
    titles differ mainly by inserted or dropped words (subtitles,
    "a systematic review and meta-analysis"), not by character noise.
    """
    ta = set(normalize_title(a).split())
    tb = set(normalize_title(b).split())
    if not ta or not tb:
        return 0.0
    intersection = len(ta & tb)
    union = len(ta | tb)
    return intersection / union if union else 0.0


def safe_int(value: object, minimum: int | None = None, maximum: int | None = None) -> int | None:
    """Best-effort integer coercion that returns ``None`` instead of guessing."""
    if value is None:
        return None
    try:
        if isinstance(value, bool):
            return None
        result = int(str(value).strip()[:12])
    except (TypeError, ValueError):
        return None
    if minimum is not None and result < minimum:
        return None
    if maximum is not None and result > maximum:
        return None
    return result


def truncate(text: str | None, limit: int, suffix: str = "…") -> str:
    """Truncate ``text`` to ``limit`` characters without splitting mid-word."""
    if not text:
        return ""
    if len(text) <= limit:
        return text
    cut = text[:limit].rsplit(" ", 1)[0]
    return f"{cut}{suffix}"
