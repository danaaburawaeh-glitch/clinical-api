"""openFDA engine (PART 29).

Uses the official openFDA device endpoints on ``api.fda.gov``:

    /device/510k.json          510(k) clearances
    /device/pma.json           PMA approvals / supplements
    /device/classification.json device classification
    /device/enforcement.json   recalls (enforcement reports)
    /device/recall.json        device recalls
    /device/event.json         adverse events (MAUDE)

Two rules the rest of the system depends on:

*   **"cleared" is not "approved."** 510(k) clearance means substantial
    equivalence to a predicate; PMA approval means a premarket review of
    safety and effectiveness. This engine keeps them in separate fields
    (``regulatory_pathway``) and never collapses the vocabulary
    (PART 29, PART 31).
*   **Exact identifier lookup first.** A caller who supplies ``K251002``
    gets an exact ``k_number`` lookup; only if that misses do we fall
    back to a text search (PART 29).

No API key is required for openFDA's public rate tier.
"""

from __future__ import annotations

import logging
import re
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
from app.utils.helpers import utc_now_iso
from app.utils.normalize import normalize_whitespace, safe_int

logger = logging.getLogger(__name__)

PROVIDER = "openFDA"
API_HOST = "api.fda.gov"
SOURCE_DOMAIN = "api.fda.gov"
DISPLAY_DOMAIN = "accessdata.fda.gov"

BASE = f"https://{API_HOST}/device"

K_NUMBER_RE = re.compile(r"\bK\d{6}\b", re.IGNORECASE)
P_NUMBER_RE = re.compile(r"\bP\d{6}\b", re.IGNORECASE)
DEN_NUMBER_RE = re.compile(r"\bDEN\d{6}\b", re.IGNORECASE)


