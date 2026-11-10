from __future__ import annotations

import json

import httpx
import pytest

from receipt_bot.config import AppSettings
from receipt_bot.extraction.errors import (
    ExtractionConfigurationError,
    MalformedProviderResponseError,
    ProviderAuthenticationError,
    ProviderAuthorizationError,
    ProviderModelUnavailableError,
    ProviderPaymentRequiredError,
    ProviderRateLimitError,
    ProviderRefusalError,
    ProviderTimeoutError,
    ProviderUnavailableError,
)
from receipt_bot.extraction.providers.factory import build_provider
from receipt_bot.extraction.providers.onexai import OneXAIProvider, map_onexai_status
from receipt_bot.extraction.schema import ProviderReceipt

from test_extraction_providers import candidate_payload, prepared_image


def onexai_response(
    payload: dict | None = None,
    *,
    finish_reason: str = "STOP",
    status: int = 200,
) -> httpx.Response:
    return httpx.Response(
        status,
        headers={"x-request-id": "1x-request-1"},
        json={
            "candidates": [
                {
                    "finishReason": finish_reason,
                    "content": {
                        "role": "model",
                        "parts": [{"text": json.dumps(payload or candidate_payload())}],
                    },
                }
            ],
            "modelVersion": "gemini-test-version",
            "usageMetadata": {"promptTokenCount": 10, "totalTokenCount": 30},
        },
    )


def test_onexai_image_contract_uses_native_gemini_schema() -> None:
    captured: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["url"] = str(request.url)
        captured["key"] = request.headers["x-goog-api-key"]
        captured["payload"] = json.loads(request.content)
        return onexai_response()

    provider = OneXAIProvider(
        api_key="test-key",
        model="gemini-2.5-flash",
        client=httpx.Client(transport=httpx.MockTransport(handler)),
    )
    result = provider.extract(prepared_image())

    assert result.provider == "1xai"
    assert result.model == "gemini-test-version"
    assert result.request_id == "1x-request-1"
    assert result.candidate.source_name == "الناز"
    assert captured["key"] == "test-key"
    assert captured["url"].endswith(
        "/gemini/v1beta/models/gemini-2.5-flash:generateContent"
    )
    parts = captured["payload"]["contents"][0]["parts"]
    assert parts[0]["text"].startswith("You are a careful OCR")
    assert parts[1]["inlineData"]["mimeType"] == "image/png"
    assert parts[1]["inlineData"]["data"]
    generation = captured["payload"]["generationConfig"]
    assert generation["temperature"] == 0
    assert generation["responseMimeType"] == "application/json"
    assert generation["responseJsonSchema"]["type"] == "object"
    assert "default" not in json.dumps(generation["responseJsonSchema"])


def test_onexai_text_contract_has_no_image_and_keeps_persian() -> None:
    captured: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["payload"] = json.loads(request.content)
        return onexai_response()

    provider = OneXAIProvider(
        api_key="test-key",
        model="gemini-2.5-flash",
        client=httpx.Client(transport=httpx.MockTransport(handler)),
    )
    result = provider.extract_text("مبلغ: ۶۰۰٬۰۰۰ تومان\nشماره پیگیری: ۱۲۳۴۵")

    assert result.candidate.amount_unit.value == "toman"
    text = captured["payload"]["contents"][0]["parts"][0]["text"]
    assert "untrusted Telegram message content" in text
    assert "مبلغ: ۶۰۰٬۰۰۰ تومان" in text
    assert "inlineData" not in text


@pytest.mark.parametrize(
    ("status", "error_type"),
    [
        (401, ProviderAuthenticationError),
        (403, ProviderAuthorizationError),
        (428, ProviderAuthorizationError),
        (402, ProviderPaymentRequiredError),
        (404, ProviderModelUnavailableError),
        (429, ProviderRateLimitError),
        (504, ProviderTimeoutError),
        (503, ProviderUnavailableError),
    ],
)
def test_onexai_error_mapping(status: int, error_type: type[Exception]) -> None:
    assert isinstance(map_onexai_status(status), error_type)


def test_onexai_rate_limit_preserves_hint_and_makes_one_default_call() -> None:
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(
            429,
            headers={"Retry-After": "23"},
            json={"error": {"message": "must never be exposed"}},
        )

    provider = OneXAIProvider(
        api_key="secret-not-for-errors",
        model="gemini-2.5-flash",
        client=httpx.Client(transport=httpx.MockTransport(handler)),
    )
    with pytest.raises(ProviderRateLimitError) as caught:
        provider.extract(prepared_image())
    assert calls == 1
    assert caught.value.retry_after_seconds == 23
    assert "secret" not in str(caught.value)
    assert "must never" not in str(caught.value)


