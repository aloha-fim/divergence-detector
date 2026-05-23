"""Application settings, loaded from env."""
from functools import lru_cache
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    database_url: str = "postgresql+asyncpg://divergence:divergence@localhost:5433/divergence"
    anthropic_api_key: str = ""
    use_mock_llm: bool = False
    cors_origins: str = "*"

    # Scoring
    default_lookback_days: int = 252
    alert_threshold_z: float = 2.0
    narrative_threshold_z: float = 2.5

    # Narrative
    prompt_version: str = "v4-2026-06"
    llm_model: str = "claude-opus-4-7"
    llm_temperature: float = 0.3

    # Worker
    scoring_interval_minutes: int = 15


@lru_cache
def get_settings() -> Settings:
    return Settings()
