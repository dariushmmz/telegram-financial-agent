from __future__ import annotations

import base64
import json
import time
from collections.abc import Callable
from typing import Any
from urllib.parse import quote

import httpx
from pydantic import ValidationError

from receipt_bot.extraction.errors import (
    MalformedProviderResponseError,
    ProviderAuthenticationError,
    ProviderAuthorizationError,
    ProviderError,
    ProviderModelUnavailableError,
    ProviderPaymentRequiredError,
    ProviderRateLimitError,
    ProviderRefusalError,
    ProviderTimeoutError,
    ProviderUnavailableError,
)
from receipt_bot.extraction.image import PreparedImage
from receipt_bot.extraction.prompt import RECEIPT_PROMPT, TEXT_RECEIPT_PROMPT
from receipt_bot.extraction.providers.common import RetryPolicy, parse_retry_after
from receipt_bot.extraction.providers.gemini import _gemini_response_schema
from receipt_bot.extraction.schema import ProviderReceipt, ProviderResult


DEFAULT_BASE_URL = "https://1xai.ir/gemini/v1beta"
_REFUSAL_REASONS = {"SAFETY", "BLOCKLIST", "PROHIBITED_CONTENT", "RECITATION"}


def _onexai_response_schema() -> dict[str, Any]:
    """Return raw JSON Schema using only Gemini-documented response keywords."""

    def remove_unsupported(value: Any) -> Any:
        if isinstance(value, dict):
            return {
                key: remove_unsupported(item)
                for key, item in value.items()
                if key != "default"
            }
        if isinstance(value, list):
            return [remove_unsupported(item) for item in value]
        return value

    return remove_unsupported(_gemini_response_schema())


def map_onexai_status(
    status_code: int, *, retry_after_seconds: float | None = None
) -> ProviderError:
    if status_code == 401:
        return ProviderAuthenticationError("1xAi authentication failed", status_code=status_code)
    if status_code in (403, 428):
        return ProviderAuthorizationError(
            "1xAi account is not authorized for this request", status_code=status_code
        )
    if status_code == 402:
        return ProviderPaymentRequiredError(
            "1xAi account balance cannot fund this request", status_code=status_code
        )
    if status_code == 404:
        return ProviderModelUnavailableError(
            "1xAi model or endpoint is unavailable", status_code=status_code
        )
    if status_code == 429:
        return ProviderRateLimitError(
            "1xAi rate limit reached",
            status_code=status_code,
            retry_after_seconds=retry_after_seconds,
        )
    if status_code in (408, 504):
        return ProviderTimeoutError("1xAi request timed out", status_code=status_code)
    if status_code in (500, 502, 503):
        return ProviderUnavailableError(
            "1xAi service is temporarily unavailable", status_code=status_code
        )
    return ProviderError("1xAi request failed", status_code=status_code)


