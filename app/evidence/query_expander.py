"""Dental query expansion and PICO query building (PART 16, PART 17).

Two competing failure modes are being balanced:

  * too little expansion -> the search misses the literature because the
    clinician wrote "veneer bonding" and PubMed indexes
    "resin cementation of ceramic laminate veneers";
  * too much expansion -> the search silently answers a different
    question from the one that was asked.

The rules that keep this honest:

  * a concept is only expanded when one of its own terms appears in the
    query, on a word boundary;
  * expansion is capped (``max_concepts``, ``max_terms_per_concept``,
    ``max_total_terms``);
  * brand -> generic is permitted, generic -> brand is forbidden, so
    "lithium disilicate" never collapses into "IPS e.max";
  * terms in ``never_auto_expand`` (bond strength, thermocycling, ...)
    are never injected, because adding them converts a clinical question
    into a laboratory question.
"""

from __future__ import annotations

import functools
import logging
import re
from dataclasses import dataclass, field
from pathlib import Path

import yaml

from app.evidence.rules import EvidenceRules, get_evidence_rules
from app.settings import get_settings
from app.utils.normalize import normalize_whitespace

logger = logging.getLogger(__name__)

__all__ = ["ExpansionResult", "QueryExpander", "get_query_expander"]


@dataclass
class ExpansionResult:
    """Outcome of expanding one user query."""

    original_query: str
    expanded_query: str
    europe_pmc_query: str = ""
    # Progressively looser queries, tried in order when the strict query
    # returns nothing. Each entry is (label, pubmed_query).
    fallback_queries: list[tuple[str, str]] = field(default_factory=list)
    matched_concepts: list[str] = field(default_factory=list)
    matched_brands: list[str] = field(default_factory=list)
    added_terms: list[str] = field(default_factory=list)
    pico_clause: str | None = None
    notes: list[str] = field(default_factory=list)


@dataclass
class _Concept:
    key: str
    specialty: str
    terms: tuple[str, ...]
    mesh: tuple[str, ...]
    patterns: tuple[re.Pattern[str], ...]

    def first_position(self, text: str) -> int:
        """Index of the earliest trigger match in ``text`` (or a large number)."""
        positions = [m.start() for p in self.patterns if (m := p.search(text))]
        return min(positions) if positions else 10**6


@dataclass
class _Brand:
    key: str
    terms: tuple[str, ...]
    generic: tuple[str, ...]
    manufacturer: str | None
    patterns: tuple[re.Pattern[str], ...]


