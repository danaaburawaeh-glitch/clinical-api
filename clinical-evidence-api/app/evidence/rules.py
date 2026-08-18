"""Loader for ``evidence_rules.yaml`` and ``approved_journals.yaml``.

Compiled once at import time so that the hot path does no YAML parsing
and no regex compilation.
"""

from __future__ import annotations

import functools
import logging
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

from app.settings import get_settings
from app.utils.normalize import normalize_title, normalize_whitespace

logger = logging.getLogger(__name__)

__all__ = ["EvidenceRules", "JournalRegistry", "get_evidence_rules", "get_journal_registry"]


def _compile(patterns: list[str]) -> list[re.Pattern[str]]:
    compiled: list[re.Pattern[str]] = []
    for pattern in patterns or []:
        try:
            compiled.append(re.compile(pattern, re.IGNORECASE))
        except re.error:
            logger.warning("evidence_rules: invalid regex skipped: %r", pattern)
    return compiled


@dataclass
class IntegrityRule:
    name: str
    severity: int
    excludes_from_recommendation: bool
    api_flag: str
    publication_types: frozenset[str]
    patterns: list[re.Pattern[str]]
    message: str


@dataclass
class EvidenceRules:
    """Compiled evidence classification / ranking configuration."""

    raw: dict[str, Any] = field(default_factory=dict)

    classes: dict[str, dict[str, Any]] = field(default_factory=dict)
    pubtype_rules: list[tuple[frozenset[str], str]] = field(default_factory=list)
    text_patterns: dict[str, list[re.Pattern[str]]] = field(default_factory=dict)

    lab_enabled: bool = True
    lab_forced_class: str = "D1"
    lab_translation: str = "uncertain"
    lab_markers: list[re.Pattern[str]] = field(default_factory=list)
    lab_overrides: list[re.Pattern[str]] = field(default_factory=list)

    integrity_rules: list[IntegrityRule] = field(default_factory=list)

    ranking: dict[str, Any] = field(default_factory=dict)
    conflict: dict[str, Any] = field(default_factory=dict)
    conflict_positive: list[re.Pattern[str]] = field(default_factory=list)
    conflict_negative: list[re.Pattern[str]] = field(default_factory=list)
    conflict_uncertain: list[re.Pattern[str]] = field(default_factory=list)

    expansion: dict[str, Any] = field(default_factory=dict)
    cache_ttls: dict[str, int] = field(default_factory=dict)
    summary_cfg: dict[str, Any] = field(default_factory=dict)

    # ------------------------------------------------------------------
    @classmethod
    def from_file(cls, path: Path) -> "EvidenceRules":
        raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        return cls.from_mapping(raw)

    @classmethod
    def from_mapping(cls, raw: dict[str, Any]) -> "EvidenceRules":
        classes = raw.get("classes") or {}

        pubtype_rules: list[tuple[frozenset[str], str]] = []
        for rule in raw.get("publication_type_map") or []:
            types = frozenset(t.lower() for t in (rule.get("types") or []))
            cls_name = rule.get("cls")
            if types and cls_name:
                pubtype_rules.append((types, cls_name))

        text_patterns = {
            key: _compile(values)
            for key, values in (raw.get("text_patterns") or {}).items()
        }

        lab = raw.get("laboratory_firewall") or {}
        integrity_rules: list[IntegrityRule] = []
        for name, spec in (raw.get("integrity_flags") or {}).items():
            integrity_rules.append(
                IntegrityRule(
                    name=name,
                    severity=int(spec.get("severity", 0)),
                    excludes_from_recommendation=bool(
                        spec.get("excludes_from_recommendation", False)
                    ),
                    api_flag=str(spec.get("api_flag", "integrity_notice")),
                    publication_types=frozenset(
                        t.lower() for t in (spec.get("publication_types") or [])
                    ),
                    patterns=_compile(spec.get("patterns") or []),
                    message=normalize_whitespace(spec.get("message", "")),
                )
            )
        integrity_rules.sort(key=lambda r: r.severity, reverse=True)

        conflict = raw.get("conflict_detection") or {}

        return cls(
            raw=raw,
            classes=classes,
            pubtype_rules=pubtype_rules,
            text_patterns=text_patterns,
            lab_enabled=bool(lab.get("enabled", True)),
            lab_forced_class=str(lab.get("forced_class", "D1")),
            lab_translation=str(lab.get("clinical_translation", "uncertain")),
            lab_markers=_compile(lab.get("markers") or []),
            lab_overrides=_compile(lab.get("clinical_override_markers") or []),
            integrity_rules=integrity_rules,
            ranking=raw.get("ranking") or {},
            conflict=conflict,
            conflict_positive=_compile(conflict.get("positive_markers") or []),
            conflict_negative=_compile(conflict.get("negative_markers") or []),
            conflict_uncertain=_compile(conflict.get("uncertainty_markers") or []),
            expansion=raw.get("query_expansion") or {},
            cache_ttls={
                k: int(v) for k, v in (raw.get("cache_ttl_seconds") or {}).items()
            },
            summary_cfg=raw.get("evidence_summary") or {},
        )

    # ------------------------------------------------------------------
    def api_level(self, evidence_class: str | None) -> str:
        spec = self.classes.get(evidence_class or "", {})
        return str(spec.get("api_level", "UNCLASSIFIED"))

    def base_score(self, evidence_class: str | None) -> float:
        spec = self.classes.get(evidence_class or "", {})
        return float(spec.get("base_score", 20))

    def design_name(self, evidence_class: str | None) -> str:
        spec = self.classes.get(evidence_class or "", {})
        return str(spec.get("design", "unclassified"))

    def class_label(self, evidence_class: str | None) -> str:
        spec = self.classes.get(evidence_class or "", {})
        return str(spec.get("label", "Unclassified"))

    def ttl(self, key: str, default: int = 3600) -> int:
        return int(self.cache_ttls.get(key, default))


