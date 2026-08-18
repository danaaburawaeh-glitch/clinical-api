"""Hard, server-side source allowlist.

This module is the single point of truth for "may this backend touch, or
return, this domain?". It is loaded once from ``config/sources.yaml`` and
enforced by the URL validator, the SSRF guard, the safe HTTP client and
every API route.

Design intent (PART 1, PART 18):
The Custom GPT cannot widen this list. Even if a model is prompt-injected
into asking for ``https://randomdentalblog.com/best-veneers``, the request
never leaves this process, because the check happens server-side before
any socket is opened *and* again before any URL is serialised into a
response.
"""

from __future__ import annotations

import functools
import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable

import yaml

from app.settings import get_settings
from app.utils.normalize import normalize_hostname

logger = logging.getLogger(__name__)

__all__ = [
    "SourceEntry",
    "SourceRegistry",
    "get_source_registry",
    "reload_source_registry",
    "TrustTier",
    "SourceCategory",
]


class SourceCategory:
    """Canonical source categories (PART 70)."""

    SCIENTIFIC_DATABASE = "scientific_database"
    JOURNAL = "journal"
    PROFESSIONAL_ORGANIZATION = "professional_organization"
    GUIDELINE_BODY = "guideline_body"
    REGULATOR = "regulator"
    STANDARDS_BODY = "standards_body"
    MANUFACTURER = "manufacturer"
    BIBLIOGRAPHIC_METADATA = "bibliographic_metadata"
    CLINICAL_TRIAL_REGISTRY = "clinical_trial_registry"
    PUBLIC_HEALTH_AUTHORITY = "public_health_authority"

    ALL = frozenset(
        {
            SCIENTIFIC_DATABASE,
            JOURNAL,
            PROFESSIONAL_ORGANIZATION,
            GUIDELINE_BODY,
            REGULATOR,
            STANDARDS_BODY,
            MANUFACTURER,
            BIBLIOGRAPHIC_METADATA,
            CLINICAL_TRIAL_REGISTRY,
            PUBLIC_HEALTH_AUTHORITY,
        }
    )


class TrustTier:
    """Trust tiers (PART 71).

    A trust tier describes *source reliability*, never *evidence
    strength*. A case report indexed in PubMed sits in TIER_A_EVIDENCE
    yet remains low-level evidence (PART 72).
    """

    EVIDENCE = "TIER_A_EVIDENCE"
    GUIDELINE = "TIER_B_GUIDELINE"
    PROFESSIONAL = "TIER_C_PROFESSIONAL"
    REGULATORY = "TIER_R_REGULATORY"
    MANUFACTURER = "TIER_M_MANUFACTURER"
    METADATA = "TIER_META_METADATA"

    _YAML_MAP = {
        "evidence": EVIDENCE,
        "guideline": GUIDELINE,
        "professional": PROFESSIONAL,
        "regulatory": REGULATORY,
        "manufacturer": MANUFACTURER,
        "metadata": METADATA,
    }

    @classmethod
    def from_yaml(cls, value: str | None) -> str:
        return cls._YAML_MAP.get((value or "").strip().lower(), cls.PROFESSIONAL)


@dataclass(frozen=True)
class SourceEntry:
    """One allowlisted domain and the rules attached to it."""

    key: str
    domain: str
    include_subdomains: bool
    category: str
    trust_tier: str
    specialties: tuple[str, ...] = ()
    allowed_for: tuple[str, ...] = ()
    forbidden_for: tuple[str, ...] = ()
    requires_journal_allowlist: bool = False
    notes: str = ""

    @property
    def is_manufacturer(self) -> bool:
        return self.category == SourceCategory.MANUFACTURER

    @property
    def is_regulator(self) -> bool:
        return self.category in {SourceCategory.REGULATOR, SourceCategory.STANDARDS_BODY}

    @property
    def is_guideline_body(self) -> bool:
        return self.category in {
            SourceCategory.GUIDELINE_BODY,
            SourceCategory.PROFESSIONAL_ORGANIZATION,
            SourceCategory.PUBLIC_HEALTH_AUTHORITY,
        }

    def allowed_use_text(self) -> str:
        """Human-readable summary for ``/v1/source/verify``."""
        if self.is_manufacturer:
            return "IFU and product technical information only"
        if not self.allowed_for:
            return "general use within this gateway"
        return ", ".join(self.allowed_for)


