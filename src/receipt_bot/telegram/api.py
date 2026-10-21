from __future__ import annotations

from typing import Any

import httpx
from pydantic import SecretStr


class TelegramAPIError(RuntimeError):
    def __init__(self, code: str, *, retryable: bool, status_code: int | None = None) -> None:
        super().__init__(f"Telegram API operation failed ({code})")
        self.code = code
        self.retryable = retryable
        self.status_code = status_code


class TelegramAPIClient:
    """Minimal async Bot API client whose errors never include the token or response body."""

    def __init__(
        self,
        token: str | SecretStr,
        *,
        client: httpx.AsyncClient | None = None,
        api_root: str = "https://api.telegram.org",
    ) -> None:
        secret = token.get_secret_value() if isinstance(token, SecretStr) else token
        if not secret:
            raise ValueError("Telegram bot token is required")
        self._api_root = api_root.rstrip("/")
        self._token = secret
        self._owns_client = client is None
        self._client = client or httpx.AsyncClient(timeout=httpx.Timeout(60, connect=15))

    async def __aenter__(self) -> "TelegramAPIClient":
        return self

    async def __aexit__(self, *_args: Any) -> None:
        await self.close()

    async def close(self) -> None:
        if self._owns_client:
            await self._client.aclose()

    async def _call(self, method: str, payload: dict[str, Any]) -> Any:
        url = f"{self._api_root}/bot{self._token}/{method}"
        try:
            response = await self._client.post(url, json=payload)
            status_code = response.status_code
            body = response.json()
        except (httpx.TimeoutException, httpx.TransportError) as exc:
            raise TelegramAPIError("transport", retryable=True) from None
        except (ValueError, TypeError):
            raise TelegramAPIError("malformed_response", retryable=True) from None
        if not isinstance(body, dict):
            raise TelegramAPIError("malformed_response", retryable=True, status_code=status_code)
        if response.is_error or body.get("ok") is not True:
            retryable = status_code == 429 or status_code >= 500
            raise TelegramAPIError(
                "rate_limit" if status_code == 429 else "request_failed",
                retryable=retryable,
                status_code=status_code,
            )
        return body.get("result")

    async def get_updates(
        self, *, offset: int | None, timeout: int, allowed_updates: list[str]
    ) -> list[dict[str, Any]]:
        payload: dict[str, Any] = {"timeout": timeout, "allowed_updates": allowed_updates}
        if offset is not None:
            payload["offset"] = offset
        result = await self._call("getUpdates", payload)
        if not isinstance(result, list) or not all(isinstance(item, dict) for item in result):
            raise TelegramAPIError("malformed_updates", retryable=True)
        return result

    async def get_me(self) -> dict[str, Any]:
        result = await self._call("getMe", {})
        if not isinstance(result, dict) or not isinstance(result.get("id"), int):
            raise TelegramAPIError("malformed_bot_identity", retryable=True)
        return result

    async def send_message(
        self,
        chat_id: int,
        text: str,
        *,
        reply_markup: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        payload: dict[str, Any] = {"chat_id": chat_id, "text": text}
        if reply_markup is not None:
            payload["reply_markup"] = reply_markup
        result = await self._call("sendMessage", payload)
        if not isinstance(result, dict):
            raise TelegramAPIError("malformed_message", retryable=True)
        return result

    async def answer_callback_query(
        self, callback_query_id: str, *, text: str | None = None, show_alert: bool = False
    ) -> None:
        payload: dict[str, Any] = {
            "callback_query_id": callback_query_id,
            "show_alert": show_alert,
        }
        if text:
            payload["text"] = text
        await self._call("answerCallbackQuery", payload)

    async def get_chat_member(self, chat_id: int, user_id: int) -> dict[str, Any]:
        result = await self._call("getChatMember", {"chat_id": chat_id, "user_id": user_id})
        if not isinstance(result, dict) or not isinstance(result.get("status"), str):
            raise TelegramAPIError("malformed_chat_member", retryable=True)
        return result

    async def download_file(self, file_id: str) -> bytes:
        result = await self._call("getFile", {"file_id": file_id})
        if not isinstance(result, dict) or not isinstance(result.get("file_path"), str):
            raise TelegramAPIError("file_unavailable", retryable=True)
        file_path = result["file_path"].lstrip("/")
        url = f"{self._api_root}/file/bot{self._token}/{file_path}"
        try:
            response = await self._client.get(url)
            response.raise_for_status()
            return response.content
        except httpx.HTTPStatusError as exc:
            status_code = exc.response.status_code
            raise TelegramAPIError(
                "file_download_failed",
                retryable=status_code == 429 or status_code >= 500,
                status_code=status_code,
            ) from None
        except (httpx.TimeoutException, httpx.TransportError):
            raise TelegramAPIError("file_transport", retryable=True) from None
