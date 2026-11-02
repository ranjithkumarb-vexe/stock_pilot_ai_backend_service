"""Application settings (pydantic-settings). Every variable is documented in `.env.example`."""

from __future__ import annotations

import re
import secrets
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
    #: number of trusted reverse proxies in front of the app (Render = 1). The client IP is the entry that the
    #: outermost trusted proxy appended to X-Forwarded-For, i.e. counted from the RIGHT (SEC-08). Only used when
    #: TRUST_PROXY=true.
    trusted_proxy_hops: Annotated[int, Field(ge=1, le=10)] = 1
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
    #: no default on purpose (SEC-03, fail closed): `dev` mints tokens for any e-mail. An unset AUTH_MODE resolves to
    #: `supabase` when APP_ENV=prod and is a start-up error otherwise; local dev sets AUTH_MODE=dev explicitly
    #: (.env.example, `make`, docker-compose do).
    auth_mode: Literal["dev", "supabase"] = "dev"
    dev_jwt_secret: str = DEV_JWT_SECRET_DEFAULT
    dev_token_ttl_s: int = 28_800
    supabase_url: str = ""
    supabase_anon_key: str = ""
    supabase_service_role_key: str = ""
    supabase_jwt_secret: str = ""
    supabase_jwks_url: str = ""
    jwt_audience: str = "authenticated"
    jwt_issuer: str = ""  # when set the `iss` claim must match; REQUIRED when APP_ENV=prod (SEC-18)
    #: reject tokens whose `aal` claim is not `aal2` (TOTP MFA completed). None = default: on in prod, off elsewhere.
    require_mfa: bool | None = None
    jwt_leeway_s: int = 10
    jwks_cache_ttl_s: int = 600
    stream_token_ttl_s: int = 60

    # --- rate limiting -------------------------------------------------
    rate_limit_enabled: bool = True
    rate_limit_ip_burst: int = 400
    rate_limit_ip_per_s: float = 40.0
    rate_limit_user_burst: int = 300
    rate_limit_user_per_s: float = 30.0
    # tighter per-user bucket for the endpoints that can spend LLM budget: /v1/chat, /v1/briefs/*/generate,
    # /v1/proposals/refresh (SEC-17 / OWASP LLM10)
    rate_limit_expensive_burst: int = 30
    rate_limit_expensive_per_s: float = 0.5

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
        if "auth_mode" not in self.model_fields_set:
            if self.app_env == "prod":
                self.auth_mode = "supabase"
            else:
                raise ValueError(
                    "AUTH_MODE is not set: set AUTH_MODE=dev for local development (dev-login mints tokens for any "
                    "e-mail) or AUTH_MODE=supabase; production resolves it to supabase"
                )
        if self.require_mfa is None:
            self.require_mfa = self.app_env == "prod"
        if self.auth_mode == "supabase" and not (self.supabase_jwt_secret or self.supabase_jwks_url):
            raise ValueError("AUTH_MODE=supabase needs SUPABASE_JWT_SECRET or SUPABASE_JWKS_URL")
        if self.app_env == "prod":
            self._check_prod()
        if self.auth_mode == "dev" and len(self.dev_jwt_secret.encode()) < 32:
            raise ValueError("DEV_JWT_SECRET must be at least 32 bytes")
        return self

    def _check_prod(self) -> None:
        """APP_ENV=prod refuses to boot with dev defaults (SEC-02, SEC-18, CR-REL-2). All problems are reported at once."""
        if self.auth_mode == "dev":
            raise ValueError("AUTH_MODE=dev is not allowed when APP_ENV=prod")
        if "*" in self.cors_origins:
            raise ValueError("CORS_ORIGINS must not contain '*' when APP_ENV=prod")
        problems: list[str] = []
        if self.app_db_password in {"", APP_ROLE_DEV_PASSWORD}:
            problems.append("APP_DB_PASSWORD is empty or the public dev default")
        if urlsplit(self.database_url).password in {None, "", APP_ROLE_DEV_PASSWORD}:
            problems.append("DATABASE_URL has no password or the public dev password")
        if urlsplit(self.database_admin_url).password in {"postgres", "password", "admin", "root"}:
            problems.append("DATABASE_ADMIN_URL uses a well-known password (postgres:postgres)")
        if not self.jwt_issuer:
            problems.append(
                "JWT_ISSUER is required (strict iss check), e.g. https://<ref>.supabase.co/auth/v1"
            )
        ua = self.sec_user_agent.strip().strip("\"'")
        if not re.search(r"[^\s@<>]+@[^\s@<>]+\.[A-Za-z]{2,}", ua) or "your-email" in ua.lower():
            problems.append("SEC_USER_AGENT must declare a real contact e-mail (SEC fair access)")
        if self.llm_provider == "meta" and not self.meta_model_api_key:
            problems.append("LLM_PROVIDER=meta needs META_MODEL_API_KEY")
        if self.market_data_source == "alpaca" and not (self.alpaca_key_id and self.alpaca_secret):
            problems.append("MARKET_DATA_SOURCE=alpaca needs ALPACA_KEY_ID and ALPACA_SECRET")
        if self.market_data_source == "finnhub" and not self.finnhub_key:
            problems.append("MARKET_DATA_SOURCE=finnhub needs FINNHUB_KEY")
        if problems:
            raise ValueError("APP_ENV=prod refuses to start: " + "; ".join(problems))
        if self.dev_jwt_secret == DEV_JWT_SECRET_DEFAULT:
            # never verifies anything in prod (dev auth is refused above); make a leaked/public default useless anyway
            self.dev_jwt_secret = secrets.token_urlsafe(48)

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
