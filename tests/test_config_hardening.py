"""SEC-02 / SEC-03 / SEC-04 / SEC-18 / CR-REL-2: production start-up guards and fail-closed auth defaults."""

from __future__ import annotations

from typing import Any

import pytest

from app.core.config import APP_ROLE_DEV_PASSWORD, DEV_JWT_SECRET_DEFAULT, Settings
from tests.prod_config import prod_kwargs


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in ("AUTH_MODE", "APP_ENV", "REQUIRE_MFA", "TRUSTED_PROXY_HOPS"):
        monkeypatch.delenv(name, raising=False)


def test_valid_prod_config_boots_and_defaults_to_mfa_on() -> None:
    s = Settings(**prod_kwargs())
    assert s.auth_mode == "supabase" and s.require_mfa is True


# ------------------------------------------------------------------------------------------------ SEC-03
def test_unset_auth_mode_is_a_startup_error_outside_prod() -> None:
    with pytest.raises(ValueError, match="AUTH_MODE is not set"):
        Settings(_env_file=None)
    with pytest.raises(ValueError, match="AUTH_MODE is not set"):
        Settings(_env_file=None, app_env="test")


def test_unset_auth_mode_resolves_to_supabase_in_prod() -> None:
    kw = prod_kwargs()
    del kw["auth_mode"]
    assert Settings(**kw).auth_mode == "supabase"
    # ... and prod without any Supabase key material still refuses to start
    kw2 = prod_kwargs()
    del kw2["auth_mode"], kw2["supabase_jwks_url"]
    with pytest.raises(ValueError, match="SUPABASE_JWT_SECRET or SUPABASE_JWKS_URL"):
        Settings(**kw2)


def test_explicit_dev_mode_still_works_for_local_development() -> None:
    s = Settings(_env_file=None, auth_mode="dev")
    assert s.auth_mode == "dev" and s.app_env == "dev" and s.require_mfa is False


def test_auth_mode_from_environment_counts_as_explicit(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("AUTH_MODE", "dev")
    assert Settings(_env_file=None).auth_mode == "dev"
    monkeypatch.delenv("AUTH_MODE")
    with pytest.raises(ValueError, match="AUTH_MODE is not set"):
        Settings(_env_file=None)


# ------------------------------------------------------------------------------------------------ SEC-02 / 18
@pytest.mark.parametrize(
    ("over", "needle"),
    [
        ({"app_db_password": APP_ROLE_DEV_PASSWORD}, "APP_DB_PASSWORD"),
        ({"app_db_password": ""}, "APP_DB_PASSWORD"),
        ({"database_url": f"postgresql://stockpilot_app:{APP_ROLE_DEV_PASSWORD}@db/x"}, "DATABASE_URL"),
        ({"database_url": "postgresql://stockpilot_app@db/x"}, "DATABASE_URL"),
        ({"database_admin_url": "postgresql://postgres:postgres@db/x"}, "DATABASE_ADMIN_URL"),
        ({"jwt_issuer": ""}, "JWT_ISSUER"),
        ({"sec_user_agent": "StockPilot <your-email>"}, "SEC_USER_AGENT"),
        ({"sec_user_agent": "StockPilot your-email@example.com"}, "SEC_USER_AGENT"),
        ({"sec_user_agent": "StockPilot"}, "SEC_USER_AGENT"),
        ({"llm_provider": "meta"}, "META_MODEL_API_KEY"),
        ({"market_data_source": "alpaca"}, "ALPACA_KEY_ID"),
        ({"market_data_source": "alpaca", "alpaca_key_id": "k"}, "ALPACA_SECRET"),
        ({"market_data_source": "finnhub"}, "FINNHUB_KEY"),
    ],
)
def test_prod_refuses_unsafe_or_incomplete_values(over: dict[str, Any], needle: str) -> None:
    with pytest.raises(ValueError, match=needle):
        Settings(**prod_kwargs(**over))


def test_prod_reports_every_problem_at_once() -> None:
    with pytest.raises(ValueError, match="refuses to start") as ei:
        Settings(**prod_kwargs(app_db_password=APP_ROLE_DEV_PASSWORD, jwt_issuer="", llm_provider="meta"))
    msg = str(ei.value)
    assert "APP_DB_PASSWORD" in msg and "JWT_ISSUER" in msg and "META_MODEL_API_KEY" in msg


def test_prod_accepts_real_keys_for_optional_providers() -> None:
    Settings(**prod_kwargs(llm_provider="meta", meta_model_api_key="k"))
    Settings(**prod_kwargs(market_data_source="alpaca", alpaca_key_id="k", alpaca_secret="s"))
    Settings(**prod_kwargs(market_data_source="finnhub", finnhub_key="k"))


def test_non_prod_environments_keep_dev_defaults_usable() -> None:
    s = Settings(_env_file=None, auth_mode="dev", llm_provider="meta")  # dev: degrades to the mock provider
    assert s.app_db_password == APP_ROLE_DEV_PASSWORD and s.jwt_issuer == ""


def test_prod_replaces_the_public_default_dev_jwt_secret() -> None:
    s = Settings(**prod_kwargs())
    assert s.dev_jwt_secret != DEV_JWT_SECRET_DEFAULT and len(s.dev_jwt_secret) >= 32
    keep = Settings(**prod_kwargs(dev_jwt_secret="x" * 40))
    assert keep.dev_jwt_secret == "x" * 40


# ------------------------------------------------------------------------------------------------ SEC-04
def test_require_mfa_defaults_by_environment_and_can_be_overridden() -> None:
    assert Settings(_env_file=None, auth_mode="dev").require_mfa is False
    assert Settings(**prod_kwargs()).require_mfa is True
    assert Settings(**prod_kwargs(require_mfa=False)).require_mfa is False
    assert Settings(_env_file=None, auth_mode="dev", require_mfa=True).require_mfa is True


def test_trusted_proxy_hops_default_and_bounds() -> None:
    assert Settings(_env_file=None, auth_mode="dev").trusted_proxy_hops == 1
    with pytest.raises(ValueError, match="greater than or equal to 1"):
        Settings(_env_file=None, auth_mode="dev", trusted_proxy_hops=0)


def test_env_example_loads_and_is_explicit_dev() -> None:
    """`cp .env.example .env` must keep working: it chooses AUTH_MODE=dev itself (SEC-03) and documents the new knobs."""
    from pathlib import Path

    path = Path(__file__).resolve().parents[1] / ".env.example"
    s = Settings(_env_file=path)
    assert (
        s.auth_mode == "dev" and s.app_env == "dev" and s.trusted_proxy_hops == 1 and s.require_mfa is False
    )
    text = path.read_text()
    for knob in ("TRUSTED_PROXY_HOPS", "REQUIRE_MFA", "JWT_ISSUER", "AUTH_MODE"):
        assert knob in text
