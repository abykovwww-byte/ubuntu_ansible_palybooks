"""Environment configuration for the single Decision 043 RP runtime."""

from __future__ import annotations

import os
from dataclasses import dataclass


def env_bool(name: str, default: bool = False) -> bool:
    value = os.getenv(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


def env_int(name: str, default: int) -> int:
    value = os.getenv(name)
    if value is None or not value.strip():
        return default
    return int(value)


@dataclass(frozen=True)
class Settings:
    app_env: str = os.getenv("APP_ENV", "production")
    app_host: str = os.getenv("APP_HOST", "0.0.0.0")
    app_port: int = env_int("APP_PORT", 8088)
    scenario_type: str = os.getenv("SCENARIO_TYPE", "rp")
    database_url: str = os.getenv("DATABASE_URL", "sqlite:////data/rp_gateway.db")
    rp_database_url: str = os.getenv("RP_DATABASE_URL", "sqlite:////data/rp_engine.db")
    worldpacks_path: str = os.getenv("WORLD_PACKS_PATH", "/worldpacks")

    rp_narrator_enabled: bool = env_bool("RP_NARRATOR_ENABLED", True)
    rp_atomic_service_enabled: bool = env_bool("RP_ATOMIC_SERVICE_ENABLED", True)
    rp_administrator_enabled: bool = env_bool("RP_ADMINISTRATOR_ENABLED", True)
    rp_derived_wait_seconds: float = float(os.getenv("RP_DERIVED_WAIT_SECONDS", "15"))
    rp_runner_poll_interval_seconds: float = float(
        os.getenv("RP_RUNNER_POLL_INTERVAL_SECONDS", "0.05")
    )

    openrouter_api_base: str = os.getenv(
        "OPENROUTER_API_BASE", "https://openrouter.ai/api/v1"
    )
    openrouter_api_key: str = os.getenv("OPENROUTER_API_KEY", "")
    service_openrouter_api_key: str = os.getenv("SERVICE_OPENROUTER_API_KEY", "")
    gemini_api_base: str = os.getenv(
        "GEMINI_API_BASE",
        "https://generativelanguage.googleapis.com/v1beta/openai",
    )
    gemini_api_key: str = os.getenv("GEMINI_API_KEY", "")
    llm_api_key: str = ""
    local_llm_enabled: bool = env_bool("LOCAL_LLM_ENABLED", True)
    local_llm_base_url: str = os.getenv(
        "LOCAL_LLM_BASE_URL", "http://rp-local-llm:8080/v1"
    )
    local_llm_model_alias: str = os.getenv(
        "LOCAL_LLM_MODEL_ALIAS", "gemma-4-26b-a4b-it-rp-q4"
    )
    local_llm_context_tokens: int = env_int("LOCAL_LLM_CONTEXT_TOKENS", 32_768)
    model_attempt_timeout_seconds: float = float(
        os.getenv("MODEL_ATTEMPT_TIMEOUT_SECONDS", "150")
    )

    log_level: str = os.getenv("LOG_LEVEL", "INFO")
    auth_enabled: bool = env_bool("GATEWAY_AUTH_ENABLED", True)
    auth_session_cookie_name: str = os.getenv(
        "GATEWAY_SESSION_COOKIE_NAME", "rp_gateway_session"
    )
    auth_session_ttl_seconds: int = env_int(
        "GATEWAY_SESSION_TTL_SECONDS", 60 * 60 * 24 * 14
    )
    auth_cookie_secure: bool = env_bool("GATEWAY_COOKIE_SECURE", False)
    bootstrap_admin_username: str = os.getenv(
        "GATEWAY_BOOTSTRAP_ADMIN_USERNAME", "admin"
    )
    bootstrap_admin_password: str = os.getenv(
        "GATEWAY_BOOTSTRAP_ADMIN_PASSWORD", ""
    )

    @property
    def sqlite_path(self) -> str:
        return _sqlite_path(self.database_url, "DATABASE_URL")

    @property
    def rp_sqlite_path(self) -> str:
        return _sqlite_path(self.rp_database_url, "RP_DATABASE_URL")


def _sqlite_path(value: str, name: str) -> str:
    prefix = "sqlite:///"
    if not value.startswith(prefix):
        raise ValueError(f"Only sqlite:/// {name} values are supported")
    return value[len(prefix) :]


def get_settings() -> Settings:
    return Settings()
