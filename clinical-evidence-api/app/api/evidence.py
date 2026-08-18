"""``POST /v1/evidence/search`` — clinical evidence search."""

from __future__ import annotations

import logging

from fastapi import APIRouter, Query

from app.api.deps import Cache, CurrentIdentity, HttpClient, Registry, RequestId
from app.evidence.rules import get_evidence_rules
from app.models.schemas import EvidenceSearchRequest, SearchResponse
from app.services.cache import cache_key
from app.services.logging_service import log_event
from app.services.search_orchestrator import EvidenceOrchestrator
from app.settings import get_settings
from app.utils.helpers import Stopwatch

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/v1/evidence", tags=["evidence"])

ENDPOINT = "evidence_search"


@router.post(
    "/search",
    response_model=SearchResponse,
    response_model_exclude_none=False,
    summary="Search trusted clinical and scientific dental evidence",
    operation_id="searchClinicalEvidence",
)
async def search_clinical_evidence(
    payload: EvidenceSearchRequest,
    identity: CurrentIdentity,
    http: HttpClient,
    cache: Cache,
    registry: Registry,
    request_id: RequestId,
    debug: bool = Query(default=False, include_in_schema=False),
) -> SearchResponse:
    """Search PubMed, Europe PMC, approved guidelines and Crossref metadata.

    All source allowlisting, evidence classification, laboratory
    firewalling, deduplication, retraction checking and conflict detection
    happen server-side. The response is structured data for the calling
    model to synthesise — it is not a clinical answer.
    """
    settings = get_settings()
    rules = get_evidence_rules()

    key = cache_key(ENDPOINT, payload.model_dump(mode="json"))
    cached = await cache.get(key)
    if cached is not None:
        log_event(
            logger, "cache_hit", endpoint=ENDPOINT, request_id=request_id,
            key_name=identity.key_name,
        )
        response = SearchResponse.model_validate(cached.payload)
        response.cache_hit = True
        response.request_id = request_id
        return response

    orchestrator = EvidenceOrchestrator(http, registry=registry)

    with Stopwatch() as watch:
        response = await orchestrator.search(payload, request_id=request_id)

    log_event(
        logger,
        "evidence_search_completed",
        endpoint=ENDPOINT,
        request_id=request_id,
        key_name=identity.key_name,
        query=payload.query,
        specialty=payload.specialty.value if payload.specialty else None,
        result_count=response.result_count,
        duplicates_merged=response.duplicates_merged,
        excluded_count=response.excluded_count,
        partial_results=response.partial_results,
        insufficient_evidence=response.insufficient_evidence,
        successful_sources=response.successful_sources,
        failed_sources=[f.provider for f in response.failed_sources],
        duration_ms=watch.elapsed_ms,
        cache_hit=False,
    )

    # Only cache complete, successful responses. A partial result would
    # otherwise pin an upstream outage into the cache for hours.
    if not response.partial_results and response.result_count > 0:
        await cache.set(
            key, ENDPOINT, response.model_dump(mode="json"),
            rules.ttl("evidence_search", 43200),
        )

    if debug and settings.debug_endpoints_enabled and not settings.is_production:
        response.debug = {
            "expanded_query": response.expanded_query,
            "duration_ms": watch.elapsed_ms,
            "cache_key": key[:16],
        }

    return response
