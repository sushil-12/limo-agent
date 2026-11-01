"""Settings for the realtime service.

Shares the database host/name and the Passport key with the chatbot (same .env names),
but connects with its own writer account: RT_DB_USERNAME / RT_DB_PASSWORD. The chatbot's
DB_USERNAME/DB_PASSWORD are never used here, and the writer password is never read by
the chatbot's Settings.
"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from typing import Annotated

import orjson
from pydantic import Field, SecretStr, field_validator, model_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict

from app.core.config import AppEnv

MIN_SECRET_LENGTH = 32


class RealtimeSettings(BaseSettings):
    """Typed configuration for limo-rt. Field names map to upper-case env vars."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
    )

    # --- Application -------------------------------------------------------------
    app_env: AppEnv = "local"
    log_level: str = "INFO"
    rt_cors_origins: Annotated[list[str], NoDecode] = Field(default_factory=list)
    rt_expose_docs: bool | None = None

    # --- Database: shared host/name, separate writer account ------------------------
    db_host: str = "127.0.0.1"
    db_port: int = 3306
    db_database: str
    db_username: str | None = None  # the chatbot's read-only user; only used for a safety check
    db_timezone: str = "+00:00"
    rt_db_username: str
    rt_db_password: SecretStr
    rt_db_pool_size: int = Field(default=5, gt=0)
    rt_db_max_overflow: int = Field(default=5, ge=0)
    rt_db_pool_recycle: int = Field(default=1800, gt=0)
    rt_db_connect_timeout: int = Field(default=5, gt=0)

    # --- Auth ----------------------------------------------------------------------
    passport_public_key: SecretStr | None = None
    passport_public_key_path: Path | None = None
    passport_leeway_seconds: int = Field(default=5, ge=0)
    rt_auth_allowed_roles: dict[int, str] = Field(
        default_factory=lambda: {5: "rider", 2: "driver", 1: "admin"}
    )
    rt_auth_cache_ttl_seconds: float = Field(default=60.0, ge=0)
    # Laravel -> limo-rt calls are signed with this (HMAC-SHA256). Never the old shared secret.
    rt_internal_secret: SecretStr
    # Signs public share links for the web tracking page.
    rt_share_token_secret: SecretStr
    rt_share_token_max_hours: float = Field(default=24.0, gt=0)

    # --- Redis ---------------------------------------------------------------------
    redis_url: str = "redis://127.0.0.1:6379/0"

    # --- Laravel (outbound: SMS) -----------------------------------------------------
    laravel_api_base_url: str = "https://api.1800limo.com/api/"
    laravel_timeout_seconds: float = Field(default=8.0, gt=0)
    # Path of the internal endpoint that dispatches the ride-completed SMS job.
    laravel_ride_completed_path: str = "internal/rides/{ride_id}/completed-sms"

    # --- FCM (HTTP v1) ---------------------------------------------------------------
    fcm_project_id: str | None = None
    fcm_credentials_path: Path | None = None
    fcm_credentials_json: SecretStr | None = None
    fcm_topic_prefix: str | None = None  # default: "production" in production, else "staging"

    # --- Licence scanning (Google Document AI); same names as limoRTService's .env -----
    documentai_project_id: str | None = None
    documentai_location: str = "us"
    documentai_processor_id: str | None = None
    documentai_credentials_json: SecretStr | None = None
    google_application_credentials: Path | None = None  # service-account JSON file
    documentai_timeout_ms: int = Field(default=30_000, gt=0)
    documentai_max_file_size_bytes: int = Field(default=5 * 1024 * 1024, gt=0)
    rt_ocr_rate_per_minute: int = Field(default=10, gt=0)  # per user

    # --- Tuning ----------------------------------------------------------------------
    rt_hello_timeout_seconds: float = Field(default=5.0, gt=0)
    rt_ping_interval_seconds: float = Field(default=20.0, gt=0)
    rt_idle_timeout_seconds: float = Field(default=45.0, gt=0)  # no frame from the client
    rt_ride_auth_ttl_seconds: int = Field(default=60, gt=0)
    rt_location_ttl_seconds: int = Field(default=300, gt=0)
    rt_location_max_per_second: float = Field(default=5.0, gt=0)
    rt_stream_max_entries: int = Field(default=1000, gt=10)
    rt_stream_ttl_active_seconds: int = Field(default=7 * 86400, gt=0)
    rt_stream_ttl_ended_seconds: int = Field(default=86400, gt=0)
    rt_presence_ttl_seconds: int = Field(default=45, gt=0)
    rt_push_ack_timeout_seconds: float = Field(default=5.0, gt=0)
    rt_trail_min_seconds: float = Field(default=10.0, gt=0)
    rt_trail_min_meters: float = Field(default=25.0, gt=0)
    rt_trail_flush_seconds: float = Field(default=30.0, gt=0)
    rt_proximity_interval_seconds: float = Field(default=5.0, gt=0)
    rt_sms_cap_per_ride: int = Field(default=3, ge=0)
    rt_retention_months: int = Field(default=12, ge=1)
    rt_retention_enabled: bool = False  # deletes nothing until an archive target is set up
    rt_flags_refresh_seconds: float = Field(default=30.0, gt=0)
    # Reservation watcher: Laravel-side cancels, reassignments and status edits.
    rt_watch_interval_seconds: float = Field(default=3.0, gt=0)
    rt_watch_sweep_seconds: float = Field(default=60.0, gt=0)

    @field_validator(
        "passport_public_key",
        "passport_public_key_path",
        "fcm_project_id",
        "fcm_credentials_path",
        "fcm_credentials_json",
        "fcm_topic_prefix",
        "rt_expose_docs",
        "db_username",
        "documentai_project_id",
        "documentai_processor_id",
        "documentai_credentials_json",
        "google_application_credentials",
        mode="before",
    )
    @classmethod
    def _empty_is_unset(cls, value: object) -> object:
        """Treat ``KEY=`` in .env as not set."""
        return None if isinstance(value, str) and not value.strip() else value

    @field_validator("rt_cors_origins", mode="before")
    @classmethod
    def _split_origins(cls, value: object) -> object:
        if isinstance(value, str):
            stripped = value.strip()
            if stripped.startswith("["):
                return orjson.loads(stripped)
            return [item.strip() for item in stripped.split(",") if item.strip()]
        return value

    @field_validator("log_level")
    @classmethod
    def _upper_log_level(cls, value: str) -> str:
        return value.upper()

    @model_validator(mode="after")
    def _check_safety(self) -> RealtimeSettings:
        if self.passport_public_key is None and self.passport_public_key_path is None:
            raise ValueError("PASSPORT_PUBLIC_KEY or PASSPORT_PUBLIC_KEY_PATH must be set")
        if self.db_username is not None and self.rt_db_username == self.db_username:
            # The writer must be its own account, so the chatbot's account can stay read-only.
            raise ValueError("RT_DB_USERNAME must differ from the chatbot's DB_USERNAME")
        for name in ("rt_internal_secret", "rt_share_token_secret"):
            if len(getattr(self, name).get_secret_value()) < MIN_SECRET_LENGTH:
                raise ValueError(f"{name.upper()} must be at least {MIN_SECRET_LENGTH} chars")
        if self.rt_internal_secret.get_secret_value() == self.rt_share_token_secret.get_secret_value():
            raise ValueError("RT_INTERNAL_SECRET and RT_SHARE_TOKEN_SECRET must differ")
        return self

    @property
    def is_production(self) -> bool:
        return self.app_env == "production"

    @property
    def docs_enabled(self) -> bool:
        return self.rt_expose_docs if self.rt_expose_docs is not None else not self.is_production

    @property
    def topic_prefix(self) -> str:
        """FCM topic prefix: broadcasts go to ``{prefix}_all_drivers`` etc."""
        if self.fcm_topic_prefix:
            return self.fcm_topic_prefix
        return "production" if self.is_production else "staging"

    @property
    def fcm_enabled(self) -> bool:
        return self.fcm_project_id is not None and (
            self.fcm_credentials_path is not None or self.fcm_credentials_json is not None
        )

    def passport_public_key_pem(self) -> str:
        if self.passport_public_key is not None:
            return self.passport_public_key.get_secret_value().replace("\\n", "\n")
        if self.passport_public_key_path is None:
            raise ValueError("PASSPORT_PUBLIC_KEY or PASSPORT_PUBLIC_KEY_PATH must be set")
        return self.passport_public_key_path.read_text(encoding="utf-8")

    def documentai_credentials(self) -> dict[str, str] | None:
        """Service-account JSON for Document AI (inline wins over the file, like Node)."""
        if self.documentai_credentials_json is not None:
            return orjson.loads(self.documentai_credentials_json.get_secret_value())
        if self.google_application_credentials is not None:
            return orjson.loads(self.google_application_credentials.read_bytes())
        return None

    def fcm_credentials(self) -> dict[str, str] | None:
        """The Firebase service-account JSON, or None when FCM is not configured."""
        if self.fcm_credentials_json is not None:
            return orjson.loads(self.fcm_credentials_json.get_secret_value())
        if self.fcm_credentials_path is not None:
            return orjson.loads(self.fcm_credentials_path.read_bytes())
        return None


@lru_cache
def get_realtime_settings() -> RealtimeSettings:
    """Load settings once per process."""
    return RealtimeSettings()  # type: ignore[call-arg]  # values come from the environment
