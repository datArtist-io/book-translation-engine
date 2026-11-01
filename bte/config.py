"""Runtime settings, loaded from environment variables (and .env)."""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    anthropic_api_key: str = Field("", alias="ANTHROPIC_API_KEY")
    gemini_api_key: str = Field("", alias="GEMINI_API_KEY")
    google_translate_api_key: str = Field("", alias="GOOGLE_TRANSLATE_API_KEY")

    # "provider:model" specs. The judge must be a different model family from the
    # drafter for independent verification; the engine warns and raises risk if not.
    drafter: str = Field("anthropic:claude-opus-5-5", alias="BTE_DRAFTER")
    judge: str = Field("gemini:gemini-2.5-pro", alias="BTE_JUDGE")
    alternate: str = Field("gemini:gemini-2.5-pro", alias="BTE_ALTERNATE")
    nmt: str = Field("google_translate", alias="BTE_NMT")

    data_dir: Path = Field(Path("./data"), alias="BTE_DATA_DIR")
    database_url: str = Field("", alias="BTE_DATABASE_URL")

    target_major_rate: float = Field(0.01, alias="BTE_TARGET_MAJOR_RATE")
    refine_passes: int = Field(1, alias="BTE_REFINE_PASSES")
    max_repair_rungs: int = Field(5, alias="BTE_MAX_REPAIR_RUNGS")

    max_upload_mb: int = Field(100, alias="BTE_MAX_UPLOAD_MB")
    job_token_budget: int = Field(20_000_000, alias="BTE_JOB_TOKEN_BUDGET")
    provider_concurrency: int = Field(4, alias="BTE_PROVIDER_CONCURRENCY")
    chunk_chars: int = Field(4000, alias="BTE_CHUNK_CHARS")

    access_token: str = Field("", alias="BTE_ACCESS_TOKEN")

    @property
    def db_url(self) -> str:
        if self.database_url:
            return self.database_url
        self.data_dir.mkdir(parents=True, exist_ok=True)
        return f"sqlite:///{(self.data_dir / 'bte.db').resolve()}"


@lru_cache
def get_settings() -> Settings:
    return Settings()  # type: ignore[call-arg]


def reset_settings_cache() -> None:
    get_settings.cache_clear()