class QueryExpander:
    """Expand a dental clinical query into a scientific search string."""

    def __init__(
        self,
        concepts: dict[str, _Concept],
        brands: dict[str, _Brand],
        never_auto_expand: frozenset[str],
        rules: EvidenceRules,
    ) -> None:
        self._concepts = concepts
        self._brands = brands
        self._never = never_auto_expand
        self._rules = rules
        cfg = rules.expansion or {}
        self._max_concepts = int(cfg.get("max_concepts", 4))
        self._max_terms = int(cfg.get("max_terms_per_concept", 6))
        self._max_total = int(cfg.get("max_total_terms", 24))
        self._allow_generic_to_brand = bool(cfg.get("allow_generic_to_brand", False))

    # ------------------------------------------------------------------
    @classmethod
    def from_file(cls, path: Path, rules: EvidenceRules | None = None) -> "QueryExpander":
        raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        return cls.from_mapping(raw, rules or get_evidence_rules())

    @classmethod
    def from_mapping(cls, raw: dict, rules: EvidenceRules) -> "QueryExpander":
        concepts: dict[str, _Concept] = {}
        for key, spec in (raw.get("concepts") or {}).items():
            if not isinstance(spec, dict):
                continue
            terms = tuple(
                normalize_whitespace(t) for t in (spec.get("terms") or []) if str(t).strip()
            )
            if not terms:
                continue
            # The concept key itself is a legitimate trigger: a user who
            # types "veneers" or "bonding" must match the `veneers` /
            # `bonding` concepts even though the term list contains only
            # multi-word phrases such as "porcelain veneer".
            triggers = {key.replace("_", " "), *terms}
            concepts[key] = _Concept(
                key=key,
                specialty=str(spec.get("specialty", "all")),
                terms=terms,
                mesh=tuple(str(m) for m in (spec.get("mesh") or [])),
                patterns=tuple(_term_pattern(t) for t in sorted(triggers)),
            )

        brands: dict[str, _Brand] = {}
        for key, spec in (raw.get("brands") or {}).items():
            if not isinstance(spec, dict):
                continue
            terms = tuple(
                normalize_whitespace(t) for t in (spec.get("terms") or []) if str(t).strip()
            )
            if not terms:
                continue
            brands[key] = _Brand(
                key=key,
                terms=terms,
                generic=tuple(str(g) for g in (spec.get("generic") or [])),
                manufacturer=spec.get("manufacturer"),
                patterns=tuple(_term_pattern(t) for t in terms),
            )

        never = frozenset(
            normalize_whitespace(t).lower() for t in (raw.get("never_auto_expand") or [])
        )
        return cls(concepts, brands, never, rules)

    # ------------------------------------------------------------------
    def expand(
        self,
        query: str,
        *,
        specialty: str | None = None,
        population: str | None = None,
        intervention: str | None = None,
        comparator: str | None = None,
        outcome: str | None = None,
    ) -> ExpansionResult:
        """Return an expanded PubMed-style query string."""
        original = normalize_whitespace(query)
        result = ExpansionResult(original_query=original, expanded_query=original)
        if not original:
            return result

        lowered = original.lower()

        # --- brands first: brand -> generic is a safe widening ----------
        triggered_generics: set[str] = set()
        for brand in self._brands.values():
            if any(p.search(lowered) for p in brand.patterns):
                result.matched_brands.append(brand.key)
                triggered_generics.update(brand.generic)

        # --- concepts ---------------------------------------------------
        matched: list[_Concept] = []
        for concept in self._concepts.values():
            if any(p.search(lowered) for p in concept.patterns):
                matched.append(concept)

        for generic_key in triggered_generics:
            concept = self._concepts.get(generic_key)
            if concept and concept not in matched:
                matched.append(concept)
                result.notes.append(
                    f"Added generic concept '{generic_key}' because a brand name "
                    "was mentioned."
                )

        # Prefer concepts matching the requested specialty, then order of
        # appearance in the query, then cap.
        matched.sort(
            key=lambda c: (
                (c.specialty != specialty) if specialty else False,
                c.first_position(lowered),
            )
        )
        matched = matched[: self._max_concepts]
        result.matched_concepts = [c.key for c in matched]

        # --- build OR groups -------------------------------------------
        groups: list[str] = []
        total_added = 0
        for concept in matched:
            terms: list[str] = []
            for term in concept.terms:
                if total_added >= self._max_total or len(terms) >= self._max_terms:
                    break
                lowered_term = term.lower()
                if lowered_term in self._never and lowered_term not in lowered:
                    continue
                if not self._allow_generic_to_brand and self._is_brand_term(lowered_term):
                    continue
                terms.append(f'"{term}"[Title/Abstract]')
                if lowered_term not in lowered:
                    result.added_terms.append(term)
                    total_added += 1
            for mesh in concept.mesh:
                if total_added >= self._max_total:
                    break
                terms.append(f'"{mesh}"[MeSH Terms]')
                result.added_terms.append(mesh)
                total_added += 1
            if terms:
                groups.append("(" + " OR ".join(terms) + ")")

        # --- PICO clause ------------------------------------------------
        pico_clause = self._build_pico_clause(
            population=population,
            intervention=intervention,
            comparator=comparator,
            outcome=outcome,
        )
        if pico_clause:
            result.pico_clause = pico_clause

        # --- assemble ---------------------------------------------------
        # The user's own words always stay in the query as a free-text
        # clause: expansion widens, it never replaces.
        clauses = [f"({original})"]
        if groups:
            clauses.append(" AND ".join(groups))
        if pico_clause:
            clauses.append(pico_clause)

        result.expanded_query = " AND ".join(clauses) if len(clauses) > 1 else clauses[0]
        result.europe_pmc_query = _to_europe_pmc_dialect(result.expanded_query)

        # --- relaxation ladder -------------------------------------------
        # The strict query ANDs the user's own words with every concept
        # group and every PICO clause. That is precise but brittle: a
        # natural-language question ("strongest independent evidence on X
        # versus Y") ANDs a sentence no paper contains, and each PICO field
        # adds another hard requirement. When the strict query returns
        # nothing we must widen rather than report "no evidence".
        ladder: list[tuple[str, str]] = []
        if groups and pico_clause:
            # drop PICO, keep the user's words + concept groups
            ladder.append(("without_pico", " AND ".join([f"({original})", " AND ".join(groups)])))
        if groups:
            # drop the raw free-text sentence, keep the dental concepts
            ladder.append(("concepts_only", " AND ".join(groups)))
        if not groups:
            ladder.append(("original_only", f"({original})"))
        # de-duplicate and never repeat the strict query itself
        seen_q = {result.expanded_query}
        result.fallback_queries = []
        for label, q in ladder:
            if q and q not in seen_q:
                seen_q.add(q)
                result.fallback_queries.append((label, q))

        if not groups and not pico_clause:
            result.notes.append(
                "No dental concept matched; the original query was used unchanged."
            )
        return result

    # ------------------------------------------------------------------
    def _is_brand_term(self, term: str) -> bool:
        for brand in self._brands.values():
            if term in (t.lower() for t in brand.terms):
                return True
        return False

    def _build_pico_clause(
        self,
        *,
        population: str | None,
        intervention: str | None,
        comparator: str | None,
        outcome: str | None,
    ) -> str | None:
        """Build a structured PICO clause (PART 17).

        Population, intervention and outcome are ANDed because they must
        all be present for a record to answer the question. The
        comparator is ORed with the intervention rather than ANDed:
        requiring both named arms in the title/abstract is far too
        restrictive and loses most relevant trials.
        """
        parts: list[str] = []

        intervention_terms = _free_text_clause(intervention)
        comparator_terms = _free_text_clause(comparator)

        if intervention_terms and comparator_terms:
            parts.append(f"({intervention_terms} OR {comparator_terms})")
        elif intervention_terms:
            parts.append(f"({intervention_terms})")
        elif comparator_terms:
            parts.append(f"({comparator_terms})")

        population_terms = _free_text_clause(population)
        if population_terms:
            parts.append(f"({population_terms})")

        outcome_terms = _free_text_clause(outcome)
        if outcome_terms:
            parts.append(f"({outcome_terms})")

        if not parts:
            return None
        return " AND ".join(parts)



