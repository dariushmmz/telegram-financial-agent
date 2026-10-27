from __future__ import annotations

import base64
import json
import time
from collections.abc import Callable
from typing import Any

import httpx
from pydantic import ValidationError

from receipt_bot.extraction.errors import (
    MalformedProviderResponseError,
    ProviderAuthenticationError,
    ProviderError,
    ProviderModelUnavailableError,
    ProviderRateLimitError,
    ProviderRefusalError,
    ProviderTimeoutError,
    ProviderUnavailableError,
)
from receipt_bot.extraction.image import PreparedImage
from receipt_bot.extraction.prompt import RECEIPT_PROMPT, TEXT_RECEIPT_PROMPT
from receipt_bot.extraction.providers.common import RetryPolicy, parse_retry_after
from receipt_bot.extraction.schema import ProviderReceipt, ProviderResult


DEFAULT_ENDPOINT = "https://openrouter.ai/api/v1/chat/completions"


def map_openrouter_status(
    status_code: int, *, retry_after_seconds: float | None = None
) -> ProviderError:
    if status_code in (401, 403):
        return ProviderAuthenticationError("OpenRouter authentication failed", status_code=status_code)
    if status_code == 404:
        return ProviderModelUnavailableError("OpenRouter model or endpoint is unavailable", status_code=status_code)
    if status_code == 429:
        return ProviderRateLimitError(
            "OpenRouter rate limit reached",
            status_code=status_code,
            retry_after_seconds=retry_after_seconds,
        )
    if status_code in (408, 504):
        return ProviderTimeoutError("OpenRouter request timed out", status_code=status_code)
    if status_code in (500, 502, 503):
        return ProviderUnavailableError("OpenRouter service is temporarily unavailable", status_code=status_code)
    return ProviderError("OpenRouter request failed", status_code=status_code)


class OpenRouterProvider:
    name = "openrouter"

    def __init__(
        self,
        *,
        api_key: str,
        model: str,
        client: httpx.Client | None = None,
        endpoint: str = DEFAULT_ENDPOINT,
        timeout_seconds: float = 45.0,
        retry_policy: RetryPolicy | None = None,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        if not api_key:
            raise ValueError("OpenRouter API key is required")
        if not model:
            raise ValueError("OpenRouter model is required")
        self.api_key = api_key
        self.model = model
        self.endpoint = endpoint
        self.client = client or httpx.Client(timeout=timeout_seconds)
        self.retry_policy = retry_policy or RetryPolicy()
        self.sleep = sleep

    def _payload(self, image: PreparedImage) -> dict[str, Any]:
        encoded = base64.b64encode(image.data).decode("ascii")
        return {
            "model": self.model,
            "temperature": 0,
            "messages": [
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": RECEIPT_PROMPT},
                        {
                            "type": "image_url",
                            "image_url": {"url": f"data:{image.mime_type};base64,{encoded}"},
                        },
                    ],
                }
            ],
            "response_format": {
                "type": "json_schema",
                "json_schema": {
                    "name": "iranian_receipt_extraction",
                    "strict": True,
                    "schema": ProviderReceipt.model_json_schema(),
                },
            },
            "provider": {"require_parameters": True},
        }

    def _text_payload(self, text: str) -> dict[str, Any]:
        return {
            "model": self.model,
            "temperature": 0,
            "messages": [
                {
                    "role": "user",
                    "content": (
                        f"{TEXT_RECEIPT_PROMPT}\n<receipt_text>\n{text}\n</receipt_text>"
                    ),
                }
            ],
            "response_format": {
                "type": "json_schema",
                "json_schema": {
                    "name": "iranian_text_receipt_extraction",
                    "strict": True,
                    "schema": ProviderReceipt.model_json_schema(),
                },
            },
            "provider": {"require_parameters": True},
        }

    def _parse_response(self, response: httpx.Response) -> ProviderResult:
        try:
            body = response.json()
            choice = body["choices"][0]
        except (ValueError, KeyError, IndexError, TypeError) as exc:
            raise MalformedProviderResponseError("OpenRouter returned an invalid response envelope") from exc

        choice_error = choice.get("error")
        if choice_error:
            code = choice_error.get("code") if isinstance(choice_error, dict) else None
            if isinstance(code, int):
                raise map_openrouter_status(code)
            raise ProviderError("OpenRouter returned a provider error")

        finish_reason = str(choice.get("finish_reason") or "").casefold()
        if finish_reason == "content_filter":
            raise ProviderRefusalError("OpenRouter refused the receipt extraction request")
        if finish_reason == "error":
            raise ProviderError("OpenRouter provider failed during generation")

        content = choice.get("message", {}).get("content")
        if not isinstance(content, str) or not content.strip():
            raise MalformedProviderResponseError("OpenRouter returned empty structured output")
        try:
            raw = json.loads(content)
            if not isinstance(raw, dict):
                raise ValueError("structured output is not an object")
            candidate = ProviderReceipt.model_validate(raw)
        except (json.JSONDecodeError, ValidationError, ValueError, TypeError) as exc:
            raise MalformedProviderResponseError("OpenRouter returned invalid structured output") from exc

        usage = body.get("usage") if isinstance(body.get("usage"), dict) else {}
        return ProviderResult(
            provider="openrouter",
            model=body.get("model") or self.model,
            candidate=candidate,
            raw_response=raw,
            usage=usage,
            request_id=body.get("id"),
        )

    def extract(self, image: PreparedImage) -> ProviderResult:
        return self._request(self._payload(image))

    def extract_text(self, text: str) -> ProviderResult:
        return self._request(self._text_payload(text))

    def _request(self, payload: dict[str, Any]) -> ProviderResult:
        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
        }
        last_error: ProviderError | None = None
        for attempt in range(1, self.retry_policy.max_attempts + 1):
            try:
                response = self.client.post(self.endpoint, headers=headers, json=payload)
                if response.status_code >= 400:
                    raise map_openrouter_status(
                        response.status_code,
                        retry_after_seconds=parse_retry_after(response.headers.get("Retry-After")),
                    )
                return self._parse_response(response)
            except httpx.TimeoutException as exc:
                mapped: ProviderError = ProviderTimeoutError("OpenRouter request timed out")
                mapped.__cause__ = exc
            except httpx.TransportError as exc:
                mapped = ProviderUnavailableError("OpenRouter transport is unavailable")
                mapped.__cause__ = exc
            except MalformedProviderResponseError as exc:
                mapped = exc
            except ProviderRefusalError:
                raise
            except ProviderError as exc:
                mapped = exc

            last_error = mapped
            if isinstance(mapped, (ProviderAuthenticationError, ProviderModelUnavailableError)):
                raise mapped from None
            if mapped.retryable and attempt < self.retry_policy.max_attempts:
                self.sleep(self.retry_policy.delay(attempt))
                continue
            break

        if last_error is None:
            raise ProviderError("OpenRouter extraction failed")
        raise last_error from None
