"""
src/instances/config.py
Application configuration singleton loaded from environment variables.
"""
from __future__ import annotations

from functools import lru_cache
from typing import List

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    # ── App ──────────────────────────────────────────────────────────────
    app_env: str
    app_secret_key: str
    app_password: str = "admin"
    app_host: str
    app_port: int
    app_debug: bool
    app_website_url: str = "http://35.212.162.76:5173"


    # ── SMTP (Email) ──────────────────────────────────────────────────────
    smtp_host: str = "smtp.gmail.com"
    smtp_port: int = 587
    smtp_user: str = ""
    smtp_password: str = ""
    smtp_sender: str = ""

    # ── Database ──────────────────────────────────────────────────────────
    database_url: str
    # Disable for a Postgres container reached over the private Docker network
    database_ssl: bool = True

    # ── Gemini ────────────────────────────────────────────────────────────
    gemini_api_key: str
    gemini_model: str
    fallback_models: str = "gemini-3.1-flash-lite,gemma-4-26b-it,gemma-4-31b-it,gemini-2.5-flash-lite"

    # ── 永豐金 (Sinopac / 豐存股) ─────────────────────────────────────────
    sinopac_api_base_url: str
    sinopac_account_id: str
    sinopac_api_key: str
    sinopac_api_secret: str
    sinopac_cert_path: str
    sinopac_cert_password: str

    # ── 台新證券 (Taishin) ─────────────────────────────────────────────────
    taishin_api_base_url: str
    taishin_account_id: str
    taishin_account_password: str | None = None
    taishin_api_key: str
    taishin_api_secret: str
    taishin_cert_path: str
    taishin_cert_password: str

    # ── File Upload ───────────────────────────────────────────────────────
    upload_dir: str
    max_upload_size_mb: int



    # ── CORS ──────────────────────────────────────────────────────────────
    cors_origins: List[str]

    # ── Business Logic ────────────────────────────────────────────────────
    # Account IDs that belong to the user — inter-account transfers are excluded
    # from income statement expense calculations
    internal_account_ids: List[str]

    @property
    def is_production(self) -> bool:
        return self.app_env == "production"


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Return cached settings singleton."""
    return Settings()

def reload_settings() -> Settings:
    """Clear cached settings and return a re-instantiated Settings object."""
    get_settings.cache_clear()
    return get_settings()
