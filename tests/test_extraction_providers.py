from __future__ import annotations

import io
import json
from types import SimpleNamespace

import httpx
import pytest
from google.genai import errors
from PIL import Image

from receipt_bot.config import AppSettings
from receipt_bot.extraction.errors import (
    ExtractionConfigurationError,
    MalformedProviderResponseError,
    ProviderAuthenticationError,
    ProviderModelUnavailableError,
    ProviderRateLimitError,
    ProviderRefusalError,
    ProviderTimeoutError,
    ProviderUnavailableError,
)
from receipt_bot.extraction.image import prepare_image
from receipt_bot.extraction.providers.common import RetryPolicy, parse_retry_after
from receipt_bot.extraction.providers.factory import build_provider
from receipt_bot.extraction.providers.gemini import (
    GeminiProvider,
    _gemini_response_schema,
    map_gemini_error,
)
from receipt_bot.extraction.providers.openrouter import (
    OpenRouterProvider,
    map_openrouter_status,
)
from receipt_bot.extraction.schema import ProviderReceipt


def prepared_image():
    image = Image.new("RGB", (3, 2), (50, 100, 150))
    image.putpixel((0, 0), (255, 255, 255))
    output = io.BytesIO()
    image.save(output, format="PNG")
    return prepare_image(output.getvalue())


def candidate_payload() -> dict:
    return ProviderReceipt(
        source_name="الناز",
        amount_value="۶۰۰٬۰۰۰",
        amount_unit="toman",
        transaction_datetime="۱۴۰۵/۰۷/۱۸ ۱۲:۱۳:۱۹",
        tracking_code="۱۲۳۴۵",
    ).model_dump(mode="json")


class FakeGeminiModels:
    def __init__(self, events):
        self.events = list(events)
        self.calls = []

    def generate_content(self, **kwargs):
        self.calls.append(kwargs)
        event = self.events.pop(0)
        if isinstance(event, BaseException):
            raise event
        return event


class FakeGeminiClient:
    def __init__(self, events):
        self.models = FakeGeminiModels(events)


def gemini_response(payload=None, **overrides):
    values = {
        "parsed": ProviderReceipt.model_validate(payload or candidate_payload()),
        "model_version": "gemini-test-version",
        "response_id": "gemini-request-1",
        "usage_metadata": None,
        "prompt_feedback": None,
        "candidates": [],
    }
    values.update(overrides)
    return SimpleNamespace(**values)


def test_gemini_adapter_contract_uses_structured_image_request() -> None:
    client = FakeGeminiClient([gemini_response()])
    provider = GeminiProvider(api_key="test-key", models=["gemini-test"], client=client)
    result = provider.extract(prepared_image())
    assert result.provider == "gemini"
    assert result.model == "gemini-test-version"
    assert result.candidate.source_name == "الناز"
    assert result.request_id == "gemini-request-1"
    call = client.models.calls[0]
    assert call["model"] == "gemini-test"
    assert isinstance(call["contents"][0], str)
    assert call["config"].temperature == 0
    assert call["config"].response_mime_type == "application/json"


def test_gemini_client_has_explicit_timeout_and_disables_sdk_retries(monkeypatch) -> None:
    captured = {}

    def client_factory(**kwargs):
        captured.update(kwargs)
        return FakeGeminiClient([gemini_response()])

    monkeypatch.setattr("receipt_bot.extraction.providers.gemini.genai.Client", client_factory)
    provider = GeminiProvider(
        api_key="test-key", models=["gemini-test"], timeout_seconds=12.5
    )
    provider.extract(prepared_image())
    options = captured["http_options"]
    assert options.timeout == 12_500
    assert options.retry_options.attempts == 1


def test_gemini_adapter_supports_untrusted_text_receipts() -> None:
    client = FakeGeminiClient([gemini_response()])
    provider = GeminiProvider(api_key="test-key", models=["gemini-test"], client=client)
    result = provider.extract_text("مبلغ: ۶۰۰٬۰۰۰ تومان\nشماره پیگیری: ۱۲۳۴۵")
    assert result.provider == "gemini"
    contents = client.models.calls[0]["contents"]
    assert "untrusted Telegram message content" in contents[0]
    assert contents[1].startswith("<receipt_text>")
    assert "image" not in contents[1]


def test_gemini_schema_omits_unsupported_additional_properties_recursively() -> None:
    schema = _gemini_response_schema()

    def assert_supported(value) -> None:
        if isinstance(value, dict):
            assert "additionalProperties" not in value
            for item in value.values():
                assert_supported(item)
        elif isinstance(value, list):
            for item in value:
                assert_supported(item)

    assert schema["type"] == "object"
    assert "properties" in schema
    assert_supported(schema)


