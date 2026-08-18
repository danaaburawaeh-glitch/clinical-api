"""Clinical Evidence Gateway — FastAPI application.

Run locally::

    uvicorn app.main:app --host 0.0.0.0 --port 8000

Or with Docker::

    docker compose up --build
"""

from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from typing import Any, AsyncIterator

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

from app.api import evidence, manufacturer, regulatory, source_verify
from app.errors import ErrorCode, GatewayError, error_payload
from app.security.allowlist import get_source_registry
from app.security.authentication import API_KEY_HEADER_NAME
from app.security.safe_http import (
    DisallowedContentType,
    ResponseTooLarge,
    SafeHttpClient,
    UpstreamError,
    UpstreamRateLimited,
    UpstreamTimeout,
)
from app.security.url_validator import UrlValidationError
from app.services.cache import build_cache
from app.services.logging_service import configure_logging, log_event
from app.settings import get_settings
from app.utils.helpers import SlidingWindowLimiter, Stopwatch, new_request_id

logger = logging.getLogger(__name__)

DESCRIPTION = """
Evidence gateway for dentistry. Searches only approved scientific,
regulatory, professional and manufacturer sources.

All source allowlisting and validation is enforced server-side: the
calling model cannot widen the source set, and a domain absent from the
allowlist can be neither fetched nor returned.

Key guarantees:

* **Hard source allowlist** — enforced before any socket is opened and
  again before any URL is serialised into a response.
* **Manufacturer firewall** — manufacturer documents are always
  `MANUFACTURER_INFORMATION` and can never become independent evidence.
* **Laboratory firewall** — bench studies (bond strength, thermocycling,
  FEA, artificial saliva) are always `EARLY_PRECLINICAL` with
  `clinical_translation = uncertain`.
* **Regulatory/clinical separation** — FDA clearance is never rendered as
  clinical superiority, and clearance is never conflated with approval.
* **No fabricated metadata** — absent identifiers are `null`, never guessed.
* **Retraction safety** — retracted work is flagged and excluded from the
  recommendable set.
"""


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    """Create and dispose shared resources."""
    configure_logging()
    settings = get_settings()

    registry = get_source_registry()
    app.state.http_client = SafeHttpClient()
    app.state.cache = build_cache()
    app.state.rate_limiter = SlidingWindowLimiter(settings.rate_limit_per_minute)

    if settings.auth_required and not settings.parsed_api_keys():
        logger.error(
            "startup_no_api_keys_configured",
            extra={
                "hint": "Set CLINICAL_API_KEY or CLINICAL_API_KEYS. "
                        "All requests will be rejected until you do."
            },
        )

    log_event(
        logger,
        "service_started",
        service=settings.service_name,
        version=settings.version,
        environment=settings.environment,
        allowlisted_domains=len(registry.all_domains()),
        manufacturer_domains=len(registry.manufacturers()),
        cache_backend=type(app.state.cache).__name__,
        rate_limit_per_minute=settings.rate_limit_per_minute,
    )

    try:
        yield
    finally:
        await app.state.http_client.aclose()
        await app.state.cache.close()
        log_event(logger, "service_stopped", service=settings.service_name)


