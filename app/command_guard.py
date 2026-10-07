from __future__ import annotations

import threading
from typing import Any

COMMANDS = {
    "/help",
    "/start",
    "/setup",
    "/group",
    "/groups",
    "/today",
    "/tomorrow",
    "/date",
    "/week",
    "/autopost_on",
    "/autopost_off",
    "/calendar",
    "/calendar_off",
}
SCHEDULE_COMMANDS = {"/today", "/tomorrow", "/date", "/week"}


class CommandGuard:
    """Bound incoming work before submission to the shared executor."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._users: dict[int, tuple[float, float]] = {}
        self._recent: dict[tuple, float] = {}
        self._active: set[tuple] = set()
        self._cleaned_at = 0.0

    def admit(self, update: dict[str, Any], now: float) -> tuple | None:
        callback = update.get("callback_query")
        message = (callback or {}).get("message") or update.get("message") or {}
        user_id = ((callback or message).get("from") or {}).get("id")
        chat_id = (message.get("chat") or {}).get("id")
        if not user_id or not chat_id:
            return None
        if callback:
            command = "callback"
            argument = str(callback.get("data", ""))
            if argument not in {
                "courses",
                "refresh",
                "setup:groups",
                "setup:teacher",
            } and not argument.startswith(
                ("course:", "group:", "teacher:", "teachers:")
            ):
                return None
        else:
            parts = str(message.get("text", "")).strip().split(maxsplit=1)
            if not parts:
                return None
            command = parts[0].split("@", 1)[0].casefold()
            if command not in COMMANDS:
                return None
            argument = parts[1].strip() if command == "/date" and len(parts) > 1 else ""
        request = (chat_id, message.get("message_thread_id") or 0, command, argument)
        recent_key = (user_id, *request)
        active_key = request if command in SCHEDULE_COMMANDS else recent_key
        with self._lock:
            if now - self._cleaned_at >= 60:
                self._users = {k: v for k, v in self._users.items() if now - v[1] < 60}
                self._recent = {k: v for k, v in self._recent.items() if now - v < 3}
                self._cleaned_at = now
            tokens, last = self._users.get(user_id, (3.0, now))
            tokens = min(3.0, tokens + (now - last) / 2)
            if tokens < 1 or active_key in self._active:
                return None
            if now - self._recent.get(recent_key, float("-inf")) < 3:
                return None
            # Bound memory even when many different accounts send commands.
            if len(self._users) >= 10000 and user_id not in self._users:
                return None
            if len(self._recent) >= 30000 and recent_key not in self._recent:
                return None
            self._users[user_id] = (tokens - 1, now)
            self._recent[recent_key] = now
            self._active.add(active_key)
            return active_key

    def finish(self, key: tuple) -> None:
        with self._lock:
            self._active.discard(key)
