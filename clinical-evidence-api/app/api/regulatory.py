"""``POST /v1/regulatory/search`` — regulatory status lookup (PART 28-31, 61)."""

from __future__ import annotations

import asyncio
import logging

from fastapi import APIRouter

from app.api.deps import Cache, CurrentIdentity, HttpClient, Registry, RequestId
from app.engines.base import EngineError, RawRecord
from app.engines.fda import REGULATORY_INTERPRETATION_NOTE, adapters_for_authority
from app.engines.openfda import OpenFdaEngine
from app.engines.sfda import SfdaEngine
from app.evidence.classifier import classify_records
from app.evidence.rules import get_evidence_rules
from app.models.schemas import FailedSource, RegulatorySearchRequest, SearchResponse
from app.services.cache import cache_key
from app.services.logging_service import log_event
from app.services.search_orchestrator import records_to_results
from app.utils.helpers import Stopwatch, utc_now_iso

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/v1/regulatory", tags=["regulatory"])

ENDPOINT = "regulatory_search"


@router.post(
    "/search",
    response_model=SearchResponse,
    summary="Search approved regulatory sources",
    operation_id="searchRegulatoryEvidence",
)
async def search_regulatory_evidence(
    payload: RegulatorySearchRequest,
    identity: CurrentIdentity,
    http: HttpClient,
    cache: Cache,
    registry: Registry,
    request_id: RequestId,
) -> SearchResponse:
    """Search FDA / openFDA, SFDA and other allowlisted regulators.

    Regulatory status is returned as regulatory status. It is never
    presented as clinical effectiveness, and FDA "clearance" is never
    conflated with FDA "approval".
    """
    rules = get_evidence_rules()
    key = cache_key(ENDPOINT, payload.model_dump(mode="json"))

    cached = await cache.get(key)
    if cached is not None:
        response = SearchResponse.model_validate(cached.payload)
        response.cache_hit = True
        response.request_id = request_id
        # Regulatory data ages badly; make the age explicit (PART 61).
        response.warnings = list(response.warnings) + [
            f"Served from cache (age {int(cached.age_seconds)}s). Regulatory "
            "status can change; re-query or verify with the authority before "
            "relying on it."
        ]
        return response

    authority = payload.authority.value
    warnings: list[str] = [REGULATORY_INTERPRETATION_NOTE]
    searched: list[str] = []
    successful: list[str] = []
    failed: list[FailedSource] = []
    records: list[RawRecord] = []

    with Stopwatch() as watch:
        tasks: dict[str, asyncio.Future] = {}

        # --- openFDA (structured API) ---------------------------------
        if authority in {"FDA", "ANY_APPROVED"}:
            openfda = OpenFdaEngine(http)
            tasks["openFDA"] = asyncio.ensure_future(
                openfda.search(
                    payload.query,
                    product_name=payload.product_name,
                    manufacturer=payload.manufacturer,
                    regulatory_type=payload.regulatory_type.value,
                    identifier=payload.identifier,
                    max_results=payload.max_results,
                )
            )

        # --- SFDA (domain-restricted) ---------------------------------
        sfda_engine: SfdaEngine | None = None
        if authority in {"SFDA", "ANY_APPROVED"}:
            sfda_engine = SfdaEngine(http, registry)
            tasks["SFDA"] = asyncio.ensure_future(
                sfda_engine.search(
                    payload.query,
                    product_name=payload.product_name,
                    manufacturer=payload.manufacturer,
                    regulatory_type=payload.regulatory_type.value,
                    max_results=min(payload.max_results, 5),
                )
            )

        # --- Other regulators (extensible adapters) -------------------
        for adapter in adapters_for_authority(authority, http, registry):
            tasks[adapter.spec.display_name] = asyncio.ensure_future(
                adapter.search(
                    payload.query,
                    product_name=payload.product_name,
                    manufacturer=payload.manufacturer,
                    regulatory_type=payload.regulatory_type.value,
                    max_results=min(payload.max_results, 5),
                )
            )

        if not tasks:
            warnings.append(
                f"No engine is configured for authority {authority!r}."
            )

        for name, task in tasks.items():
            searched.append(name)
            try:
                outcome = await task
            except EngineError as exc:
                failed.append(
                    FailedSource(provider=name, reason=exc.reason, retryable=exc.retryable)
                )
                continue
            except Exception as exc:  # noqa: BLE001
                logger.exception("regulator_engine_failed", extra={"provider": name})
                failed.append(
                    FailedSource(
                        provider=name,
                        reason=f"unexpected error: {exc.__class__.__name__}",
                        retryable=True,
                    )
                )
                continue

            successful.append(name)
            if hasattr(outcome, "records"):  # SfdaOutcome
                records.extend(outcome.records)
                warnings.extend(outcome.warnings)
            else:
                records.extend(outcome)

    classify_records(records, registry=registry)
    for record in records:
        record.retrieved_at = record.retrieved_at or utc_now_iso()

    results = records_to_results(records[: payload.max_results], registry)

    if not results:
        warnings.append(
            "No regulatory record was retrieved from the approved authorities for "
            "this query. Absence of a record here is NOT evidence that a product "
            "lacks clearance, approval or registration."
        )

    response = SearchResponse(
        query=payload.query,
        result_count=len(results),
        searched_sources=searched,
        successful_sources=successful,
        failed_sources=failed,
        partial_results=bool(failed) and bool(successful),
        results=results,
        evidence_summary=None,
        summary_requires_model_synthesis=True,
        warnings=warnings,
        insufficient_evidence=not results,
        retrieved_at=utc_now_iso(),
        request_id=request_id,
        ranking_method="Regulatory records are returned in source order; they are "
                       "not ranked as clinical evidence.",
    )

    log_event(
        logger,
        "regulatory_search_completed",
        endpoint=ENDPOINT,
        request_id=request_id,
        key_name=identity.key_name,
        query=payload.query,
        authority=authority,
        result_count=len(results),
        duration_ms=watch.elapsed_ms,
    )

    if results and not response.partial_results:
        await cache.set(
            key, ENDPOINT, response.model_dump(mode="json"),
            rules.ttl("regulatory_search", 3600),
        )

    return response