def create_app() -> FastAPI:
    settings = get_settings()

    app = FastAPI(
        title="Clinical Evidence Safe Search",
        description=DESCRIPTION,
        version=settings.version,
        lifespan=lifespan,
        docs_url=None if settings.is_production else "/docs",
        redoc_url=None if settings.is_production else "/redoc",
        openapi_url="/openapi.json",
    )

    if settings.cors_origins_list:
        app.add_middleware(
            CORSMiddleware,
            allow_origins=settings.cors_origins_list,
            allow_credentials=False,
            allow_methods=["GET", "POST"],
            allow_headers=[API_KEY_HEADER_NAME, "Content-Type"],
        )

    # ------------------------------------------------------------------
    # Request context + access logging
    # ------------------------------------------------------------------
    @app.middleware("http")
    async def request_context(request: Request, call_next):  # type: ignore[no-untyped-def]
        request_id = new_request_id()
        request.state.request_id = request_id
        with Stopwatch() as watch:
            try:
                response = await call_next(request)
            except Exception:
                logger.exception(
                    "unhandled_request_error",
                    extra={"request_id": request_id, "path": request.url.path},
                )
                return JSONResponse(
                    status_code=500,
                    content=error_payload(
                        ErrorCode.INTERNAL_ERROR,
                        "An internal error occurred.",
                        False,
                        request_id,
                    ),
                    headers={"X-Request-ID": request_id},
                )

        response.headers["X-Request-ID"] = request_id
        # Defensive headers; this API returns JSON only.
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Referrer-Policy"] = "no-referrer"
        response.headers["Cache-Control"] = "no-store"

        log_event(
            logger,
            "http_request",
            request_id=request_id,
            method=request.method,
            path=request.url.path,
            status_code=response.status_code,
            duration_ms=watch.elapsed_ms,
            key_name=getattr(request.state, "key_name", None),
        )
        return response

    # ------------------------------------------------------------------
    # Error handlers — structured envelope, never a stack trace (PART 56)
    # ------------------------------------------------------------------
    def _request_id(request: Request) -> str:
        return getattr(request.state, "request_id", "unknown")

    @app.exception_handler(GatewayError)
    async def handle_gateway_error(request: Request, exc: GatewayError) -> JSONResponse:
        return JSONResponse(
            status_code=exc.status_code,
            content=exc.payload(_request_id(request)),
        )

    @app.exception_handler(UrlValidationError)
    async def handle_url_error(request: Request, exc: UrlValidationError) -> JSONResponse:
        logger.warning("url_validation_blocked", extra={"reason": exc.reason})
        return JSONResponse(
            status_code=403,
            content=error_payload(
                ErrorCode.SOURCE_NOT_ALLOWED, exc.reason, False, _request_id(request)
            ),
        )

    @app.exception_handler(UpstreamTimeout)
    async def handle_upstream_timeout(request: Request, exc: UpstreamTimeout) -> JSONResponse:
        return JSONResponse(
            status_code=504,
            content=error_payload(
                ErrorCode.UPSTREAM_TIMEOUT, exc.message, True, _request_id(request)
            ),
        )

    @app.exception_handler(UpstreamRateLimited)
    async def handle_upstream_rate_limit(
        request: Request, exc: UpstreamRateLimited
    ) -> JSONResponse:
        return JSONResponse(
            status_code=503,
            content=error_payload(
                ErrorCode.UPSTREAM_RATE_LIMIT, exc.message, True, _request_id(request)
            ),
        )

    @app.exception_handler(ResponseTooLarge)
    async def handle_too_large(request: Request, exc: ResponseTooLarge) -> JSONResponse:
        return JSONResponse(
            status_code=502,
            content=error_payload(
                ErrorCode.RESPONSE_TOO_LARGE, exc.message, False, _request_id(request)
            ),
        )

    @app.exception_handler(DisallowedContentType)
    async def handle_bad_mime(request: Request, exc: DisallowedContentType) -> JSONResponse:
        return JSONResponse(
            status_code=502,
            content=error_payload(
                ErrorCode.DISALLOWED_CONTENT_TYPE, exc.message, False, _request_id(request)
            ),
        )

    @app.exception_handler(UpstreamError)
    async def handle_upstream_error(request: Request, exc: UpstreamError) -> JSONResponse:
        return JSONResponse(
            status_code=502,
            content=error_payload(
                ErrorCode.UPSTREAM_ERROR, exc.message, exc.retryable, _request_id(request)
            ),
        )

    @app.exception_handler(RequestValidationError)
    async def handle_validation_error(
        request: Request, exc: RequestValidationError
    ) -> JSONResponse:
        # Field paths and messages only — never the submitted values, which
        # could contain clinical free text.
        details = "; ".join(
            f"{'.'.join(str(p) for p in err.get('loc', [])[1:])}: {err.get('msg', '')}"
            for err in exc.errors()[:6]
        )
        return JSONResponse(
            status_code=422,
            content=error_payload(
                ErrorCode.INVALID_REQUEST,
                f"Request validation failed. {details}",
                False,
                _request_id(request),
            ),
        )

    @app.exception_handler(StarletteHTTPException)
    async def handle_http_exception(
        request: Request, exc: StarletteHTTPException
    ) -> JSONResponse:
        code = {
            401: ErrorCode.INVALID_API_KEY,
            403: ErrorCode.SOURCE_NOT_ALLOWED,
            404: ErrorCode.INVALID_REQUEST,
            429: ErrorCode.RATE_LIMITED,
        }.get(exc.status_code, ErrorCode.INTERNAL_ERROR)
        return JSONResponse(
            status_code=exc.status_code,
            content=error_payload(
                code, str(exc.detail), exc.status_code >= 500, _request_id(request)
            ),
        )

    @app.exception_handler(Exception)
    async def handle_unexpected(request: Request, exc: Exception) -> JSONResponse:
        logger.exception("unhandled_exception", extra={"path": request.url.path})
        return JSONResponse(
            status_code=500,
            content=error_payload(
                ErrorCode.INTERNAL_ERROR,
                "An internal error occurred.",
                False,
                _request_id(request),
            ),
        )

    # ------------------------------------------------------------------
    app.include_router(source_verify.router)
    app.include_router(evidence.router)
    app.include_router(regulatory.router)
    app.include_router(manufacturer.router)

    _apply_openapi_security(app)
    return app


def _apply_openapi_security(app: FastAPI) -> None:
    """Attach the ``ClinicalAPIKey`` security scheme to the generated schema."""
    base_openapi = app.openapi

    def custom_openapi() -> dict[str, Any]:
        if app.openapi_schema:
            return app.openapi_schema
        schema = base_openapi()
        schema.setdefault("components", {})["securitySchemes"] = {
            "ClinicalAPIKey": {
                "type": "apiKey",
                "in": "header",
                "name": API_KEY_HEADER_NAME,
            }
        }
        schema["security"] = [{"ClinicalAPIKey": []}]
        app.openapi_schema = schema
        return schema

    app.openapi = custom_openapi  # type: ignore[method-assign]


app = create_app()