@dataclass
class JournalEntry:
    name: str
    abbrevs: tuple[str, ...]
    issn: str | None
    publisher: str | None
    specialty: str
    peer_reviewed: bool
    status: str
    tier: str


@dataclass
class JournalRegistry:
    """Lookup for recognised dental journals (PART 24)."""

    entries: list[JournalEntry] = field(default_factory=list)
    _index: dict[str, JournalEntry] = field(default_factory=dict, repr=False)

    TIER_WEIGHTS = {"core": 1.0, "major": 0.8, "supporting": 0.6}

    @classmethod
    def from_file(cls, path: Path) -> "JournalRegistry":
        raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        return cls.from_list(raw.get("journals") or [])

    @classmethod
    def from_list(cls, items: list[dict]) -> "JournalRegistry":
        entries: list[JournalEntry] = []
        index: dict[str, JournalEntry] = {}

        for item in items:
            if not isinstance(item, dict) or not item.get("name"):
                continue
            abbrevs = item.get("abbrev") or []
            if isinstance(abbrevs, str):
                abbrevs = [abbrevs]
            entry = JournalEntry(
                name=str(item["name"]),
                abbrevs=tuple(str(a) for a in abbrevs),
                issn=(str(item["issn"]) if item.get("issn") else None),
                publisher=(str(item["publisher"]) if item.get("publisher") else None),
                specialty=str(item.get("specialty", "general_dentistry")),
                peer_reviewed=bool(item.get("peer_reviewed", True)),
                status=str(item.get("status", "active")),
                tier=str(item.get("tier", "supporting")),
            )
            entries.append(entry)
            for alias in (entry.name, *entry.abbrevs):
                key = normalize_title(alias)
                if key:
                    index.setdefault(key, entry)

        return cls(entries=entries, _index=index)

    def lookup(self, journal: str | None) -> JournalEntry | None:
        if not journal:
            return None
        key = normalize_title(journal)
        if not key:
            return None
        direct = self._index.get(key)
        if direct:
            return direct
        # Substring fallback handles "J Prosthet Dent." vs "J Prosthet Dent"
        # and titles carrying a section suffix.
        for indexed_key, entry in self._index.items():
            if len(indexed_key) >= 8 and (indexed_key in key or key in indexed_key):
                return entry
        return None

    def is_recognised(self, journal: str | None) -> bool:
        return self.lookup(journal) is not None

    def relevance_weight(self, journal: str | None, specialty: str | None = None) -> float:
        """0..1 weight for journal recognition, nudged by specialty match."""
        entry = self.lookup(journal)
        if entry is None:
            return 0.0
        weight = self.TIER_WEIGHTS.get(entry.tier, 0.6)
        if specialty and entry.specialty == specialty:
            weight = min(1.0, weight + 0.15)
        return weight


@functools.lru_cache(maxsize=1)
def get_evidence_rules() -> EvidenceRules:
    return EvidenceRules.from_file(get_settings().evidence_rules_file)


@functools.lru_cache(maxsize=1)
def get_journal_registry() -> JournalRegistry:
    return JournalRegistry.from_file(get_settings().journals_file)