class OneXAIProvider:
    """1xAi adapter using its documented native Gemini passthrough route."""

    name = "1xai"

    def __init__(
        self,
        *,
        api_key: str,
        model: str,
        client: httpx.Client | None = None,
        base_url: str = DEFAULT_BASE_URL,
        timeout_seconds: float = 45.0,
        retry_policy: RetryPolicy | None = None,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        if not api_key:
            raise ValueError("1xAi API key is required")
        if not model:
            raise ValueError("1xAi model is required")
        if timeout_seconds <= 0:
            raise ValueError("1xAi timeout must be positive")
        normalized_base = base_url.strip().rstrip("/")
        if not normalized_base:
            raise ValueError("1xAi base URL is required")
        self.api_key = api_key
        self.model = model.strip()
        self.base_url = normalized_base
        self.endpoint = f"{normalized_base}/models/{quote(self.model, safe='._-')}:generateContent"
        self.client = client or httpx.Client(timeout=httpx.Timeout(timeout_seconds))
        self.retry_policy = retry_policy or RetryPolicy()
        self.sleep = sleep

    @staticmethod
    def _generation_config() -> dict[str, Any]:
        return {
            "temperature": 0,
            "responseMimeType": "application/json",
            # 1xAi documents this route as an exact Gemini passthrough. Raw
            # JSON Schema belongs in responseJsonSchema; responseSchema is the
            # older OpenAPI-shaped field and rejects Pydantic's $defs/$ref.
            "responseJsonSchema": _onexai_response_schema(),
        }

    def _image_payload(self, image: PreparedImage) -> dict[str, Any]:
        encoded = base64.b64encode(image.data).decode("ascii")
        return {
            "contents": [
                {
                    "role": "user",
                    "parts": [
                        {"text": RECEIPT_PROMPT},
                        {
                            "inlineData": {
                                "mimeType": image.mime_type,
                                "data": encoded,
                            }
                        },
                    ],
                }
            ],
            "generationConfig": self._generation_config(),
        }

    def _text_payload(self, text: str) -> dict[str, Any]:
        return {
            "contents": [
                {
                    "role": "user",
                    "parts": [
                        {
                            "text": (
                                f"{TEXT_RECEIPT_PROMPT}\n"
                                f"<receipt_text>\n{text}\n</receipt_text>"
                            )
                        }
                    ],
                }
            ],
            "generationConfig": self._generation_config(),
        }

    def _parse_response(self, response: httpx.Response) -> ProviderResult:
        try:
            body = response.json()
            if not isinstance(body, dict):
                raise TypeError("response body is not an object")
        except (ValueError, TypeError) as exc:
            raise MalformedProviderResponseError(
                "1xAi returned an invalid response envelope"
            ) from exc

        feedback = body.get("promptFeedback")
        if isinstance(feedback, dict):
            block_reason = str(feedback.get("blockReason") or "").upper()
            if block_reason and block_reason not in ("NONE", "BLOCK_REASON_UNSPECIFIED"):
                raise ProviderRefusalError("1xAi refused the receipt extraction request")

        candidates = body.get("candidates")
        if not isinstance(candidates, list) or not candidates or not isinstance(candidates[0], dict):
            raise MalformedProviderResponseError("1xAi returned no extraction candidate")
        candidate_body = candidates[0]
        finish_reason = str(candidate_body.get("finishReason") or "").upper()
        if finish_reason in _REFUSAL_REASONS:
            raise ProviderRefusalError("1xAi refused the receipt extraction request")

        content = candidate_body.get("content")
        parts = content.get("parts") if isinstance(content, dict) else None
        text_parts = [
            part["text"]
            for part in parts or []
            if isinstance(part, dict) and isinstance(part.get("text"), str)
        ]
        structured_text = "".join(text_parts).strip()
        if not structured_text:
            raise MalformedProviderResponseError("1xAi returned empty structured output")
        try:
            raw = json.loads(structured_text)
            if not isinstance(raw, dict):
                raise ValueError("structured output is not an object")
            candidate = ProviderReceipt.model_validate(raw)
        except (json.JSONDecodeError, ValidationError, ValueError, TypeError) as exc:
            raise MalformedProviderResponseError(
                "1xAi returned invalid structured output"
            ) from exc

        usage = body.get("usageMetadata")
        request_id = body.get("responseId") or response.headers.get("x-request-id")
        return ProviderResult(
            provider=self.name,
            model=str(body.get("modelVersion") or self.model),
            candidate=candidate,
            raw_response=raw,
            usage=usage if isinstance(usage, dict) else {},
            request_id=str(request_id) if request_id else None,
        )

    def extract(self, image: PreparedImage) -> ProviderResult:
        return self._request(self._image_payload(image))

    def extract_text(self, text: str) -> ProviderResult:
        return self._request(self._text_payload(text))

    def _request(self, payload: dict[str, Any]) -> ProviderResult:
        headers = {
            "x-goog-api-key": self.api_key,
            "Content-Type": "application/json",
        }
        last_error: ProviderError | None = None
        for attempt in range(1, self.retry_policy.max_attempts + 1):
            try:
                response = self.client.post(self.endpoint, headers=headers, json=payload)
                if response.status_code >= 400:
                    raise map_onexai_status(
                        response.status_code,
                        retry_after_seconds=parse_retry_after(
                            response.headers.get("Retry-After")
                        ),
                    )
                return self._parse_response(response)
            except httpx.TimeoutException as exc:
                mapped: ProviderError = ProviderTimeoutError("1xAi request timed out")
                mapped.__cause__ = exc
            except httpx.TransportError as exc:
                mapped = ProviderUnavailableError("1xAi transport is unavailable")
                mapped.__cause__ = exc
            except MalformedProviderResponseError as exc:
                mapped = exc
            except ProviderRefusalError:
                raise
            except ProviderError as exc:
                mapped = exc

            last_error = mapped
            if isinstance(
                mapped,
                (
                    ProviderAuthenticationError,
                    ProviderAuthorizationError,
                    ProviderPaymentRequiredError,
                    ProviderModelUnavailableError,
                ),
            ):
                raise mapped from None
            if mapped.retryable and attempt < self.retry_policy.max_attempts:
                self.sleep(self.retry_policy.delay(attempt))
                continue
            break

        if last_error is None:
            raise ProviderError("1xAi extraction failed")
        raise last_error from None
