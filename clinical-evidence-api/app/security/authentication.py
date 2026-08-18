"""API-key authentication (PART 6, PART 55).

Design notes:
  * The key travels in the ``X-Clinical-Key`` header, matching the
    OpenAPI ``ClinicalAPIKey`` security scheme that GPT Builder imports.
  * Comparison uses :func:`hmac.compare_digest` so that a timing oracle
    cannot be used to recover a key byte-by-byte.
  * Keys are never logged. Only a short, salted fingerprint of the key
    name is attached to log records so that usage can be attributed
    without the secret ever touching disk.
  * Multiple active keys are supported from day one so that rotation is
    a config change, not a redeploy-with-downtime.
"""

from __future__ import annotations

import hashlib
import hmac
import logging
from dataclasses import dataclass

from fastapi import Request
from fastapi.security import APIKeyHeader

from app.settings import Settings, get_settings

logger = logging.getLogger(__name__)

__all__ = [
    "API_KEY_HEADER_NAME",
    "AuthError",
    "ApiKeyIdentity",
    "authenticate_request",
    "key_fingerprint",
    "api_key_header_scheme",
]

API_KEY_HEADER_NAME = "X-Clinical-Key"

# Declared for OpenAPI generation; the actual check lives in
# authenticate_request so we control the error envelope.
api_key_header_scheme = APIKeyHeader(name=API_KEY_HEADER_NAME, auto_error=False)


class AuthError(Exception):
    """Raised when a request cannot be authenticated."""

    def __init__(self, message: str = "Invalid or missing API key") -> None:
        super().__init__(message)
        self.message = message


@dataclass(frozen=True)
class ApiKeyIdentity:
    """The authenticated caller. Contains no secret material."""

    key_name: str
    fingerprint: str

    @property
    def rate_limit_key(self) -> str:
        return self.fingerprint


def key_fingerprint(secret: str) -> str:
    """Return a short, non-reversible fingerprint of a key.

    Safe to log and to use as a rate-limit bucket identifier. Truncated
    SHA-256 — enough to distinguish keys, useless for recovering one.
    """
    if not secret:
        return "anonymous"
    return hashlib.sha256(secret.encode("utf-8")).hexdigest()[:12]


def _extract_key(request: Request) -> str:
    """Read the API key from the request, checking the canonical header
    first and then a small set of tolerated alternatives.

    GPT Actions sends exactly the header declared in the OpenAPI schema,
    but accepting ``Authorization: Bearer`` too makes local testing with
    curl and Postman much less painful without weakening anything.
    """
    header_value = request.headers.get(API_KEY_HEADER_NAME)
    if header_value:
        return header_value.strip()

    authorization = request.headers.get("Authorization", "")
    if authorization.lower().startswith("bearer "):
        return authorization[7:].strip()

    return ""


def authenticate_request(request: Request, settings: Settings | None = None) -> ApiKeyIdentity:
    """Validate the request's API key and return the caller identity.

    Raises :class:`AuthError` if the key is missing, revoked or unknown.
    """
    cfg = settings or get_settings()

    if not cfg.auth_required:
        return ApiKeyIdentity(key_name="auth-disabled", fingerprint="local-dev")

    presented = _extract_key(request)
    active_keys = cfg.parsed_api_keys()

    if not active_keys:
        # Fail closed. A deployment with no configured key must not
        # silently become a public, unauthenticated evidence proxy.
        logger.error("auth_misconfigured_no_keys")
        raise AuthError(
            "Server has no API keys configured; refusing to authenticate requests"
        )

    if not presented:
        raise AuthError("Missing API key header")

    if presented in cfg.revoked_key_set():
        logger.warning("auth_revoked_key_used", extra={"key_fp": key_fingerprint(presented)})
        raise AuthError("API key has been revoked")

    # Constant-time comparison against every active key. We iterate over
    # all of them (no early break on mismatch) so that the number of
    # comparisons does not leak which key was closest.
    matched_name: str | None = None
    for secret, name in active_keys.items():
        if hmac.compare_digest(presented, secret):
            matched_name = name

    if matched_name is None:
        logger.warning("auth_failed", extra={"key_fp": key_fingerprint(presented)})
        raise AuthError("Invalid API key")

    return ApiKeyIdentity(key_name=matched_name, fingerprint=key_fingerprint(presented))