def test_gemini_model_fallback_on_404() -> None:
    client = FakeGeminiClient(
        [
            errors.ClientError(404, {"error": {"message": "not found"}}),
            gemini_response(),
        ]
    )
    provider = GeminiProvider(
        api_key="test-key",
        models=["retired", "working"],
        client=client,
        retry_policy=RetryPolicy(max_attempts=1, base_delay_seconds=0),
    )
    assert provider.extract(prepared_image()).candidate.amount_unit.value == "toman"
    assert [call["model"] for call in client.models.calls] == ["retired", "working"]


def test_gemini_rate_limit_retries_without_leaking_response() -> None:
    client = FakeGeminiClient(
        [errors.ClientError(429, {"error": {"message": "sensitive upstream body"}}), gemini_response()]
    )
    delays = []
    provider = GeminiProvider(
        api_key="test-key",
        models=["model"],
        client=client,
        retry_policy=RetryPolicy(max_attempts=2, base_delay_seconds=0.25),
        sleep=delays.append,
    )
    provider.extract(prepared_image())
    assert delays == [0.25]


def test_gemini_rate_limit_does_not_fan_out_across_model_fallbacks() -> None:
    client = FakeGeminiClient(
        [errors.ClientError(429, {"error": {"message": "limited"}})]
    )
    provider = GeminiProvider(
        api_key="test-key",
        models=["primary", "fallback"],
        client=client,
    )
    with pytest.raises(ProviderRateLimitError):
        provider.extract(prepared_image())
    assert [call["model"] for call in client.models.calls] == ["primary"]


def test_gemini_malformed_refusal_timeout_and_auth_errors() -> None:
    malformed = SimpleNamespace(
        parsed=None,
        text="not json",
        prompt_feedback=None,
        candidates=[],
        usage_metadata=None,
        model_version=None,
        response_id=None,
    )
    with pytest.raises(MalformedProviderResponseError):
        GeminiProvider(
            api_key="x",
            models=["m"],
            client=FakeGeminiClient([malformed]),
            retry_policy=RetryPolicy(max_attempts=1),
        ).extract(prepared_image())

    refusal = gemini_response(
        parsed=None,
        prompt_feedback=SimpleNamespace(block_reason="SAFETY"),
    )
    with pytest.raises(ProviderRefusalError):
        GeminiProvider(api_key="x", models=["m"], client=FakeGeminiClient([refusal])).extract(
            prepared_image()
        )

    with pytest.raises(ProviderTimeoutError):
        GeminiProvider(
            api_key="x",
            models=["m"],
            client=FakeGeminiClient([TimeoutError()]),
            retry_policy=RetryPolicy(max_attempts=1),
        ).extract(prepared_image())

    with pytest.raises(ProviderAuthenticationError):
        GeminiProvider(
            api_key="x",
            models=["m"],
            client=FakeGeminiClient([errors.ClientError(401, {"error": {"message": "bad key"}})]),
        ).extract(prepared_image())


@pytest.mark.parametrize(
    ("code", "error_type"),
    [
        (401, ProviderAuthenticationError),
        (404, ProviderModelUnavailableError),
        (429, ProviderRateLimitError),
        (504, ProviderTimeoutError),
        (503, ProviderUnavailableError),
    ],
)
def test_provider_error_mapping(code, error_type) -> None:
    assert isinstance(map_gemini_error(errors.APIError(code, {"error": {"message": "x"}})), error_type)
    assert isinstance(map_openrouter_status(code), error_type)


def openrouter_response(payload=None, *, finish_reason="stop", status=200):
    return httpx.Response(
        status,
        json={
            "id": "or-request-1",
            "model": "google/test-vision",
            "choices": [
                {
                    "finish_reason": finish_reason,
                    "message": {"role": "assistant", "content": json.dumps(payload or candidate_payload())},
                }
            ],
            "usage": {"prompt_tokens": 10, "completion_tokens": 20, "total_tokens": 30},
        },
    )


def test_openrouter_adapter_contract_and_private_base64_image() -> None:
    captured = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["authorization"] = request.headers["Authorization"]
        captured["payload"] = json.loads(request.content)
        return openrouter_response()

    provider = OpenRouterProvider(
        api_key="or-test-key",
        model="google/test-vision",
        client=httpx.Client(transport=httpx.MockTransport(handler)),
    )
    result = provider.extract(prepared_image())
    assert result.provider == "openrouter"
    assert result.candidate.amount_value == "۶۰۰٬۰۰۰"
    assert result.usage["total_tokens"] == 30
    assert captured["authorization"] == "Bearer or-test-key"
    payload = captured["payload"]
    assert payload["messages"][0]["content"][0]["type"] == "text"
    assert payload["messages"][0]["content"][1]["image_url"]["url"].startswith("data:image/png;base64,")
    assert payload["response_format"]["type"] == "json_schema"
    assert payload["response_format"]["json_schema"]["strict"] is True
    assert payload["provider"]["require_parameters"] is True


