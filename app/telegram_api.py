from __future__ import annotations

import json
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Any

MAX_TELEGRAM_RESPONSE_BYTES = 10 * 1024 * 1024


class TelegramAPIError(RuntimeError):
    def __init__(self, method: str, code: int, description: str):
        self.code = code
        self.delivery_unavailable = code == 403 or (
            code == 400
            and any(
                reason in description.casefold()
                for reason in (
                    "chat not found",
                    "message thread not found",
                    "chat_id is empty",
                )
            )
        )
        super().__init__(f"Telegram API {method}: {description}")


class TelegramRateLimitError(TelegramAPIError):
    def __init__(self, method: str, retry_after: float):
        self.retry_after = max(float(retry_after), 0.0)
        super().__init__(method, 429, f"retry after {self.retry_after} seconds")


def _read_response(response: Any) -> bytes:
    content = response.read(MAX_TELEGRAM_RESPONSE_BYTES + 1)
    if len(content) > MAX_TELEGRAM_RESPONSE_BYTES:
        raise RuntimeError("Telegram API response is too large")
    return content


class TelegramAPI:
    def __init__(self, token: str):
        self.base_url = f"https://api.telegram.org/bot{token}"

    def request(
        self, method: str, *, retry_rate_limit: bool = True, **params: Any
    ) -> Any:
        for attempt in range(2):
            try:
                return self._request_once(method, params)
            except TelegramRateLimitError as error:
                if not retry_rate_limit or attempt == 1:
                    raise
                time.sleep(error.retry_after)
        raise RuntimeError(f"Telegram API {method}: retry limit reached")

    def _request_once(self, method: str, params: dict[str, Any]) -> Any:
        encoded: dict[str, str] = {}
        for key, value in params.items():
            if value is None:
                continue
            encoded[key] = (
                json.dumps(value, ensure_ascii=False)
                if isinstance(value, (dict, list))
                else str(value)
            )
        request = urllib.request.Request(
            f"{self.base_url}/{method}",
            data=urllib.parse.urlencode(encoded).encode("utf-8"),
            headers={"Content-Type": "application/x-www-form-urlencoded"},
            method="POST",
        )
        parsed_url = urllib.parse.urlparse(request.full_url)
        if parsed_url.scheme != "https" or parsed_url.hostname != "api.telegram.org":
            raise RuntimeError("Refusing an unsafe Telegram API URL")
        http_code = 0
        try:
            # The HTTPS scheme and exact Telegram host are validated above.
            with urllib.request.urlopen(  # nosec B310
                request, timeout=70 if method == "getUpdates" else 20
            ) as response:
                payload = json.loads(_read_response(response).decode("utf-8"))
        except urllib.error.HTTPError as error:
            http_code = error.code
            try:
                payload = json.loads(_read_response(error).decode("utf-8"))
            except (json.JSONDecodeError, UnicodeDecodeError, RuntimeError, OSError):
                raise TelegramAPIError(method, http_code, f"HTTP {http_code}") from None
        if payload.get("ok"):
            return payload.get("result")
        code = int(payload.get("error_code", http_code))
        retry_after = float((payload.get("parameters") or {}).get("retry_after", 0))
        if code == 429 or retry_after > 0:
            raise TelegramRateLimitError(method, retry_after or 1)
        raise TelegramAPIError(
            method, code, str(payload.get("description", "unknown error"))
        )

    def updates(self, offset: int | None) -> list[dict[str, Any]]:
        return self.request(
            "getUpdates",
            offset=offset,
            timeout=50,
            allowed_updates=["message", "callback_query"],
        )

    def send_message(
        self,
        chat_id: int,
        text: str,
        thread_id: int | None = None,
        reply_markup: dict[str, Any] | None = None,
    ) -> None:
        self.request(
            "sendMessage",
            retry_rate_limit=False,
            chat_id=chat_id,
            message_thread_id=thread_id,
            text=text,
            parse_mode="HTML",
            disable_web_page_preview="true",
            reply_markup=reply_markup,
        )

    def edit_message(
        self,
        chat_id: int,
        message_id: int,
        text: str,
        reply_markup: dict[str, Any] | None = None,
    ) -> None:
        self.request(
            "editMessageText",
            retry_rate_limit=False,
            chat_id=chat_id,
            message_id=message_id,
            text=text,
            parse_mode="HTML",
            disable_web_page_preview="true",
            reply_markup=reply_markup,
        )

    def answer_callback(self, callback_id: str, text: str = "") -> None:
        self.request("answerCallbackQuery", callback_query_id=callback_id, text=text)
