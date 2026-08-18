"""Structured error envelope (PART 56).

Every failure the client can see is emitted as::

    {"error": {"code": "...", "message": "...", "retryable": bool,
               "request_id": "..."}}

Stack traces and internal paths never leave the process in production.
"""

from __future__ import annotations

from dataclasses import dataclass

__all__ = [
    "ErrorCode",
    "GatewayError",
    "InvalidApiKeyError",
    "InvalidRequestError",
    "SourceNotAllowedError",
    "UpstreamTimeoutError",
    "UpstreamRateLimitError",
    "NoResultsError",
    "ParsingError",
    "InternalError",
    "RateLimitedError",
    "error_payload",
]


class ErrorCode:
    INVALID_API_KEY = "INVALID_API_KEY"
    INVALID_REQUEST = "INVALID_REQUEST"
    SOURCE_NOT_ALLOWED = "SOURCE_NOT_ALLOWED"
    UPSTREAM_TIMEOUT = "UPSTREAM_TIMEOUT"
    UPSTREAM_RATE_LIMIT = "UPSTREAM_RATE_LIMIT"
    UPSTREAM_ERROR = "UPSTREAM_ERROR"
    NO_RESULTS = "NO_RESULTS"
    PARSING_ERROR = "PARSING_ERROR"
    RATE_LIMITED = "RATE_LIMITED"
    INTERNAL_ERROR = "INTERNAL_ERROR"
    RESPONSE_TOO_LARGE = "RESPONSE_TOO_LARGE"
    DISALLOWED_CONTENT_TYPE = "DISALLOWED_CONTENT_TYPE"
    NOT_CONFIGURED = "NOT_CONFIGURED"


@dataclass
class GatewayError(Exception):
    """Base class for all client-visible errors."""

    message: str
    code: str = ErrorCode.INTERNAL_ERROR
    status_code: int = 500
    retryable: bool = False

    def __post_init__(self) -> None:
        Exception.__init__(self, self.message)

    def payload(self, request_id: str | None = None) -> dict:
        return error_payload(self.code, self.message, self.retryable, request_id)


class InvalidApiKeyError(GatewayError):
    def __init__(self, message: str = "Invalid or missing API key") -> None:
        super().__init__(message, ErrorCode.INVALID_API_KEY, 401, False)


class InvalidRequestError(GatewayError):
    def __init__(self, message: str) -> None:
        super().__init__(message, ErrorCode.INVALID_REQUEST, 422, False)


class SourceNotAllowedError(GatewayError):
    def __init__(self, message: str) -> None:
        super().__init__(message, ErrorCode.SOURCE_NOT_ALLOWED, 403, False)


class UpstreamTimeoutError(GatewayError):
    def __init__(self, message: str = "Upstream request timed out.") -> None:
        super().__init__(message, ErrorCode.UPSTREAM_TIMEOUT, 504, True)


class UpstreamRateLimitError(GatewayError):
    def __init__(self, message: str = "Upstream provider rate limit reached.") -> None:
        super().__init__(message, ErrorCode.UPSTREAM_RATE_LIMIT, 503, True)


class NoResultsError(GatewayError):
    def __init__(self, message: str = "No results within approved sources.") -> None:
        super().__init__(message, ErrorCode.NO_RESULTS, 200, False)


class ParsingError(GatewayError):
    def __init__(self, message: str = "Upstream response could not be parsed.") -> None:
        super().__init__(message, ErrorCode.PARSING_ERROR, 502, True)


class RateLimitedError(GatewayError):
    def __init__(self, message: str = "Rate limit exceeded for this API key.") -> None:
        super().__init__(message, ErrorCode.RATE_LIMITED, 429, True)


class NotConfiguredError(GatewayError):
    def __init__(self, message: str) -> None:
        super().__init__(message, ErrorCode.NOT_CONFIGURED, 501, False)


class InternalError(GatewayError):
    def __init__(self, message: str = "An internal error occurred.") -> None:
        super().__init__(message, ErrorCode.INTERNAL_ERROR, 500, False)


def error_payload(
    code: str,
    message: str,
    retryable: bool = False,
    request_id: str | None = None,
) -> dict:
    """Build the canonical error envelope."""
    return {
        "error": {
            "code": code,
            "message": message,
            "retryable": retryable,
            "request_id": request_id,
        }
    }