def test_openrouter_adapter_supports_text_without_image_payload() -> None:
    captured = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["payload"] = json.loads(request.content)
        return openrouter_response()

    provider = OpenRouterProvider(
        api_key="or-test-key",
        model="google/test",
        client=httpx.Client(transport=httpx.MockTransport(handler)),
    )
    provider.extract_text("مبلغ: ۶۰۰٬۰۰۰ تومان\nشماره پیگیری: ۱۲۳۴۵")
    content = captured["payload"]["messages"][0]["content"]
    assert isinstance(content, str)
    assert "<receipt_text>" in content
    assert "image_url" not in content


def test_openrouter_rate_limit_retries_then_succeeds() -> None:
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(429) if calls == 1 else openrouter_response()

    delays = []
    provider = OpenRouterProvider(
        api_key="x",
        model="m",
        client=httpx.Client(transport=httpx.MockTransport(handler)),
        retry_policy=RetryPolicy(max_attempts=2, base_delay_seconds=0.5),
        sleep=delays.append,
    )
    provider.extract(prepared_image())
    assert calls == 2
    assert delays == [0.5]


def test_openrouter_preserves_retry_after_hint_without_default_nested_retry() -> None:
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(429, headers={"Retry-After": "17"})

    provider = OpenRouterProvider(
        api_key="x",
        model="m",
        client=httpx.Client(transport=httpx.MockTransport(handler)),
    )
    with pytest.raises(ProviderRateLimitError) as caught:
        provider.extract(prepared_image())
    assert calls == 1
    assert caught.value.retry_after_seconds == 17
    assert parse_retry_after("not-a-delay") is None


def test_openrouter_malformed_refusal_and_timeout() -> None:
    malformed_client = httpx.Client(
        transport=httpx.MockTransport(lambda request: httpx.Response(200, text="not-json"))
    )
    with pytest.raises(MalformedProviderResponseError):
        OpenRouterProvider(
            api_key="x",
            model="m",
            client=malformed_client,
            retry_policy=RetryPolicy(max_attempts=1),
        ).extract(prepared_image())

    refusal_client = httpx.Client(
        transport=httpx.MockTransport(
            lambda request: httpx.Response(
                200,
                json={
                    "choices": [
                        {"finish_reason": "content_filter", "message": {"content": None}}
                    ]
                },
            )
        )
    )
    with pytest.raises(ProviderRefusalError):
        OpenRouterProvider(api_key="x", model="m", client=refusal_client).extract(prepared_image())

    def timeout_handler(request: httpx.Request):
        raise httpx.ReadTimeout("timeout", request=request)

    with pytest.raises(ProviderTimeoutError):
        OpenRouterProvider(
            api_key="x",
            model="m",
            client=httpx.Client(transport=httpx.MockTransport(timeout_handler)),
            retry_policy=RetryPolicy(max_attempts=1),
        ).extract(prepared_image())


def test_provider_factory_requires_only_selected_provider_credentials(tmp_path) -> None:
    base = {
        "database_url": f"sqlite:///{(tmp_path / 'db.sqlite').as_posix()}",
        "data_root": tmp_path / "data",
        "allowed_telegram_usernames": "@operator_test",
    }
    with pytest.raises(ExtractionConfigurationError, match="GEMINI_API_KEY"):
        build_provider(AppSettings(**base, _env_file=None))

    settings = AppSettings(
        **base, gemini_api_key="test", gemini_models=" a, b ", _env_file=None
    )
    provider = build_provider(settings, gemini_client=FakeGeminiClient([gemini_response()]))
    assert isinstance(provider, GeminiProvider)
    assert provider.models == ("a", "b")

    with pytest.raises(ExtractionConfigurationError, match="OPENROUTER_API_KEY"):
        build_provider(
            AppSettings(
                **base,
                extraction_provider="openrouter",
                openrouter_model="m",
                _env_file=None,
            )
        )


def test_both_adapters_return_the_same_provider_neutral_candidate() -> None:
    payload = candidate_payload()
    gemini = GeminiProvider(
        api_key="x", models=["m"], client=FakeGeminiClient([gemini_response(payload)])
    ).extract(prepared_image())
    openrouter = OpenRouterProvider(
        api_key="x",
        model="m",
        client=httpx.Client(
            transport=httpx.MockTransport(lambda request: openrouter_response(payload))
        ),
    ).extract(prepared_image())
    assert gemini.candidate == openrouter.candidate
    assert gemini.raw_response == openrouter.raw_response
