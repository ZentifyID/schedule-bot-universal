from __future__ import annotations

import itertools
import logging
import queue
import threading
import time
from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import dataclass, field
from typing import Any

from .telegram_api import TelegramAPI, TelegramRateLimitError

logger = logging.getLogger(__name__)


@dataclass(order=True)
class _QueuedMessage:
    priority: int
    sequence: int
    chat_id: int = field(compare=False)
    text: str = field(compare=False)
    thread_id: int | None = field(compare=False)
    reply_markup: dict[str, Any] | None = field(compare=False)
    result: Future[None] = field(compare=False)
    message_id: int | None = field(default=None, compare=False)
    attempts: int = field(default=0, compare=False)
    ready_at: float = field(default=0.0, compare=False)


class TelegramSendQueue:
    """Schedule bounded parallel sends, preserving FIFO within each chat."""

    def __init__(
        self,
        api: TelegramAPI,
        messages_per_second: float = 20.0,
        per_chat_interval: float = 1.05,
        max_size: int = 1000,
    ) -> None:
        if messages_per_second <= 0:
            raise ValueError("messages_per_second must be positive")
        self.api = api
        self._global_interval = 1.0 / messages_per_second
        self._per_chat_interval = max(per_chat_interval, 0.0)
        self._max_size = max(max_size, 1)
        self._condition = threading.Condition()
        self._pending: list[_QueuedMessage] = []
        self._busy_chats: set[int] = set()
        self._sequence = itertools.count()
        self._closed = False
        self._outstanding = 0
        self._last_global_send = 0.0
        self._last_chat_send: dict[int, float] = {}
        self._blocked_until = 0.0
        self._delivery_workers = 4
        self._executor = ThreadPoolExecutor(
            max_workers=self._delivery_workers, thread_name_prefix="telegram-delivery"
        )
        self._worker = threading.Thread(
            target=self._run,
            name="telegram-send-queue",
            daemon=True,
        )
        self._worker.start()

    def _enqueue(
        self,
        chat_id: int,
        text: str,
        thread_id: int | None,
        reply_markup: dict[str, Any] | None,
        priority: int,
        message_id: int | None = None,
        *,
        timeout: float = 10,
    ) -> Future[None]:
        deadline = time.monotonic() + timeout
        with self._condition:
            while self._outstanding >= self._max_size and not self._closed:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise queue.Full
                self._condition.wait(remaining)
            if self._closed:
                raise RuntimeError("Telegram send queue is closed")
            result: Future[None] = Future()
            self._pending.append(
                _QueuedMessage(
                    priority,
                    next(self._sequence),
                    chat_id,
                    text,
                    thread_id,
                    reply_markup,
                    result,
                    message_id,
                )
            )
            self._outstanding += 1
            self._condition.notify_all()
            return result

    def send_message(
        self,
        chat_id: int,
        text: str,
        thread_id: int | None = None,
        reply_markup: dict[str, Any] | None = None,
        *,
        priority: int = 0,
    ) -> None:
        self._enqueue(chat_id, text, thread_id, reply_markup, priority).result()

    def edit_message(
        self,
        chat_id: int,
        message_id: int,
        text: str,
        reply_markup: dict[str, Any] | None = None,
        *,
        priority: int = 0,
    ) -> None:
        self._enqueue(chat_id, text, None, reply_markup, priority, message_id).result()

    def _next_message(self, now: float) -> tuple[_QueuedMessage | None, float | None]:
        # Only the oldest pending message of each chat can be selected.
        first_by_chat: dict[int, _QueuedMessage] = {}
        for item in self._pending:
            first = first_by_chat.get(item.chat_id)
            if first is None or item.sequence < first.sequence:
                first_by_chat[item.chat_id] = item
        eligible: list[_QueuedMessage] = []
        waits: list[float] = []
        for item in first_by_chat.values():
            if item.chat_id in self._busy_chats:
                continue
            ready_at = max(
                item.ready_at,
                self._blocked_until,
                self._last_global_send + self._global_interval,
                self._last_chat_send.get(item.chat_id, 0.0) + self._per_chat_interval,
            )
            if ready_at <= now:
                eligible.append(item)
            else:
                waits.append(ready_at - now)
        if eligible:
            return min(eligible), 0.0
        return None, min(waits) if waits else None

    def _run(self) -> None:
        while True:
            with self._condition:
                if self._closed and self._outstanding == 0:
                    return
                if len(self._busy_chats) >= self._delivery_workers:
                    self._condition.wait()
                    continue
                item, delay = self._next_message(time.monotonic())
                if item is None:
                    self._condition.wait(delay)
                    continue
                self._pending.remove(item)
                self._busy_chats.add(item.chat_id)
                self._last_global_send = time.monotonic()
                item.attempts += 1
                self._executor.submit(self._deliver, item)

    def _deliver(self, item: _QueuedMessage) -> None:
        error: Exception | None = None
        try:
            if item.message_id is None:
                self.api.send_message(
                    item.chat_id,
                    item.text,
                    item.thread_id,
                    item.reply_markup,
                )
            else:
                self.api.edit_message(
                    item.chat_id,
                    item.message_id,
                    item.text,
                    item.reply_markup,
                )
        except Exception as caught:  # noqa: BLE001
            error = caught
        with self._condition:
            now = time.monotonic()
            self._busy_chats.remove(item.chat_id)
            self._last_chat_send[item.chat_id] = now
            if isinstance(error, TelegramRateLimitError):
                # Telegram does not identify the scope: conservatively pause all sends.
                self._blocked_until = max(self._blocked_until, now + error.retry_after)
                if item.attempts < 3:
                    item.ready_at = self._blocked_until
                    self._pending.append(item)
                    self._condition.notify_all()
                    return
            self._outstanding -= 1
            if len(self._last_chat_send) > 5000:
                cutoff = now - 3600
                self._last_chat_send = {
                    chat_id: timestamp
                    for chat_id, timestamp in self._last_chat_send.items()
                    if timestamp >= cutoff
                }
            self._condition.notify_all()
        if error is None:
            item.result.set_result(None)
        else:
            logger.warning(
                "Telegram send failed chat_id=%s thread_id=%s: %s",
                item.chat_id,
                item.thread_id,
                error,
            )
            item.result.set_exception(error)

    def close(self) -> None:
        with self._condition:
            self._closed = True
            self._condition.notify_all()
        self._worker.join()
        self._executor.shutdown(wait=True)
