"""Environment-driven settings."""
from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    database_url: str = "postgresql+psycopg://postgres:postgres@localhost:5432/assess"
    test_database_url: str = "postgresql+psycopg://postgres:postgres@localhost:5432/assess_test"
    jwt_secret: str = "dev-only-change-me-use-a-long-random-secret-32b"
    upload_dir: str = "./uploads"

    llm_provider: str = "mock"  # mock | ollama | gemini
    ollama_url: str = "http://localhost:11434"
    ollama_model: str = "llama3.1"
    gemini_api_key: str = ""
    gemini_model: str = "gemini-1.5-flash"
    llm_token_budget_per_eval: int = 60000

    google_api_key: str = ""
    whisper_enabled: bool = False

    job_max_attempts: int = 3
    job_lease_seconds: int = 60


@lru_cache
def get_settings() -> Settings:
    return Settings()
