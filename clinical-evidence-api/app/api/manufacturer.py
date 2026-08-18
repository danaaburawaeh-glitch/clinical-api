"""``POST /v1/manufacturer/search`` — official manufacturer documentation.

Enforces the manufacturer firewall (PART 25) at the transport layer:
``required_category="manufacturer"`` is passed to the URL validator, so
this endpoint physically cannot fetch or return a non-manufacturer
domain, let alone an unapproved one.
"""

from __future__ import annotations

import logging

from fastapi import APIRouter

from app.api.deps import Cache, CurrentIdentity, HttpClient, Registry, RequestId
from app.engines.manufacturer_search import ManufacturerSearchEngine
from app.evidence.classifier import classify_records
from app.evidence.rules import get_evidence_rules
from app.models.schemas import ManufacturerSearchRequest, SearchResponse
from app.services.cache import cache_key
from app.services.logging_service import log_event
from app.services.search_orchestrator import records_to_results
from app.utils.helpers import Stopwatch, utc_now_iso

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/v1/manufacturer", tags=["manufacturer"])

ENDPOINT = "manufacturer_search"

MANUFACTURER_FIREWALL_NOTE = (
    "MANUFACTURER INFORMATION. These records are supplied by the product "
    "manufacturer. They are authoritative for instructions for use, "
    "composition, indications, contraindications, handling and compatibility, "
    "and they are NOT independent clinical evidence. They cannot establish "
    "that a product is clinically superior, has better survival, or produces "
    "lower failure rates than a competitor. For any comparative or "
    "superiority question, call searchClinicalEvidence for independent "
    "evidence and report the two separately."
)


@router.post(
    "/search",
    response_model=SearchResponse,
    summary="Retrieve official manufacturer documentation",
    operation_id="getManufacturerDocument",
)
async def get_manufacturer_document(
    payload: ManufacturerSearchRequest,
    identity: CurrentIdentity,
    http: HttpClient,
    cache: Cache,
    registry: Registry,
    request_id: RequestId,
) -> SearchResponse:
    """Search only official, allowlisted manufacturer domains."""
    rules = get_evidence_rules()
    key = cache_key(ENDPOINT, payload.model_dump(mode="json"))

    cached = await cache.get(key)
    if cached is not None:
        response = SearchResponse.model_validate(cached.payload)
        response.cache_hit = True
        response.request_id = request_id
        return response

    engine = ManufacturerSearchEngine(http, registry)

    with Stopwatch() as watch:
        outcome = await engine.search(
            payload.query,
            manufacturer=payload.manufacturer,
            product_name=payload.product_name,
            document_type=payload.document_type.value,
            max_results=payload.max_results,
        )

    # Classification forces evidence_level = MANUFACTURER_INFORMATION for
    # every record whose source domain is a manufacturer (PART 25).
    classify_records(outcome.records, registry=registry)
    for record in outcome.records:
        record.retrieved_at = record.retrieved_at or utc_now_iso()

    results = records_to_results(outcome.records, registry)

    warnings = [MANUFACTURER_FIREWALL_NOTE, *outcome.warnings]

    unverified = [r for r in results if r.current_document_verified is False]
    if unverified:
        warnings.append(
            f"{len(unverified)} document(s) could not be confirmed as the current "
            "official revision. No version number or document date has been "
            "invented; check the manufacturer's site for the latest IFU revision."
        )

    response = SearchResponse(
        query=payload.query,
        result_count=len(results),
        searched_sources=[f"Manufacturer: {d}" for d in outcome.searched_domains],
        successful_sources=(
            [f"Manufacturer: {d}" for d in outcome.searched_domains] if results else []
        ),
        failed_sources=[],
        partial_results=False,
        results=results,
        evidence_summary=None,
        summary_requires_model_synthesis=True,
        warnings=warnings,
        insufficient_evidence=not results,
        retrieved_at=utc_now_iso(),
        request_id=request_id,
        ranking_method=(
            "Manufacturer documents are ordered by keyword relevance of the "
            "discovered on-domain link. They are never ranked as clinical evidence."
        ),
    )

    log_event(
        logger,
        "manufacturer_search_completed",
        endpoint=ENDPOINT,
        request_id=request_id,
        key_name=identity.key_name,
        query=payload.query,
        manufacturer=payload.manufacturer,
        document_type=payload.document_type.value,
        result_count=len(results),
        searched_domains=outcome.searched_domains,
        duration_ms=watch.elapsed_ms,
    )

    if results:
        await cache.set(
            key, ENDPOINT, response.model_dump(mode="json"),
            rules.ttl("manufacturer_search", 86400),
        )

    return response