class OpenFdaEngine:
    """Async client for openFDA device endpoints."""

    def __init__(self, http: SafeHttpClient) -> None:
        self._http = http
        settings = get_settings()
        http.register_rate_limiter("openfda", settings.openfda_rate_limit_per_second)

    # ------------------------------------------------------------------
    # Identifier routing
    # ------------------------------------------------------------------
    @staticmethod
    def detect_identifier(text: str | None) -> tuple[str, str] | None:
        """Detect a regulatory identifier and its kind in free text."""
        if not text:
            return None
        for pattern, kind in (
            (K_NUMBER_RE, "510k"),
            (DEN_NUMBER_RE, "denovo"),
            (P_NUMBER_RE, "pma"),
        ):
            match = pattern.search(text)
            if match:
                return match.group(0).upper(), kind
        return None

    # ------------------------------------------------------------------
    async def lookup_identifier(self, identifier: str) -> list[RawRecord]:
        """Exact lookup for a 510(k), De Novo or PMA number."""
        detected = self.detect_identifier(identifier)
        if detected is None:
            return []
        number, kind = detected

        if kind in {"510k", "denovo"}:
            payload = await self._query(
                "510k.json", f'k_number:"{number}"', limit=1
            )
            return self._parse_510k(payload)

        payload = await self._query("pma.json", f'pma_number:"{number}"', limit=5)
        return self._parse_pma(payload)

    async def search(
        self,
        query: str,
        *,
        product_name: str | None = None,
        manufacturer: str | None = None,
        regulatory_type: str = "any",
        identifier: str | None = None,
        max_results: int = 10,
    ) -> list[RawRecord]:
        """Search openFDA, exact-identifier first."""
        records: list[RawRecord] = []

        attempted = 0
        failures = 0

        # 1. Exact identifier lookup takes precedence (PART 29).
        candidate_id = identifier or query
        detected = self.detect_identifier(candidate_id)
        if detected:
            attempted += 1
            try:
                records = await self.lookup_identifier(detected[0])
            except EngineError:
                failures += 1
                records = []
            if records:
                return records[:max_results]

        # 2. Fall back to a structured text search over the relevant
        #    endpoints for the requested regulatory type.
        endpoints = self._endpoints_for(regulatory_type)
        terms = self._build_search_expression(query, product_name, manufacturer)

        for endpoint, parser in endpoints:
            attempted += 1
            try:
                payload = await self._query(endpoint, terms, limit=max_results)
            except EngineError as exc:
                failures += 1
                logger.info("openfda_endpoint_failed", extra={
                    "endpoint": endpoint, "reason": exc.reason})
                continue
            records.extend(parser(payload))
            if len(records) >= max_results:
                break

        # If every call failed we must NOT return an empty list: "openFDA
        # was unreachable" and "openFDA has no such device" are different
        # answers, and conflating them would let the model report an
        # absence of clearance that was never actually checked.
        if not records and attempted and failures == attempted:
            raise EngineError(
                PROVIDER,
                "all openFDA endpoints failed; no regulatory status could be checked",
                retryable=True,
            )

        return records[:max_results]

    # ------------------------------------------------------------------
    def _endpoints_for(self, regulatory_type: str) -> list[tuple[str, Any]]:
        mapping = {
            "clearance": [("510k.json", self._parse_510k)],
            "approval": [("pma.json", self._parse_pma)],
            "classification": [("classification.json", self._parse_classification)],
            "recall": [
                ("recall.json", self._parse_recall),
                ("enforcement.json", self._parse_enforcement),
            ],
            "safety_alert": [("enforcement.json", self._parse_enforcement)],
            "adverse_event": [("event.json", self._parse_event)],
        }
        if regulatory_type in mapping:
            return mapping[regulatory_type]
        # "any", "registration", "indication", "labeling" -> broad sweep.
        return [
            ("510k.json", self._parse_510k),
            ("pma.json", self._parse_pma),
            ("classification.json", self._parse_classification),
        ]

    @staticmethod
    def _build_search_expression(
        query: str, product_name: str | None, manufacturer: str | None
    ) -> str:
        """Compose an openFDA Lucene-style search expression."""
        clauses: list[str] = []
        core = normalize_whitespace(query)
        if core:
            escaped = core.replace('"', " ")
            clauses.append(
                f'(device_name:"{escaped}" OR openfda.device_name:"{escaped}")'
            )
        if product_name:
            name = normalize_whitespace(product_name).replace('"', " ")
            clauses.append(f'(device_name:"{name}" OR trade_name:"{name}")')
        if manufacturer:
            maker = normalize_whitespace(manufacturer).replace('"', " ")
            clauses.append(f'(applicant:"{maker}" OR openfda.manufacturer_name:"{maker}")')
        return " AND ".join(clauses) if clauses else "*"

    # ------------------------------------------------------------------
    # Parsers — every field is copied verbatim or left None (PART 59)
    # ------------------------------------------------------------------
    def _parse_510k(self, payload: Any) -> list[RawRecord]:
        records: list[RawRecord] = []
        for item in _results(payload):
            k_number = normalize_whitespace(item.get("k_number")) or None
            decision = normalize_whitespace(item.get("decision_description")) or None
            device_name = (
                normalize_whitespace(item.get("device_name"))
                or normalize_whitespace(item.get("openfda", {}).get("device_name"))
                or "[Device name not provided by openFDA]"
            )
            url = (
                f"https://{DISPLAY_DOMAIN}/scripts/cdrh/cfdocs/cfpmn/pmn.cfm?ID={k_number}"
                if k_number
                else f"https://{DISPLAY_DOMAIN}/scripts/cdrh/cfdocs/cfpmn/pmn.cfm"
            )
            records.append(
                RawRecord(
                    provider=PROVIDER,
                    source_domain=SOURCE_DOMAIN,
                    url=url,
                    title=f"{device_name} — FDA 510(k) {k_number or 'record'}",
                    publication_year=_year(item.get("decision_date")),
                    regulatory_identifier=k_number,
                    regulatory_authority="FDA",
                    regulatory_pathway="510(k) premarket notification (cleared)",
                    regulatory_status=decision,
                    decision_date=normalize_whitespace(item.get("decision_date")) or None,
                    retrieved_at=utc_now_iso(),
                    manufacturer=normalize_whitespace(item.get("applicant")) or None,
                    product=device_name,
                    document_type="510k_clearance",
                    abstract=self._describe_510k(item),
                    limitations=(
                        "FDA 510(k) clearance denotes substantial equivalence to a "
                        "legally marketed predicate device. It is NOT an FDA "
                        "approval and is not evidence of clinical superiority."
                    ),
                    extra={"openfda_raw_keys": sorted(item.keys())},
                )
            )
        return records

    def _parse_pma(self, payload: Any) -> list[RawRecord]:
        records: list[RawRecord] = []
        for item in _results(payload):
            pma_number = normalize_whitespace(item.get("pma_number")) or None
            supplement = normalize_whitespace(item.get("supplement_number")) or None
            identifier = f"{pma_number}{('/' + supplement) if supplement else ''}"
            device_name = (
                normalize_whitespace(item.get("trade_name"))
                or normalize_whitespace(item.get("generic_name"))
                or "[Device name not provided by openFDA]"
            )
            records.append(
                RawRecord(
                    provider=PROVIDER,
                    source_domain=SOURCE_DOMAIN,
                    url=(
                        f"https://{DISPLAY_DOMAIN}/scripts/cdrh/cfdocs/cfpma/pma.cfm"
                        f"?id={pma_number}" if pma_number else
                        f"https://{DISPLAY_DOMAIN}/scripts/cdrh/cfdocs/cfpma/pma.cfm"
                    ),
                    title=f"{device_name} — FDA PMA {identifier or 'record'}",
                    publication_year=_year(item.get("decision_date")),
                    regulatory_identifier=identifier or None,
                    regulatory_authority="FDA",
                    regulatory_pathway="Premarket approval (PMA, approved)",
                    regulatory_status=(
                        normalize_whitespace(item.get("decision_code")) or None
                    ),
                    decision_date=normalize_whitespace(item.get("decision_date")) or None,
                    retrieved_at=utc_now_iso(),
                    manufacturer=normalize_whitespace(item.get("applicant")) or None,
                    product=device_name,
                    document_type="pma_approval",
                    abstract=normalize_whitespace(item.get("ao_statement")) or None,
                    limitations=(
                        "FDA premarket approval reflects a regulatory determination "
                        "of reasonable assurance of safety and effectiveness for the "
                        "approved indication. It is not a comparative claim against "
                        "other products."
                    ),
                )
            )
        return records

    def _parse_classification(self, payload: Any) -> list[RawRecord]:
        records: list[RawRecord] = []
        for item in _results(payload):
            device_name = (
                normalize_whitespace(item.get("device_name"))
                or "[Device name not provided by openFDA]"
            )
            product_code = normalize_whitespace(item.get("product_code")) or None
            records.append(
                RawRecord(
                    provider=PROVIDER,
                    source_domain=SOURCE_DOMAIN,
                    url=(
                        f"https://{DISPLAY_DOMAIN}/scripts/cdrh/cfdocs/cfPCD/"
                        f"classification.cfm?ID={product_code}"
                        if product_code
                        else f"https://{DISPLAY_DOMAIN}/scripts/cdrh/cfdocs/cfPCD/classification.cfm"
                    ),
                    title=f"{device_name} — FDA device classification",
                    regulatory_identifier=product_code,
                    regulatory_authority="FDA",
                    regulatory_pathway=(
                        f"Class {normalize_whitespace(item.get('device_class')) or '?'} "
                        "device classification"
                    ),
                    regulatory_status=normalize_whitespace(item.get("medical_specialty_description"))
                    or None,
                    retrieved_at=utc_now_iso(),
                    product=device_name,
                    document_type="classification",
                    abstract=normalize_whitespace(item.get("definition")) or None,
                    limitations=(
                        "Device classification describes the regulatory control "
                        "category only; it says nothing about clinical performance."
                    ),
                )
            )
        return records

    def _parse_recall(self, payload: Any) -> list[RawRecord]:
        records: list[RawRecord] = []
        for item in _results(payload):
            product = normalize_whitespace(item.get("product_description")) or "[Recall record]"
            number = normalize_whitespace(item.get("res_event_number")) or None
            records.append(
                RawRecord(
                    provider=PROVIDER,
                    source_domain=SOURCE_DOMAIN,
                    url=f"https://{DISPLAY_DOMAIN}/scripts/cdrh/cfdocs/cfRes/res.cfm",
                    title=f"FDA device recall — {product[:120]}",
                    regulatory_identifier=number,
                    regulatory_authority="FDA",
                    regulatory_pathway="Device recall",
                    regulatory_status=normalize_whitespace(item.get("recall_status")) or None,
                    decision_date=normalize_whitespace(item.get("event_date_initiated")) or None,
                    retrieved_at=utc_now_iso(),
                    manufacturer=normalize_whitespace(item.get("recalling_firm")) or None,
                    product=product,
                    document_type="recall",
                    abstract=normalize_whitespace(item.get("reason_for_recall")) or None,
                )
            )
        return records

    def _parse_enforcement(self, payload: Any) -> list[RawRecord]:
        records: list[RawRecord] = []
        for item in _results(payload):
            product = normalize_whitespace(item.get("product_description")) or "[Enforcement report]"
            records.append(
                RawRecord(
                    provider=PROVIDER,
                    source_domain=SOURCE_DOMAIN,
                    url=f"https://{DISPLAY_DOMAIN}/scripts/ires/index.cfm",
                    title=f"FDA enforcement report — {product[:120]}",
                    regulatory_identifier=normalize_whitespace(item.get("recall_number")) or None,
                    regulatory_authority="FDA",
                    regulatory_pathway="Enforcement report / recall classification",
                    regulatory_status=normalize_whitespace(item.get("status")) or None,
                    decision_date=normalize_whitespace(item.get("recall_initiation_date")) or None,
                    retrieved_at=utc_now_iso(),
                    manufacturer=normalize_whitespace(item.get("recalling_firm")) or None,
                    product=product,
                    document_type="safety_alert",
                    abstract=normalize_whitespace(item.get("reason_for_recall")) or None,
                    extra={"classification": normalize_whitespace(item.get("classification"))},
                )
            )
        return records

    def _parse_event(self, payload: Any) -> list[RawRecord]:
        records: list[RawRecord] = []
        for item in _results(payload):
            devices = item.get("device") or []
            device_name = "[Adverse event report]"
            manufacturer = None
            if devices and isinstance(devices[0], dict):
                device_name = (
                    normalize_whitespace(devices[0].get("brand_name"))
                    or normalize_whitespace(devices[0].get("generic_name"))
                    or device_name
                )
                manufacturer = normalize_whitespace(
                    devices[0].get("manufacturer_d_name")
                ) or None
            records.append(
                RawRecord(
                    provider=PROVIDER,
                    source_domain=SOURCE_DOMAIN,
                    url=f"https://{DISPLAY_DOMAIN}/scripts/cdrh/cfdocs/cfmaude/search.cfm",
                    title=f"FDA MAUDE adverse event — {device_name}",
                    regulatory_identifier=normalize_whitespace(item.get("report_number")) or None,
                    regulatory_authority="FDA",
                    regulatory_pathway="MAUDE adverse event report",
                    regulatory_status=normalize_whitespace(item.get("event_type")) or None,
                    decision_date=normalize_whitespace(item.get("date_received")) or None,
                    retrieved_at=utc_now_iso(),
                    manufacturer=manufacturer,
                    product=device_name,
                    document_type="adverse_event",
                    limitations=(
                        "MAUDE reports are voluntary, unverified and subject to "
                        "reporting bias. Counts cannot be used to estimate "
                        "incidence or to compare product safety."
                    ),
                )
            )
        return records

    @staticmethod
    def _describe_510k(item: dict) -> str | None:
        parts = []
        for key, label in (
            ("statement_or_summary", "Statement/summary"),
            ("clearance_type", "Clearance type"),
            ("advisory_committee_description", "Advisory committee"),
            ("product_code", "Product code"),
        ):
            value = normalize_whitespace(item.get(key))
            if value:
                parts.append(f"{label}: {value}")
        return "; ".join(parts) or None

    # ------------------------------------------------------------------
    async def _query(self, endpoint: str, search: str, limit: int = 10) -> Any:
        params = {"search": search, "limit": str(max(1, min(limit, 100)))}
        url = f"{BASE}/{endpoint}"
        try:
            return await self._http.get_json(url, params=params, provider="openfda")
        except UpstreamTimeout as exc:
            raise EngineError(PROVIDER, "timeout", retryable=True) from exc
        except UpstreamRateLimited as exc:
            raise EngineError(PROVIDER, "rate limited by openFDA", retryable=True) from exc
        except UpstreamError as exc:
            # openFDA returns 404 for "no matches", which is not an error.
            if exc.status_code == 404:
                return {"results": []}
            raise EngineError(PROVIDER, str(exc), retryable=True) from exc
        except (UrlValidationError, ValueError) as exc:
            raise EngineError(PROVIDER, str(exc), retryable=False) from exc


def _results(payload: Any) -> list[dict]:
    if not isinstance(payload, dict):
        return []
    results = payload.get("results")
    return [r for r in results if isinstance(r, dict)] if isinstance(results, list) else []


def _year(date_value: Any) -> int | None:
    text = normalize_whitespace(str(date_value or ""))
    if len(text) >= 4:
        return safe_int(text[:4], 1900, 2200)
    return None
