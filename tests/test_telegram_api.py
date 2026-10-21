from __future__ import annotations

import asyncio
import json

import httpx
import pytest

from receipt_bot.telegram.api import TelegramAPIClient, TelegramAPIError


def test_async_api_contract_for_polling_membership_messages_and_download() -> None:
    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        method = request.url.path.rsplit("/", 1)[-1]
        if method == "getMe":
            return httpx.Response(200, json={"ok": True, "result": {"id": 99, "username": "bot"}})
        if method == "getUpdates":
            return httpx.Response(200, json={"ok": True, "result": [{"update_id": 7}]})
        if method == "getChatMember":
            return httpx.Response(200, json={"ok": True, "result": {"status": "administrator"}})
        if method == "sendMessage":
            return httpx.Response(200, json={"ok": True, "result": {"message_id": 8}})
        if method == "answerCallbackQuery":
            return httpx.Response(200, json={"ok": True, "result": True})
        if method == "getFile":
            return httpx.Response(200, json={"ok": True, "result": {"file_path": "photos/a.jpg"}})
        if "/file/" in request.url.path:
            return httpx.Response(200, content=b"image-bytes")
        raise AssertionError(str(request.url))

    async def scenario() -> None:
        transport = httpx.MockTransport(handler)
        async with httpx.AsyncClient(transport=transport) as http:
            client = TelegramAPIClient("secret-token", client=http, api_root="https://telegram.test")
            assert (await client.get_me())["username"] == "bot"
            assert await client.get_updates(
                offset=7, timeout=30, allowed_updates=["message", "callback_query"]
            ) == [{"update_id": 7}]
            assert (await client.get_chat_member(-100, 5))["status"] == "administrator"
            await client.send_message(-100, "سلام", reply_markup={"inline_keyboard": []})
            await client.answer_callback_query("cb-1")
            assert await client.download_file("file-1") == b"image-bytes"

    asyncio.run(scenario())
    polling_payload = json.loads(calls[1].content)
    assert polling_payload == {
        "offset": 7,
        "timeout": 30,
        "allowed_updates": ["message", "callback_query"],
    }
    assert json.loads(calls[3].content)["reply_markup"] == {"inline_keyboard": []}


def test_api_errors_do_not_expose_token_or_upstream_body() -> None:
    token = "super-secret-token"

    async def scenario() -> None:
        transport = httpx.MockTransport(
            lambda request: httpx.Response(
                401, json={"ok": False, "description": "sensitive upstream description"}
            )
        )
        async with httpx.AsyncClient(transport=transport) as http:
            client = TelegramAPIClient(token, client=http, api_root="https://telegram.test")
            with pytest.raises(TelegramAPIError) as captured:
                await client.send_message(1, "x")
            rendered = str(captured.value)
            assert token not in rendered
            assert "sensitive upstream" not in rendered
            assert captured.value.retryable is False

    asyncio.run(scenario())