@dataclass
class SourceRegistry:
    """Immutable-after-load registry of allowlisted domains."""

    entries: dict[str, SourceEntry] = field(default_factory=dict)
    _by_domain: dict[str, SourceEntry] = field(default_factory=dict, repr=False)
    _subdomain_domains: tuple[str, ...] = field(default=(), repr=False)

    # ------------------------------------------------------------------
    # Loading
    # ------------------------------------------------------------------
    @classmethod
    def from_file(cls, path: Path) -> "SourceRegistry":
        if not path.exists():
            raise FileNotFoundError(f"sources.yaml not found at {path}")
        raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        return cls.from_mapping(raw.get("sources") or {})

    @classmethod
    def from_mapping(cls, mapping: dict) -> "SourceRegistry":
        entries: dict[str, SourceEntry] = {}
        by_domain: dict[str, SourceEntry] = {}

        for key, spec in (mapping or {}).items():
            if not isinstance(spec, dict):
                logger.warning("sources.yaml: entry %s is not a mapping; skipped", key)
                continue

            domain = normalize_hostname(str(spec.get("domain", "")).strip())
            if not domain:
                logger.warning("sources.yaml: entry %s has no usable domain; skipped", key)
                continue

            category = str(spec.get("category", "")).strip()
            if category not in SourceCategory.ALL:
                logger.warning(
                    "sources.yaml: entry %s has unknown category %r; skipped", key, category
                )
                continue

            entry = SourceEntry(
                key=str(key),
                domain=domain,
                include_subdomains=bool(spec.get("include_subdomains", False)),
                category=category,
                trust_tier=TrustTier.from_yaml(spec.get("trust_tier")),
                specialties=tuple(_as_str_tuple(spec.get("specialties"))),
                allowed_for=tuple(_as_str_tuple(spec.get("allowed_for"))),
                forbidden_for=tuple(_as_str_tuple(spec.get("forbidden_for"))),
                requires_journal_allowlist=bool(spec.get("requires_journal_allowlist", False)),
                notes=str(spec.get("notes", "") or "").strip(),
            )

            entries[entry.key] = entry
            # A more specific entry (exact host) must win over a broader
            # wildcard parent, so we keep both keyed by exact domain.
            existing = by_domain.get(domain)
            if existing is None or (existing.include_subdomains and not entry.include_subdomains):
                by_domain[domain] = entry

        subdomain_domains = tuple(
            sorted(
                (e.domain for e in by_domain.values() if e.include_subdomains),
                key=len,
                reverse=True,
            )
        )

        registry = cls(
            entries=entries,
            _by_domain=by_domain,
            _subdomain_domains=subdomain_domains,
        )
        logger.info(
            "source_allowlist_loaded",
            extra={"domain_count": len(by_domain), "entry_count": len(entries)},
        )
        return registry

    # ------------------------------------------------------------------
    # Lookup
    # ------------------------------------------------------------------
    def match_host(self, host: str | None) -> SourceEntry | None:
        """Return the allowlist entry governing ``host``, or ``None``.

        Matching rules:
          * exact hostname match always wins;
          * otherwise the host must be a *label-boundary* suffix of a
            domain whose ``include_subdomains`` is true.

        The label boundary is what defeats the classic bypasses:
        ``ivoclar.com.evil.com`` and ``notivoclar.com`` both fail, and
        ``pubmed.ncbi.nlm.nih.gov.evil.org`` fails because the suffix
        test is anchored at the end of the string.
        """
        normalised = normalize_hostname(host)
        if not normalised:
            return None

        exact = self._by_domain.get(normalised)
        if exact is not None:
            return exact

        for domain in self._subdomain_domains:
            if normalised.endswith("." + domain):
                return self._by_domain[domain]

        return None

    def is_allowed_host(self, host: str | None) -> bool:
        return self.match_host(host) is not None

    def match_url(self, url: str | None) -> SourceEntry | None:
        return self.match_host(normalize_hostname(url))

    def is_allowed_url(self, url: str | None) -> bool:
        return self.match_url(url) is not None

    # ------------------------------------------------------------------
    # Filtered views
    # ------------------------------------------------------------------
    def by_category(self, *categories: str) -> list[SourceEntry]:
        wanted = set(categories)
        return [e for e in self._by_domain.values() if e.category in wanted]

    def manufacturers(self) -> list[SourceEntry]:
        return self.by_category(SourceCategory.MANUFACTURER)

    def manufacturer_domains(self) -> list[str]:
        return sorted(e.domain for e in self.manufacturers())

    def regulators(self) -> list[SourceEntry]:
        return self.by_category(SourceCategory.REGULATOR)

    def guideline_bodies(self) -> list[SourceEntry]:
        return self.by_category(
            SourceCategory.GUIDELINE_BODY,
            SourceCategory.PROFESSIONAL_ORGANIZATION,
            SourceCategory.PUBLIC_HEALTH_AUTHORITY,
        )

    def find_manufacturer(self, name: str | None) -> SourceEntry | None:
        """Resolve a free-text manufacturer name to an allowlisted entry.

        Returns ``None`` when the name cannot be resolved — the caller
        must then refuse rather than guess a domain.
        """
        if not name:
            return None
        needle = "".join(ch for ch in name.lower() if ch.isalnum())
        if not needle:
            return None

        candidates = self.manufacturers()
        # 1. exact key / domain-label match
        for entry in candidates:
            key_norm = entry.key.replace("_", "")
            label = entry.domain.split(".")[0]
            if needle in {key_norm, label}:
                return entry
        # 2. containment either way (handles "Ivoclar Vivadent" -> ivoclar)
        for entry in candidates:
            label = entry.domain.split(".")[0]
            if len(label) >= 4 and (label in needle or needle in label):
                return entry
        for entry in candidates:
            key_norm = entry.key.replace("_", "")
            if len(key_norm) >= 4 and (key_norm in needle or needle in key_norm):
                return entry
        return None

    def all_domains(self) -> list[str]:
        return sorted(self._by_domain.keys())

    def describe(self, host: str | None) -> dict[str, object]:
        """Verification payload used by ``POST /v1/source/verify``."""
        normalised = normalize_hostname(host)
        entry = self.match_host(normalised)
        if entry is None:
            return {
                "allowed": False,
                "domain": normalised or None,
                "source_category": "unapproved",
                "trust_tier": None,
                "allowed_use": "none",
                "forbidden_use": None,
                "reason": "Domain not present in hard allowlist",
            }
        return {
            "allowed": True,
            "domain": entry.domain,
            "source_category": entry.category,
            "trust_tier": entry.trust_tier,
            "allowed_use": entry.allowed_use_text(),
            "forbidden_use": ", ".join(entry.forbidden_for) or None,
            "reason": _approval_reason(entry),
        }