def _to_europe_pmc_dialect(pubmed_query: str) -> str:
    """Translate a PubMed-syntax query into Europe PMC search syntax.

    PubMed field tags (``"term"[Title/Abstract]``, ``"term"[MeSH Terms]``)
    are not understood by Europe PMC: sending them through unchanged makes
    every expanded search return zero hits. Europe PMC uses prefixed
    fields instead (``TITLE_ABS:"term"``, ``MESH:"term"``).
    """
    if not pubmed_query:
        return ""

    field_map = {
        "title/abstract": "TITLE_ABS",
        "tiab": "TITLE_ABS",
        "title": "TITLE",
        "mesh terms": "MESH",
        "mesh": "MESH",
        "author": "AUTH",
        "journal": "JOURNAL",
        "publication type": "PUB_TYPE",
        "pt": "PUB_TYPE",
    }

    def _replace(match: re.Match[str]) -> str:
        term = match.group("term")
        tag = match.group("tag").strip().lower()
        prefix = field_map.get(tag)
        if prefix is None:
            # Unknown tag: keep the term as plain text rather than
            # emitting syntax Europe PMC would reject.
            return f'"{term}"'
        return f'{prefix}:"{term}"'

    return re.sub(
        r'"(?P<term>[^"]+)"\s*\[(?P<tag>[^\]]+)\]',
        _replace,
        pubmed_query,
    )


def _free_text_clause(value: str | None) -> str | None:
    """Turn a free-text PICO element into a Title/Abstract clause."""
    text = normalize_whitespace(value or "")
    if not text or len(text) < 3:
        return None
    # Split on obvious separators so "survival / debonding / sensitivity"
    # becomes three OR'd terms rather than one unmatchable phrase.
    fragments = [
        f.strip()
        for f in re.split(r"[\/;,]|\bor\b|\band\b", text, flags=re.IGNORECASE)
        if f.strip() and len(f.strip()) >= 3
    ]
    if not fragments:
        fragments = [text]
    fragments = fragments[:6]
    return " OR ".join(f'"{f}"[Title/Abstract]' for f in fragments)


def _term_pattern(term: str) -> re.Pattern[str]:
    """Word-boundary-anchored, case-insensitive matcher for a term.

    Tolerates hyphen/space interchange ("in-vitro" vs "in vitro") and a
    trailing plural ``s``/``es`` so that "veneer" matches "veneers".
    The leading and trailing look-arounds are what stop "bonding" from
    matching inside "debonding".
    """
    stem = _singularise(term.lower())
    escaped = re.escape(stem)
    # Single pass: replacing " " and "-" separately would rewrite the
    # hyphen inside the character class inserted by the first pass.
    escaped = re.sub(r"\\[ \-]", r"[\\s\\-]+", escaped)
    return re.compile(rf"(?<![a-z0-9]){escaped}(?:e?s)?(?![a-z0-9])", re.IGNORECASE)


def _singularise(term: str) -> str:
    """Strip a trailing plural ``s`` from the final word of ``term``.

    Combined with the optional ``(e?s)?`` suffix in the compiled pattern
    this makes matching number-insensitive in both directions, so the
    concept key ``veneers`` matches the query word "veneer" and vice
    versa. Words ending in ``ss``/``us``/``is`` (e.g. "analysis") are
    left alone.
    """
    head, _, last = term.rpartition(" ")
    word = last or term
    if len(word) > 4 and word.endswith("s") and not word.endswith(("ss", "us", "is")):
        word = word[:-1]
    return f"{head} {word}".strip() if head else word


@functools.lru_cache(maxsize=1)
def get_query_expander() -> QueryExpander:
    return QueryExpander.from_file(get_settings().synonyms_file)
