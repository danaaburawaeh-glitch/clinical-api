"""``POST /v1/source/verify`` and ``GET /health`` (PART 41, PART 42)."""

from __future__ import annotations

import logging

from fastapi import APIRouter, Request

from app.api.deps import CurrentIdentity, Registry, RequestId
from app.models.schemas import (
    HealthComponent,
    HealthResponse,
    SourceVerifyRequest,
    SourceVerifyResponse,
)
from app.security.allowlist import get_source_registry
from app.security.url_validator import UrlValidationError, validate_url_sync
from app.services.logging_service import log_event
from app.settings import get_settings
from app.utils.helpers import utc_now_iso
from app.utils.normalize import normalize_hostname

logger = logging.getLogger(__name__)

router = APIRouter(tags=["source"])


@router.post(
    "/v1/source/verify",
    response_model=SourceVerifyResponse,
    summary="Verify whether a source is approved",
    operation_id="verifyClinicalSource",
)
async def verify_clinical_source(
    payload: SourceVerifyRequest,
    identity: CurrentIdentity,
    registry: Registry,
    request_id: RequestId,
) -> SourceVerifyResponse:
    """Report whether a URL is on the hard server-side allowlist.

    Answers three separate questions:
      * is the domain allowlisted at all?
      * what category of source is it (evidence, guideline, regulator,
        manufacturer, metadata)?
      * what may it be used for, and what may it explicitly NOT be used
        for?
    """
    # Run the full syntactic validation so that scheme, port, userinfo and
    # control-character rejections are reported with their real reason,
    # not folded into a generic "not allowlisted".
    try:
        validated = validate_url_sync(payload.url, registry=registry)
    except UrlValidationError as exc:
        host = normalize_hostname(payload.url)
        log_event(
            logger, "source_verify_rejected",
            request_id=request_id, key_name=identity.key_name,
            domain=host or None, reason=exc.reason,
        )
        return SourceVerifyResponse(
            allowed=False,
            domain=host or None,
            source_category="unapproved",
            trust_tier=None,
            allowed_use="none",
            forbidden_use=None,
            reason=exc.reason,
        )

    described = registry.describe(validated.host)
    log_event(
        logger, "source_verify_allowed",
        request_id=request_id, key_name=identity.key_name,
        domain=described.get("domain"), category=described.get("source_category"),
    )
    return SourceVerifyResponse(**described)  # type: ignore[arg-type]


@router.get(
    "/health",
    response_model=HealthResponse,
    summary="Service health",
    operation_id="healthCheck",
)
async def health(request: Request) -> HealthResponse:
    """Liveness and configuration health.

    Deliberately does NOT call PubMed, Europe PMC or openFDA (PART 42):
    a health endpoint that hammers upstream APIs is a self-inflicted
    rate-limit outage. It reports what can be checked locally — config
    loading, allowlist size, cache reachability, auth configuration — and
    exposes upstream reachability only via the separate ``/health/deep``
    endpoint, which is rate-limited and authenticated.
    """
    settings = get_settings()
    registry = get_source_registry()
    components: list[HealthComponent] = []

    components.append(
        HealthComponent(
            name="source_allowlist",
            status="ok" if registry.all_domains() else "error",
            detail=f"{len(registry.all_domains())} domains loaded",
        )
    )

    try:
        from app.evidence.rules import get_evidence_rules, get_journal_registry

        rules = get_evidence_rules()
        journals = get_journal_registry()
        components.append(
            HealthComponent(
                name="evidence_rules",
                status="ok" if rules.classes else "error",
                detail=f"{len(rules.classes)} evidence classes, "
                       f"{len(journals.entries)} approved journals",
            )
        )
    except Exception as exc:  # noqa: BLE001
        components.append(
            HealthComponent(
                name="evidence_rules", status="error",
                detail=exc.__class__.__name__,
            )
        )

    cache = getattr(request.app.state, "cache", None)
    components.append(
        HealthComponent(
            name="cache",
            status="ok" if cache is not None else "error",
            detail=type(cache).__name__ if cache is not None else "not initialised",
        )
    )

    auth_ok = bool(settings.parsed_api_keys()) or not settings.auth_required
    components.append(
        HealthComponent(
            name="authentication",
            status="ok" if auth_ok else "error",
            detail=(
                f"{len(settings.parsed_api_keys())} active key(s)"
                if auth_ok
                else "no API keys configured; requests will be rejected"
            ),
        )
    )

    overall = "ok"
    if any(c.status == "error" for c in components):
        overall = "error"
    elif any(c.status == "degraded" for c in components):
        overall = "degraded"

    return HealthResponse(
        status=overall,  # type: ignore[arg-type]
        version=settings.version,
        service=settings.service_name,
        environment=settings.environment,
        allowlisted_domains=len(registry.all_domains()),
        components=components,
        checked_at=utc_now_iso(),
    )


@router.get(
    "/health/deep",
    response_model=HealthResponse,
    summary="Health check including upstream connectivity",
    operation_id="deepHealthCheck",
    include_in_schema=False,
)
async def deep_health(request: Request, identity: CurrentIdentity) -> HealthResponse:
    """Authenticated connectivity probe against upstream APIs.

    Kept out of the public schema and behind authentication so it cannot
    be used to generate upstream load (PART 42).
    """
    from app.security.safe_http import SafeHttpClient, UpstreamError
    from app.utils.helpers import Stopwatch

    settings = get_settings()
    base = await health(request)
    components = list(base.components)

    http: SafeHttpClient = request.app.state.http_client

    probes = [
        (
            "pubmed",
            "https://eutils.ncbi.nlm.nih.gov/entrez/eutils/einfo.fcgi",
            {"db": "pubmed", "retmode": "json"},
        ),
        (
            "europe_pmc",
            "https://www.ebi.ac.uk/europepmc/webservices/rest/search",
            {"query": "dental", "format": "json", "pageSize": "1"},
        ),
        ("openfda", "https://api.fda.gov/device/classification.json", {"limit": "1"}),
    ]

    for name, url, params in probes:
        with Stopwatch() as watch:
            try:
                await http.get_json(url, params=params, provider=name)
                status, detail = "ok", None
            except UpstreamError as exc:
                status, detail = "degraded", str(exc)[:160]
            except Exception as exc:  # noqa: BLE001
                status, detail = "error", exc.__class__.__name__
        components.append(
            HealthComponent(
                name=f"upstream:{name}", status=status,  # type: ignore[arg-type]
                detail=detail, latency_ms=watch.elapsed_ms,
            )
        )

    overall = "ok"
    if any(c.status == "error" for c in components):
        overall = "error"
    elif any(c.status == "degraded" for c in components):
        overall = "degraded"

    return HealthResponse(
        status=overall,  # type: ignore[arg-type]
        version=settings.version,
        service=settings.service_name,
        environment=settings.environment,
        allowlisted_domains=base.allowlisted_domains,
        components=components,
        checked_at=utc_now_iso(),
    )