def _approval_reason(entry: SourceEntry) -> str:
    if entry.is_manufacturer:
        return "Official allowlisted manufacturer domain"
    if entry.is_regulator:
        return "Official allowlisted regulatory or standards authority"
    if entry.category == SourceCategory.JOURNAL:
        return "Allowlisted scholarly publisher domain"
    if entry.category == SourceCategory.SCIENTIFIC_DATABASE:
        return "Allowlisted scientific literature database"
    if entry.category == SourceCategory.BIBLIOGRAPHIC_METADATA:
        return "Allowlisted bibliographic metadata service"
    if entry.category == SourceCategory.CLINICAL_TRIAL_REGISTRY:
        return "Allowlisted clinical trial registry"
    return "Allowlisted professional or guideline organisation"


def _as_str_tuple(value: object) -> Iterable[str]:
    if value is None:
        return ()
    if isinstance(value, str):
        return (value,)
    if isinstance(value, (list, tuple, set)):
        return tuple(str(v).strip() for v in value if str(v).strip())
    return ()


@functools.lru_cache(maxsize=1)
def get_source_registry() -> SourceRegistry:
    """Process-wide cached registry."""
    return SourceRegistry.from_file(get_settings().sources_file)


def reload_source_registry() -> SourceRegistry:
    """Clear the cache and reload from disk (used by tests and ops)."""
    get_source_registry.cache_clear()
    return get_source_registry()