def test_onexai_malformed_refusal_timeout_and_transport_errors() -> None:
    malformed = httpx.Client(
        transport=httpx.MockTransport(lambda request: httpx.Response(200, text="not-json"))
    )
    with pytest.raises(MalformedProviderResponseError):
        OneXAIProvider(api_key="x", model="m", client=malformed).extract(prepared_image())

    refusal = httpx.Client(
        transport=httpx.MockTransport(
            lambda request: onexai_response(finish_reason="SAFETY")
        )
    )
    with pytest.raises(ProviderRefusalError):
        OneXAIProvider(api_key="x", model="m", client=refusal).extract(prepared_image())

    def timeout(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("private upstream detail", request=request)

    with pytest.raises(ProviderTimeoutError):
        OneXAIProvider(
            api_key="x",
            model="m",
            client=httpx.Client(transport=httpx.MockTransport(timeout)),
        ).extract(prepared_image())

    def transport(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("private address", request=request)

    with pytest.raises(ProviderUnavailableError):
        OneXAIProvider(
            api_key="x",
            model="m",
            client=httpx.Client(transport=httpx.MockTransport(transport)),
        ).extract(prepared_image())


def test_onexai_config_alias_precedence_masking_and_factory(monkeypatch, tmp_path) -> None:
    base = {
        "database_url": f"sqlite:///{(tmp_path / 'db.sqlite').as_posix()}",
        "data_root": tmp_path / "data",
        "allowed_telegram_usernames": "operator_test",
        "extraction_provider": "1xai",
        "onexai_model": "gemini-2.5-flash",
    }
    monkeypatch.setenv("ONEXAI_API_KEY", "canonical-key")
    monkeypatch.setenv("1xai_API_KEY", "legacy-key")
    settings = AppSettings(**base, _env_file=None)
    assert settings.onexai_api_key is not None
    assert settings.onexai_api_key.get_secret_value() == "canonical-key"
    assert "canonical-key" not in repr(settings)

    provider = build_provider(
        settings,
        onexai_client=httpx.Client(
            transport=httpx.MockTransport(lambda request: onexai_response())
        ),
    )
    assert isinstance(provider, OneXAIProvider)
    assert provider.model == "gemini-2.5-flash"


def test_onexai_legacy_key_and_selected_provider_validation(monkeypatch, tmp_path) -> None:
    base = {
        "database_url": f"sqlite:///{(tmp_path / 'db.sqlite').as_posix()}",
        "data_root": tmp_path / "data",
        "allowed_telegram_usernames": "operator_test",
        "extraction_provider": "1xai",
        "onexai_model": "gemini-2.5-flash",
    }
    monkeypatch.delenv("ONEXAI_API_KEY", raising=False)
    monkeypatch.setenv("1xai_API_KEY", "legacy-key")
    settings = AppSettings(**base, _env_file=None)
    assert settings.onexai_api_key is not None
    assert settings.onexai_api_key.get_secret_value() == "legacy-key"

    monkeypatch.delenv("1xai_API_KEY")
    with pytest.raises(ExtractionConfigurationError, match="ONEXAI_API_KEY"):
        build_provider(AppSettings(**base, _env_file=None))
    with pytest.raises(ExtractionConfigurationError, match="model"):
        build_provider(
            AppSettings(
                **(base | {"onexai_model": None}),
                onexai_api_key="key",
                _env_file=None,
            )
        )


def test_onexai_rejects_unsafe_base_url_and_matches_provider_schema(tmp_path) -> None:
    base = {
        "database_url": f"sqlite:///{(tmp_path / 'db.sqlite').as_posix()}",
        "data_root": tmp_path / "data",
        "allowed_telegram_usernames": "operator_test",
    }
    with pytest.raises(ValueError, match="HTTPS URL"):
        AppSettings(**base, onexai_base_url="http://example.test?key=secret", _env_file=None)

    payload = candidate_payload()
    result = OneXAIProvider(
        api_key="x",
        model="m",
        client=httpx.Client(
            transport=httpx.MockTransport(lambda request: onexai_response(payload))
        ),
    ).extract(prepared_image())
    assert result.candidate == ProviderReceipt.model_validate(payload)
    assert result.raw_response == payload
