from __future__ import annotations

import json
import time
from collections.abc import Callable, Sequence
from typing import Any

import httpx
from google import genai
from google.genai import errors, types
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


def _gemini_response_schema() -> dict[str, Any]:
    """Return the provider schema without keywords unsupported by Gemini's API."""

    def remove_unsupported(value: Any) -> Any:
        if isinstance(value, dict):
            return {
                key: remove_unsupported(item)
                for key, item in value.items()
                if key != "additionalProperties"
            }
        if isinstance(value, list):
            return [remove_unsupported(item) for item in value]
        return value

    return remove_unsupported(ProviderReceipt.model_json_schema())


def map_gemini_error(error: errors.APIError) -> ProviderError:
    code = getattr(error, "code", None)
    response = getattr(error, "response", None)
    headers = getattr(response, "headers", None)
    retry_after = parse_retry_after(headers.get("Retry-After")) if headers is not None else None
    if code in (401, 403):
        return ProviderAuthenticationError("Gemini authentication failed", status_code=code)
    if code == 404:
        return ProviderModelUnavailableError("Gemini model is unavailable", status_code=code)
    if code == 429:
        return ProviderRateLimitError(
            "Gemini rate limit reached",
            status_code=code,
            retry_after_seconds=retry_after,
        )
    if code in (408, 504):
        return ProviderTimeoutError("Gemini request timed out", status_code=code)
    if code in (500, 502, 503):
        return ProviderUnavailableError("Gemini service is temporarily unavailable", status_code=code)
    return ProviderError("Gemini request failed", status_code=code)


def _enum_text(value: Any) -> str:
    return str(getattr(value, "value", value) or "").upper()


def _refusal_reason(response: Any) -> str | None:
    prompt_feedback = getattr(response, "prompt_feedback", None)
    block_reason = _enum_text(getattr(prompt_feedback, "block_reason", None))
    if block_reason and block_reason not in ("NONE", "BLOCK_REASON_UNSPECIFIED"):
        return block_reason
    for candidate in getattr(response, "candidates", None) or []:
        reason = _enum_text(getattr(candidate, "finish_reason", None))
        if reason in {"SAFETY", "BLOCKLIST", "PROHIBITED_CONTENT", "RECITATION"}:
            return reason
    return None


def _parse_response(response: Any, configured_model: str) -> ProviderResult:
    refusal = _refusal_reason(response)
    if refusal:
        raise ProviderRefusalError("Gemini refused the receipt extraction request")

    parsed = getattr(response, "parsed", None)
    raw: dict[str, Any]
    try:
        if isinstance(parsed, ProviderReceipt):
            candidate = parsed
            raw = candidate.model_dump(mode="json")
        elif isinstance(parsed, dict):
            raw = parsed
            candidate = ProviderReceipt.model_validate(parsed)
        else:
            text = getattr(response, "text", None)
            if not isinstance(text, str) or not text.strip():
                raise ValueError("empty response")
            loaded = json.loads(text)
            if not isinstance(loaded, dict):
                raise ValueError("response is not an object")
            raw = loaded
            candidate = ProviderReceipt.model_validate(loaded)
    except (json.JSONDecodeError, ValidationError, ValueError, TypeError) as exc:
        raise MalformedProviderResponseError("Gemini returned invalid structured output") from exc

    usage_object = getattr(response, "usage_metadata", None)
    usage = (
        usage_object.model_dump(mode="json", exclude_none=True)
        if hasattr(usage_object, "model_dump")
        else {}
    )
    return ProviderResult(
        provider="gemini",
        model=getattr(response, "model_version", None) or configured_model,
        candidate=candidate,
        raw_response=raw,
        usage=usage,
        request_id=getattr(response, "response_id", None),
    )


class GeminiProvider:
    name = "gemini"

    def __init__(
        self,
        *,
        api_key: str,
        models: Sequence[str],
        client: Any | None = None,
        timeout_seconds: float = 45.0,
        retry_policy: RetryPolicy | None = None,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        if not api_key:
            raise ValueError("Gemini API key is required")
        self.models = tuple(model.strip() for model in models if model.strip())
        if not self.models:
            raise ValueError("at least one Gemini model is required")
        if timeout_seconds <= 0:
            raise ValueError("Gemini timeout must be positive")
        self.client = client or genai.Client(
            api_key=api_key,
            http_options=types.HttpOptions(
                timeout=int(timeout_seconds * 1000),
                retry_options=types.HttpRetryOptions(attempts=1),
            ),
        )
        self.retry_policy = retry_policy or RetryPolicy()
        self.sleep = sleep

    def extract(self, image: PreparedImage) -> ProviderResult:
        image_part = types.Part.from_bytes(data=image.data, mime_type=image.mime_type)
        return self._generate([RECEIPT_PROMPT, image_part])

    def extract_text(self, text: str) -> ProviderResult:
        return self._generate([TEXT_RECEIPT_PROMPT, f"<receipt_text>\n{text}\n</receipt_text>"])

    def _generate(self, contents: list[Any]) -> ProviderResult:
        config = types.GenerateContentConfig(
            temperature=0,
            response_mime_type="application/json",
            response_schema=_gemini_response_schema(),
        )
        last_error: ProviderError | None = None
        for model in self.models:
            for attempt in range(1, self.retry_policy.max_attempts + 1):
                try:
                    response = self.client.models.generate_content(
                        model=model,
                        contents=contents,
                        config=config,
                    )
                    return _parse_response(response, model)
                except errors.APIError as exc:
                    mapped = map_gemini_error(exc)
                except (httpx.TimeoutException, TimeoutError) as exc:
                    mapped = ProviderTimeoutError("Gemini request timed out")
                    mapped.__cause__ = exc
                except httpx.TransportError as exc:
                    mapped = ProviderUnavailableError("Gemini transport is unavailable")
                    mapped.__cause__ = exc
                except MalformedProviderResponseError as exc:
                    mapped = exc
                except ProviderRefusalError:
                    raise

                last_error = mapped
                if isinstance(mapped, ProviderAuthenticationError):
                    raise mapped from None
                if isinstance(mapped, ProviderModelUnavailableError):
                    break
                if mapped.retryable and attempt < self.retry_policy.max_attempts:
                    self.sleep(self.retry_policy.delay(attempt))
                    continue
                break

            # Model fallback is only meaningful for a missing/retired model.
            # Quota, transport and timeout failures are provider-wide enough
            # that trying every configured model would multiply rejected calls.
            if last_error is not None and not isinstance(
                last_error, ProviderModelUnavailableError
            ):
                raise last_error from None

        if last_error is None:
            raise ProviderError("Gemini extraction failed")
        raise last_error from None
