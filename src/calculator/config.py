"""Environment-driven settings. Lambda env vars are set in Terraform."""

from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="APP_")

    environment: str = "local"


@lru_cache
def get_settings() -> Settings:
    return Settings()
