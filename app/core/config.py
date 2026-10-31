"""Application settings (pydantic-settings). Every variable is documented in `.env.example`."""

from __future__ import annotations

import re
from functools import lru_cache
from typing import Annotated, Literal
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from pydantic import Field, model_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict

DEV_JWT_SECRET_DEFAULT = "dev-only-secret-change-me-0123456789abcdef"  # noqa: S105 - dev default
APP_ROLE_NAME = "stockpilot_app"
APP_ROLE_DEV_PASSWORD = "stockpilot_dev_pw"  # noqa: S105 - dev default, override in deployments

_DB_NAME_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]{0,62}$")


class Settings(BaseSettings):
    """Typed environment configuration."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
    )

    # --- runtime -------------------------------------------------------
    app_env: Literal["dev", "test", "prod"] = "dev"
    log_level: str = "INFO"
    trust_proxy: bool = False  # honour X-Forwarded-For for rate limiting
    cors_origins: Annotated[list[str], NoDecode] = Field(default_factory=list)
    max_body_bytes: int = 1_048_576
    request_timeout_s: float = 30.0

    # --- database ------------------------------------------------------
    database_url: str = f"postgresql://{APP_ROLE_NAME}:{APP_ROLE_DEV_PASSWORD}@localhost:5432/stockpilot_dev"
    database_admin_url: str = "postgresql://postgres:postgres@localhost:5432/stockpilot_dev"
    db_pool_min: int = 1
    db_pool_max: int = 10
    db_admin_pool_max: int = 4
    db_statement_timeout_ms: int = 15_000
    app_db_password: str = APP_ROLE_DEV_PASSWORD  # applied to role stockpilot_app by the migrate CLI
    sp_test_db: str = "stockpilot_test"

    # --- auth ----------------------------------------------------------
    auth_mode: Literal["dev", "supabase"] = "dev"
    dev_jwt_secret: str = DEV_JWT_SECRET_DEFAULT
    dev_token_ttl_s: int = 28_800
    supabase_url: str = ""
    supabase_anon_key: str = ""
    supabase_service_role_key: str = ""
    supabase_jwt_secret: str = ""
    supabase_jwks_url: str = ""
    jwt_audience: str = "authenticated"
    jwt_issuer: str = ""  # optional; when set the `iss` claim must match
    jwt_leeway_s: int = 10
    jwks_cache_ttl_s: int = 600
    stream_token_ttl_s: int = 60

    # --- rate limiting -------------------------------------------------
    rate_limit_enabled: bool = True
    rate_limit_ip_burst: int = 200
    rate_limit_ip_per_s: float = 20.0
    rate_limit_user_burst: int = 120
    rate_limit_user_per_s: float = 10.0

    # --- cache / queue -------------------------------------------------
    redis_url: str = ""

    # --- market data ---------------------------------------------------
    market_data_source: Literal["simulated", "alpaca", "finnhub"] = "simulated"
    alpaca_key_id: str = ""
    alpaca_secret: str = ""
    finnhub_key: str = ""
    fred_api_key: str = ""
    sec_user_agent: str = "StockPilot <your-email>"

    # --- LLM -----------------------------------------------------------
    llm_provider: Literal["mock", "meta"] = "mock"
    meta_model_api_key: str = ""
    meta_api_base_url: str = "https://api.meta.ai/v1"
    model_standard: str = "muse-spark-1.3"
    model_contributor: str = "muse-spark-1.3-contributor"
    model_deep: str = "muse-wonder-1.0"
    llm_budget_alert_usd: float = 100.0
    llm_budget_hard_stop_usd: float = 140.0

    # --- observability -------------------------------------------------
    langfuse_public_key: str = ""
    langfuse_secret_key: str = ""
    langfuse_host: str = ""

    # --- paper trading -------------------------------------------------
    paper_slippage_bps: float = 5.0

    @model_validator(mode="before")
    @classmethod
    def _split_cors(cls, data: object) -> object:
        if isinstance(data, dict):
            raw = data.get("cors_origins")
            if isinstance(raw, str):
                data["cors_origins"] = [o.strip() for o in raw.split(",") if o.strip()]
        return data

    @model_validator(mode="after")
    def _check_auth(self) -> Settings:
        if self.app_env == "prod":
            if self.auth_mode == "dev":
                raise ValueError("AUTH_MODE=dev is not allowed when APP_ENV=prod")
            if "*" in self.cors_origins:
                raise ValueError("CORS_ORIGINS must not contain '*' when APP_ENV=prod")
        if self.auth_mode == "supabase" and not (self.supabase_jwt_secret or self.supabase_jwks_url):
            raise ValueError("AUTH_MODE=supabase needs SUPABASE_JWT_SECRET or SUPABASE_JWKS_URL")
        if self.auth_mode == "dev" and len(self.dev_jwt_secret.encode()) < 32:
            raise ValueError("DEV_JWT_SECRET must be at least 32 bytes")
        return self

    @property
    def docs_enabled(self) -> bool:
        return self.app_env != "prod"


@lru_cache
def get_settings() -> Settings:
    return Settings()


def dsn_with_database(dsn: str, dbname: str) -> str:
    """Return `dsn` pointing at database `dbname` (keeps host, user, query string)."""
    if not _DB_NAME_RE.match(dbname):
        raise ValueError(f"unsafe database name: {dbname!r}")
    parts = urlsplit(dsn)
    return urlunsplit((parts.scheme, parts.netloc, f"/{dbname}", parts.query, parts.fragment))


def dsn_database(dsn: str) -> str:
    """Database name of a DSN."""
    return urlsplit(dsn).path.lstrip("/")


def dsn_with_user(dsn: str, user: str, password: str | None) -> str:
    """Return `dsn` with credentials replaced (host/port preserved)."""
    parts = urlsplit(dsn)
    host = parts.hostname or ""
    if ":" in host:
        host = f"[{host}]"
    hostport = host + (f":{parts.port}" if parts.port else "")
    cred = user if password is None else f"{user}:{password}"
    query = urlencode(parse_qsl(parts.query)) if parts.query else ""
    return urlunsplit((parts.scheme, f"{cred}@{hostport}", parts.path, query, parts.fragment))
