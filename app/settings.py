"""Application settings.

All configuration comes from environment variables (optionally loaded
from a local ``.env`` file). No secret is ever hard-coded in this
repository, and no secret is ever written to a log.
"""

from __future__ import annotations

import functools
from pathlib import Path

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

APP_ROOT = Path(__file__).resolve().parent
PROJECT_ROOT = APP_ROOT.parent
CONFIG_DIR = APP_ROOT / "config"


class Settings(BaseSettings):
    """Runtime configuration for the Clinical Evidence Gateway."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
    )

    # ------------------------------------------------------------------
    # Service identity
    # ------------------------------------------------------------------
    service_name: str = "Clinical Evidence Gateway"
    # Bump on every functional change. /health reports this, which is the
    # only reliable way to confirm WHICH build a platform is actually
    # running — two builds can share a domain count and differ in code.
    version: str = "1.4.0"
    environment: str = Field(default="development")
    debug_endpoints_enabled: bool = Field(default=False)

    # ------------------------------------------------------------------
    # Authentication (PART 6, PART 55)
    # ------------------------------------------------------------------
    # Preferred: CLINICAL_API_KEYS as a comma-separated list of
    #   name:secret pairs, e.g. "gpt-prod:abc123,gpt-staging:def456".
    # Backwards compatible: CLINICAL_API_KEY holds a single secret.
    clinical_api_keys: str = Field(default="")
    clinical_api_key: str = Field(default="")
    revoked_api_keys: str = Field(default="")
    auth_required: bool = Field(default=True)

    # ------------------------------------------------------------------
    # Upstream credentials (all optional)
    # ------------------------------------------------------------------
    ncbi_api_key: str = Field(default="")
    ncbi_tool_name: str = Field(default="clinical-evidence-gateway")
    ncbi_contact_email: str = Field(default="")
    crossref_mailto: str = Field(default="")

    # ------------------------------------------------------------------
    # Networking / safety (PART 23)
    # ------------------------------------------------------------------
    http_timeout_seconds: float = Field(default=15.0)
    # Widening the search costs extra upstream round-trips. GPT Actions
    # abandon a call at roughly 45s, and a request that dies in the client
    # is reported as a technical failure — strictly worse than returning
    # the strict (empty) result honestly. Never start a new relaxation
    # round once this much of the budget is already spent.
    relaxation_budget_seconds: float = Field(default=12.0)
    http_connect_timeout_seconds: float = Field(default=5.0)
    http_max_redirects: int = Field(default=3)
    http_max_response_bytes: int = Field(default=8 * 1024 * 1024)
    http_max_connections: int = Field(default=20)
    http_user_agent: str = Field(
        default=(
            "ClinicalEvidenceGateway/1.0 "
            "(+https://github.com/; evidence-gateway; research use)"
        )
    )
    allow_http_scheme: bool = Field(default=False)
    # Set true ONLY in tests, so mock transports can target example.test.
    ssrf_allow_unresolvable_hosts: bool = Field(default=False)

    # ------------------------------------------------------------------
    # Rate limiting (PART 35)
    # ------------------------------------------------------------------
    rate_limit_per_minute: int = Field(default=60)
    rate_limit_burst: int = Field(default=15)
    rate_limit_enabled: bool = Field(default=True)

    # Upstream politeness limits (requests / second)
    pubmed_rate_limit_per_second: float = Field(default=3.0)
    pubmed_rate_limit_per_second_with_key: float = Field(default=9.0)
    europepmc_rate_limit_per_second: float = Field(default=5.0)
    crossref_rate_limit_per_second: float = Field(default=5.0)
    openfda_rate_limit_per_second: float = Field(default=3.0)
    manufacturer_rate_limit_per_second: float = Field(default=2.0)

    # ------------------------------------------------------------------
    # Cache (PART 34)
    # ------------------------------------------------------------------
    cache_enabled: bool = Field(default=True)
    cache_backend: str = Field(default="sqlite")
    cache_database_url: str = Field(default="sqlite:///./data/cache.db")
    cache_max_rows: int = Field(default=20000)

    # ------------------------------------------------------------------
    # Logging (PART 36)
    # ------------------------------------------------------------------
    log_level: str = Field(default="INFO")
    log_format: str = Field(default="json")
    log_query_text: bool = Field(default=True)
    log_query_max_chars: int = Field(default=200)

    # ------------------------------------------------------------------
    # CORS
    # ------------------------------------------------------------------
    cors_allow_origins: str = Field(default="")

    # ------------------------------------------------------------------
    # Config file locations
    # ------------------------------------------------------------------
    sources_file: Path = Field(default=CONFIG_DIR / "sources.yaml")
    journals_file: Path = Field(default=CONFIG_DIR / "approved_journals.yaml")
    synonyms_file: Path = Field(default=CONFIG_DIR / "dental_synonyms.yaml")
    evidence_rules_file: Path = Field(default=CONFIG_DIR / "evidence_rules.yaml")

    @field_validator("environment")
    @classmethod
    def _normalise_env(cls, value: str) -> str:
        return value.strip().lower() or "development"

    # ------------------------------------------------------------------
    # Derived helpers
    # ------------------------------------------------------------------
    @property
    def is_production(self) -> bool:
        return self.environment in {"production", "prod"}

    @property
    def cors_origins_list(self) -> list[str]:
        return [o.strip() for o in self.cors_allow_origins.split(",") if o.strip()]

    def parsed_api_keys(self) -> dict[str, str]:
        """Return ``{secret: key_name}`` for every active API key.

        Secrets are never logged or returned by any endpoint.
        """
        keys: dict[str, str] = {}

        raw_multi = self.clinical_api_keys.strip()
        if raw_multi:
            for entry in raw_multi.split(","):
                entry = entry.strip()
                if not entry:
                    continue
                if ":" in entry:
                    name, _, secret = entry.partition(":")
                    name, secret = name.strip(), secret.strip()
                else:
                    name, secret = "unnamed", entry
                if secret:
                    keys[secret] = name

        single = self.clinical_api_key.strip()
        if single:
            keys.setdefault(single, "default")

        for revoked in self.revoked_key_set():
            keys.pop(revoked, None)

        return keys

    def revoked_key_set(self) -> set[str]:
        return {k.strip() for k in self.revoked_api_keys.split(",") if k.strip()}


@functools.lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Cached settings singleton."""
    return Settings()


def reset_settings_cache() -> None:
    """Clear the cached settings (used by tests)."""
    get_settings.cache_clear()
