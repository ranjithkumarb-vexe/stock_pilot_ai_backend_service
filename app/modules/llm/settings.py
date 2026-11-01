"""LLM knobs that are not in `app.core.config.Settings` (read from the same `.env`/environment)."""

from __future__ import annotations

from functools import lru_cache
from typing import Literal

from pydantic_settings import BaseSettings, SettingsConfigDict


class LlmSettings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env", env_file_encoding="utf-8", extra="ignore", case_sensitive=False
    )

    llm_user_daily_budget_usd: float = 1.00  # per-user daily cap
    llm_timeout_s: float = 60.0
    llm_max_retries: int = 3  # retries after the first attempt
    llm_backoff_base_s: float = 0.5
    llm_backoff_cap_s: float = 20.0
    llm_reasoning_effort: Literal["minimal", "low", "medium", "high"] = "low"
    # UNVERIFIED placeholder prices (USD per 1M tokens): the Meta Model API price list is not confirmed
    # (docs/VERIFIED.md). They only feed cost accounting / budgets.
    llm_price_in_per_mtok: float = 1.00
    llm_price_out_per_mtok: float = 4.00


@lru_cache
def get_llm_settings() -> LlmSettings:
    return LlmSettings()
