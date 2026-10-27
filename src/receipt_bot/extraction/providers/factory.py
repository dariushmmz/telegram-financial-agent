from __future__ import annotations

from typing import Any

import httpx

from receipt_bot.config import AppSettings
from receipt_bot.extraction.errors import ExtractionConfigurationError
from receipt_bot.extraction.providers.gemini import GeminiProvider
from receipt_bot.extraction.providers.openrouter import OpenRouterProvider


def _secret_value(secret) -> str | None:
    return secret.get_secret_value() if secret is not None else None


def build_provider(
    settings: AppSettings,
    *,
    gemini_client: Any | None = None,
    openrouter_client: httpx.Client | None = None,
):
    if settings.extraction_provider == "gemini":
        api_key = _secret_value(settings.gemini_api_key)
        if not api_key:
            raise ExtractionConfigurationError("GEMINI_API_KEY is required for the Gemini provider")
        return GeminiProvider(
            api_key=api_key,
            models=settings.configured_gemini_models,
            client=gemini_client,
            timeout_seconds=settings.provider_request_timeout_seconds,
        )
    if settings.extraction_provider == "openrouter":
        api_key = _secret_value(settings.openrouter_api_key)
        if not api_key:
            raise ExtractionConfigurationError("OPENROUTER_API_KEY is required for the OpenRouter provider")
        if not settings.openrouter_model:
            raise ExtractionConfigurationError("an OpenRouter model must be configured")
        return OpenRouterProvider(
            api_key=api_key,
            model=settings.openrouter_model,
            client=openrouter_client,
            timeout_seconds=settings.provider_request_timeout_seconds,
        )
    raise ExtractionConfigurationError("unsupported extraction provider")
