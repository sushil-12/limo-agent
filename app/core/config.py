"""Application settings loaded from the environment / .env file.

Database variable names deliberately match Laravel's .env (DB_HOST, DB_DATABASE, ...)
so values can be copied across unchanged. Settings are validated at startup: a missing
or unsafe value stops the process instead of failing on the first request.
"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from typing import Annotated, Literal

import orjson
from pydantic import Field, SecretStr, field_validator, model_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict

AppEnv = Literal["local", "testing", "staging", "production"]

MIN_SERVICE_KEY_LENGTH = 32


class Settings(BaseSettings):
    """Typed service configuration. Field names map to upper-case env vars."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
    )

    # --- Application -------------------------------------------------------------
    app_env: AppEnv = "local"
    log_level: str = "INFO"
    cors_origins: Annotated[list[str], NoDecode] = Field(default_factory=list)
    service_api_key: SecretStr | None = None
    enable_debug_routes: bool = False
    expose_docs: bool | None = None  # None = enabled everywhere except production
    max_request_body_bytes: int = Field(default=512 * 1024, gt=0)
    rate_limit_per_minute: int = Field(default=20, gt=0)
    topic_filter_enabled: bool = True  # classify each message; off-topic gets a fixed reply
    # JSONL log of every chat turn: question, reply, reply type, timings. Empty = off.
    chat_log_path: Path | None = None

    # --- Guest chat (no login; identified and rate limited by client IP) -----------
    guest_chat_enabled: bool = True
    guest_rate_limit_per_minute: int = Field(default=5, gt=0)
    guest_rate_limit_per_hour: int = Field(default=30, gt=0)
    guest_max_messages: int = Field(default=10, ge=1, le=30)  # newest history messages kept
    sse_ping_interval_seconds: float = Field(default=15.0, gt=0)
    health_check_timeout_seconds: float = Field(default=2.0, gt=0)

    # --- Database (names shared with Laravel's .env) ------------------------------
    db_host: str = "127.0.0.1"
    db_port: int = 3306
    db_database: str
    db_username: str
    db_password: SecretStr
    db_pool_size: int = Field(default=10, gt=0)
    db_max_overflow: int = Field(default=10, ge=0)
    db_pool_recycle: int = Field(default=1800, gt=0)
    db_pool_timeout: float = Field(default=5.0, gt=0)
    db_connect_timeout: int = Field(default=5, gt=0)
    db_echo: bool = False
    db_timezone: str = "+00:00"  # Laravel config/app.php 'timezone' => 'UTC'
    db_read_only: bool = True

    # --- LLM ---------------------------------------------------------------------
    llm_provider: str = "ollama"
    ollama_base_url: str = "http://localhost:11434"
    ollama_model: str = "llama3.1:8b"
    ollama_keep_alive: str = "30m"
    ollama_num_ctx: int = Field(default=8192, ge=512)
    ollama_num_predict: int = Field(default=1024, gt=0)
    ollama_temperature: float = Field(default=0.3, ge=0, le=2)
    ollama_timeout: float = Field(default=120.0, gt=0)  # read timeout between chunks
    ollama_connect_timeout: float = Field(default=3.0, gt=0)
    ollama_max_connections: int = Field(default=50, gt=0)
    ollama_warmup: bool = True
    # Optional separate model for writing SQL (e.g. qwen2.5-coder:7b). Empty = OLLAMA_MODEL.
    ollama_sql_model: str | None = None

    # --- Data questions (text-to-SQL, read only) -------------------------------------
    data_queries_enabled: bool = True
    data_max_rows: int = Field(default=200, ge=1, le=1000)  # rows returned per query
    data_query_timeout_seconds: float = Field(default=5.0, gt=0)  # MySQL max_execution_time
    data_max_attempts: int = Field(default=3, ge=1, le=5)  # SQL writes incl. self-corrections
    data_schema_cache_seconds: float = Field(default=900.0, ge=0)
    # JSONL log of every data question and the SQL written for it (no result rows). Empty = off.
    data_query_log_path: Path | None = None
    # Learned question -> SQL pairs: a repeated question reuses its SQL instead of asking
    # the model to write it again. Empty = off.
    data_sql_cache_path: Path | None = None
    data_sql_cache_max_entries: int = Field(default=5000, ge=1)

    # --- Auth (Laravel Passport) ---------------------------------------------------
    passport_public_key: SecretStr | None = None
    passport_public_key_path: Path | None = None
    passport_leeway_seconds: int = Field(default=5, ge=0)
    auth_allowed_roles: dict[int, str] = Field(default_factory=lambda: {5: "rider", 2: "driver"})
    auth_cache_ttl_seconds: float = Field(default=60.0, ge=0)
    auth_cache_max_entries: int = Field(default=10_000, gt=0)

    # --- Booking a ride (shell :book and the web chat) --------------------------------
    limo_api_base_url: str = "https://api.1800limo.com/api/"  # the web app's environment.serverUrl
    google_maps_api_key: SecretStr | None = None  # server-side Places (New) + Directions key
    chat_booking_enabled: bool = True  # "book a ride ..." in /v1/chat starts the booking flow
    chat_booking_ttl_seconds: float = Field(default=1800.0, gt=0)  # idle bookings are dropped

    @field_validator(
        "service_api_key",
        "expose_docs",
        "passport_public_key",
        "passport_public_key_path",
        "ollama_sql_model",
        "data_query_log_path",
        "data_sql_cache_path",
        "chat_log_path",
        "google_maps_api_key",
        mode="before",
    )
    @classmethod
    def _empty_is_unset(cls, value: object) -> object:
        """Treat ``KEY=`` in .env as not set."""
        return None if isinstance(value, str) and not value.strip() else value

    @field_validator("cors_origins", mode="before")
    @classmethod
    def _split_origins(cls, value: object) -> object:
        """Accept a JSON list or a comma-separated string."""
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
    def _check_safety(self) -> Settings:
        """Refuse configurations that are unsafe or incomplete."""
        if self.passport_public_key is None and self.passport_public_key_path is None:
            raise ValueError("PASSPORT_PUBLIC_KEY or PASSPORT_PUBLIC_KEY_PATH must be set")
        if self.app_env != "local" and "*" in self.cors_origins:
            raise ValueError("CORS_ORIGINS may not contain '*' outside APP_ENV=local")
        if self.is_production and self.enable_debug_routes:
            raise ValueError("ENABLE_DEBUG_ROUTES must be false in production")
        if self.enable_debug_routes and self.service_api_key is None:
            raise ValueError("ENABLE_DEBUG_ROUTES=true requires SERVICE_API_KEY")
        key = self.service_api_key.get_secret_value() if self.service_api_key else None
        if key is not None and len(key) < MIN_SERVICE_KEY_LENGTH:
            raise ValueError(f"SERVICE_API_KEY must be at least {MIN_SERVICE_KEY_LENGTH} chars")
        if not self.auth_allowed_roles:
            raise ValueError("AUTH_ALLOWED_ROLES must allow at least one role")
        return self

    @property
    def is_production(self) -> bool:
        """True when running with APP_ENV=production."""
        return self.app_env == "production"

    @property
    def docs_enabled(self) -> bool:
        """Whether /docs and /openapi.json are served."""
        return self.expose_docs if self.expose_docs is not None else not self.is_production

    def passport_public_key_pem(self) -> str:
        """Return the Passport RSA public key as PEM text.

        Laravel .env files often store the key with literal ``\\n`` sequences; both
        forms are accepted.
        """
        if self.passport_public_key is not None:
            return self.passport_public_key.get_secret_value().replace("\\n", "\n")
        if self.passport_public_key_path is None:
            raise ValueError("PASSPORT_PUBLIC_KEY or PASSPORT_PUBLIC_KEY_PATH must be set")
        return self.passport_public_key_path.read_text(encoding="utf-8")


@lru_cache
def get_settings() -> Settings:
    """Load settings once per process."""
    return Settings()  # type: ignore[call-arg]  # values come from the environment
