"""Telegram Bot API over plain httpx.

Five methods do not justify python-telegram-bot and its own event loop. The
part that needs care is the token: it sits inside every request URL, so no text
leaving this module may carry a URL or an exception that quotes one.
"""

from pathlib import Path
from typing import Any

import httpx

API_URL = "https://api.telegram.org"


def redact(text: str, secret: str | None) -> str:
    return text.replace(secret, "***") if secret else text


class TelegramError(Exception):
    def __init__(self, message: str, status: int | None = None, retry_after: int | None = None) -> None:
        super().__init__(message)
        self.status = status
        """HTTP status, or None when the request never got an answer."""
        self.retry_after = retry_after


class TelegramClient:
    def __init__(self, token: str, client: httpx.Client | None = None) -> None:
        self._token = token
        self._client = client or httpx.Client(timeout=30)

    def close(self) -> None:
        self._client.close()

    def get_updates(self, offset: int | None, timeout: int) -> list[dict]:
        payload: dict[str, Any] = {"timeout": timeout, "allowed_updates": ["message", "callback_query"]}
        if offset is not None:
            payload["offset"] = offset
        # Telegram holds the request open for `timeout` seconds when there is
        # nothing to say; the read timeout has to outlast that.
        return self._call("getUpdates", timeout=timeout + 10, json=payload)

    def send_message(self, chat_id: int, html: str,
                     buttons: list[tuple[str, str]] | None = None) -> None:
        payload: dict[str, Any] = {
            "chat_id": chat_id,
            "text": html,
            "parse_mode": "HTML",
            # Otherwise the first vacancy link unfolds into a large Profesia card.
            "link_preview_options": {"is_disabled": True},
        }
        if buttons:
            payload["reply_markup"] = {"inline_keyboard": [[
                {"text": label, "callback_data": data} for label, data in buttons
            ]]}
        self._call("sendMessage", json=payload)

    def send_document(self, chat_id: int, path: Path, caption: str | None = None) -> None:
        path = Path(path)
        data = {"chat_id": str(chat_id)}
        if caption:
            data |= {"caption": caption, "parse_mode": "HTML"}
        with path.open("rb") as handle:
            self._call("sendDocument", data=data, files={"document": (path.name, handle, "text/markdown")})

    def answer_callback(self, callback_id: str) -> None:
        self._call("answerCallbackQuery", json={"callback_query_id": callback_id})

    def edit_message(self, chat_id: int, message_id: int, html: str) -> None:
        self._call("editMessageText",
                   json={"chat_id": chat_id, "message_id": message_id, "text": html, "parse_mode": "HTML"})

    def _call(self, method: str, timeout: float | None = None, **request: Any) -> Any:
        url = f"{API_URL}/bot{self._token}/{method}"
        try:
            response = self._client.post(
                url, timeout=httpx.USE_CLIENT_DEFAULT if timeout is None else timeout, **request
            )
        except httpx.HTTPError as error:
            # "from None": the chained httpx error would print the URL, token and all.
            raise TelegramError(redact(f"{method}: {type(error).__name__}: {error}", self._token)) from None

        try:
            body = response.json()
        except ValueError:
            body = {}
        if response.status_code != 200 or not body.get("ok"):
            description = body.get("description") or response.reason_phrase
            raise TelegramError(
                redact(f"{method}: {response.status_code} {description}", self._token),
                status=response.status_code,
                retry_after=(body.get("parameters") or {}).get("retry_after"),
            )
        return body.get("result")
