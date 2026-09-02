"""Fixed provider contract for the Decision 043 acceptance runtime."""

from __future__ import annotations

from typing import Any


NARRATOR_MODEL = "openai/gpt-5.6-luna-pro"
NARRATOR_PROFILE_ID = "openrouter-openai-gpt-5-6-luna-pro"
NARRATOR_MAX_TOKEN_OPTIONS = [1024, 2048, 4096, 8192, 16384]


def normalize_provider(provider: str) -> str:
    value = provider.strip().lower()
    if value in {"nvidia", "nvidia-openai-compatible"}:
        return "nvidia"
    return value


def provider_base_url(settings: Any, provider: str) -> str:
    clean = normalize_provider(provider)
    if clean == "local":
        return str(settings.local_llm_base_url)
    if clean == "gemini":
        return str(settings.gemini_api_base)
    if clean == "openrouter":
        return str(settings.openrouter_api_base)
    raise ValueError(f"provider is retired or unsupported: {provider}")


def provider_api_key(settings: Any, provider: str) -> str:
    clean = normalize_provider(provider)
    if clean == "local":
        return ""
    if clean == "gemini":
        return str(settings.gemini_api_key)
    if clean == "openrouter":
        return str(settings.openrouter_api_key)
    raise ValueError(f"provider is retired or unsupported: {provider}")


def openrouter_model_is_active(model_id: str) -> bool:
    return model_id.strip().casefold() == NARRATOR_MODEL


def narrator_control_capabilities(provider: str, model_id: str) -> dict[str, Any]:
    if normalize_provider(provider) != "openrouter" or not openrouter_model_is_active(model_id):
        return {}
    return {
        "reasoning_efforts": ["none", "low", "medium", "high", "xhigh", "max"],
        "default_reasoning_effort": "medium",
        "temperature": False,
        "top_p": False,
        "max_tokens": list(NARRATOR_MAX_TOKEN_OPTIONS),
    }


def validate_narrator_settings(
    provider: str, model_id: str, settings: dict[str, Any]
) -> dict[str, Any]:
    if not settings:
        return {}
    capabilities = narrator_control_capabilities(provider, model_id)
    if not capabilities:
        raise ValueError("manual narrator settings are not supported by this model")
    effort = settings.get("reasoning_effort")
    if effort is not None and effort not in capabilities["reasoning_efforts"]:
        raise ValueError(f"reasoning effort {effort!r} is not supported by {model_id}")
    max_tokens = settings.get("max_tokens")
    if max_tokens is not None and max_tokens not in capabilities["max_tokens"]:
        raise ValueError(f"max_tokens {max_tokens!r} is not supported by {model_id}")
    return dict(settings)


def narrator_profile(settings: Any) -> dict[str, Any]:
    return {
        "id": NARRATOR_PROFILE_ID,
        "title": "GPT-5.6 Luna Pro",
        "provider": "openrouter",
        "base_url": str(settings.openrouter_api_base).rstrip("/"),
        "model": NARRATOR_MODEL,
        "params": {"context_tokens": 1_050_000},
        "api_key_source": "server_env_or_party_byok",
        "description": "Fixed Decision 043 narrator route through OpenRouter.",
        "rp_fit": "Acceptance narrator for the clean RP runtime.",
        "context_window": "1,050,000 tokens",
        "tags": ["fixed model", "exact provider route", "no fallback"],
        "source": "decision043",
        "availability": "OpenRouter API",
        "is_free": False,
        "pricing_prompt": "",
        "pricing_completion": "",
        "pricing_input_cache_read": "",
        "pricing_input_cache_write": "",
        "pricing_input_cache_write_1h": "",
        "rp_specialized": False,
    }
