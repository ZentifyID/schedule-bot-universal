from __future__ import annotations

import datetime as dt
import io
import itertools
import json
import queue
import tempfile
import threading
import time
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from app.autopost import AutopostService
from app.replacement_service import ReplacementConflictError, apply_replacements
from app.schedule_service import ScheduleRepository, ScheduleSnapshot
from app.storage import Storage
from app.teacher_schedule import schedule_for_teacher
from app.telegram_api import TelegramAPI, TelegramAPIError, TelegramRateLimitError
from app.telegram_queue import TelegramSendQueue
from tests import test_bot as bot_tests
from tests.test_autopost import FakeReplacements, FakeSchedules, FakeSender
from tests.test_queue import FakeTelegramAPI


class ReplacementConflictTests(unittest.TestCase):
    def test_conflicting_rows_are_preserved_and_base_is_not_modified(self) -> None:
        base = {"group": "11 ис", "pairs": [{"pair": 1, "subject": "База"}]}
        rows = [
            {"pair": "I", "from": "A", "to": "B", "room": "101"},
            {"pair": "1", "from": "A", "to": "C", "room": "102"},
        ]
        with self.assertRaises(ReplacementConflictError) as caught:
            apply_replacements(base, rows)
        self.assertEqual(caught.exception.rows, rows)
        self.assertIn("11 ис", str(caught.exception))
        self.assertEqual(base["pairs"], [{"pair": 1, "subject": "База"}])
        self.assertEqual(len(rows), 2)

    def test_identical_rows_with_roman_and_arabic_pair_are_applied_once(self) -> None:
        row = {"pair": "I", "from": "A", "to": "B(Math)", "room": "101"}
        result = apply_replacements(
            {"pairs": []},
            [row, {**row, "pair": "1"}],
        )
        self.assertEqual(len(result["pairs"]), 1)
        self.assertEqual(result["pairs"][0]["teacher"], "B")


class TelegramErrorTests(unittest.TestCase):
    @staticmethod
    def response(payload: dict) -> io.BytesIO:
        return io.BytesIO(json.dumps(payload).encode())

    def test_direct_request_waits_full_retry_after(self) -> None:
        with (
            patch(
                "app.telegram_api.urllib.request.urlopen",
                side_effect=[
                    self.response(
                        {
                            "ok": False,
                            "error_code": 429,
                            "parameters": {"retry_after": 30},
                        }
                    ),
                    self.response({"ok": True, "result": []}),
                ],
            ) as request,
            patch("app.telegram_api.time.sleep") as sleep,
        ):
            self.assertEqual(TelegramAPI("test").updates(None), [])
        sleep.assert_called_once_with(30)
        self.assertEqual(request.call_count, 2)

    def test_send_hands_rate_limit_to_scheduler_without_sleeping(self) -> None:
        with (
            patch(
                "app.telegram_api.urllib.request.urlopen",
                return_value=self.response(
                    {"ok": False, "error_code": 429, "parameters": {"retry_after": 30}}
                ),
            ) as request,
            patch("app.telegram_api.time.sleep") as sleep,
            self.assertRaises(TelegramRateLimitError) as caught,
        ):
            TelegramAPI("test").send_message(1, "test")
        self.assertEqual(caught.exception.retry_after, 30)
        self.assertEqual(request.call_count, 1)
        sleep.assert_not_called()

    def test_delivery_errors_are_distinguished_from_bad_content_and_server_errors(
        self,
    ) -> None:
        self.assertTrue(
            TelegramAPIError("sendMessage", 403, "bot was blocked").delivery_unavailable
        )
        self.assertTrue(
            TelegramAPIError(
                "sendMessage", 400, "Bad Request: chat not found"
            ).delivery_unavailable
        )
        self.assertFalse(
            TelegramAPIError(
                "sendMessage", 400, "can't parse entities"
            ).delivery_unavailable
        )
        self.assertFalse(
            TelegramAPIError("sendMessage", 500, "server error").delivery_unavailable
        )


class SnapshotTests(unittest.TestCase):
    def test_teacher_calculation_uses_old_snapshot_when_repository_changes(
        self,
    ) -> None:
        groups = {
            group: {
                "course": 1,
                "days": {
                    "вторник": {
                        "числитель": [
                            {"pair": 1, "teacher": "Иванова И.И.", "subject": "old"}
                        ]
                    }
                },
            }
            for group in ("11 ис", "12 ис")
        }
        with tempfile.TemporaryDirectory() as directory:
            storage = Storage(Path(directory))
            storage.save_cache({"groups": groups})
            repository = ScheduleRepository(storage, "https://example.invalid")
            original = ScheduleSnapshot.schedule_for

            def calculate(snapshot, group, date, start):
                repository._cache = {"groups": {}}
                return original(snapshot, group, date, start)

            with patch.object(ScheduleSnapshot, "schedule_for", calculate):
                result = schedule_for_teacher(
                    repository,
                    "Иванова И.И.",
                    dt.date(2026, 9, 1),
                    dt.date(2026, 8, 31),
                )
        self.assertEqual([item["subject"] for item in result["pairs"]], ["old", "old"])

    def test_week_output_uses_one_snapshot_for_all_days_and_week_types(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            bot = bot_tests.BotPermissionTests().make_bot(directory)
            try:
                bot.storage.set_binding(42, None, "11 ис")
                bot.handlers.validate_semester = lambda: None
                bot.handlers.sender = bot_tests.FakeSender()
                days = (
                    "понедельник",
                    "вторник",
                    "среда",
                    "четверг",
                    "пятница",
                    "суббота",
                )
                bot.schedules._cache = {
                    "groups": {
                        "11 ис": {
                            "course": 1,
                            "days": {
                                day: {
                                    week: [{"pair": 1, "subject": "old"}]
                                    for week in ("числитель", "знаменатель")
                                }
                                for day in days
                            },
                        }
                    }
                }
                original = ScheduleSnapshot.schedule_for

                def calculate(snapshot, group, date, start):
                    bot.schedules._cache = {"groups": {}}
                    return original(snapshot, group, date, start)

                with patch.object(ScheduleSnapshot, "schedule_for", calculate):
                    bot.handlers.handle_message(
                        {
                            "chat": {"id": 42, "type": "private"},
                            "from": {"id": 42},
                            "text": "/week",
                        }
                    )
                self.assertEqual(len(bot.handlers.sender.messages), 6)
                self.assertTrue(
                    all("old" in text for text in bot.handlers.sender.messages)
                )
            finally:
                bot.close()

    def test_snapshot_mutation_cannot_change_live_cache(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            storage = Storage(Path(directory))
            storage.save_cache({"groups": {"11 ис": {"course": 1}}})
            repository = ScheduleRepository(storage, "https://example.invalid")
            snapshot = repository.snapshot()
            snapshot.cache["groups"].clear()
            self.assertEqual(repository.groups(), ["11 ис"])


class AutopostRecoveryTests(unittest.TestCase):
    def test_only_permanent_delivery_failures_disable_binding(self) -> None:
        for error, disabled in (
            (TimeoutError("timeout"), False),
            (TelegramRateLimitError("sendMessage", 30), False),
            (TelegramAPIError("sendMessage", 500, "server error"), False),
            (TelegramAPIError("sendMessage", 400, "can't parse entities"), False),
            (ReplacementConflictError("11 ис", 1, []), False),
            (TelegramAPIError("sendMessage", 403, "bot was blocked"), True),
        ):
            with self.subTest(error=error), tempfile.TemporaryDirectory() as directory:
                storage = Storage(Path(directory))
                storage.set_binding(1, 0, "11 ис")
                storage.set_autopost(1, 0, True)

                class Sender:
                    def __init__(self, failure: Exception):
                        self.failure = failure

                    def send_message(self, *args, **kwargs):
                        raise self.failure

                service = AutopostService(
                    SimpleNamespace(numerator_week_start=dt.date(2026, 8, 31)),
                    storage,
                    FakeSchedules(),
                    FakeReplacements(),
                    Sender(error),
                    lambda: None,
                )
                with self.assertLogs("app.autopost", level="ERROR"):
                    for _ in range(3):
                        service.run(dt.datetime(2026, 9, 1, 12, tzinfo=dt.timezone.utc))
                binding = storage.get_binding(1, 0)
                self.assertEqual(bool(binding["autopost"]), not disabled)
                self.assertEqual(binding["autopost_failures"], 3 if disabled else 0)
                self.assertIsNone(
                    storage.autopost_fingerprint(1, 0, "11 ис", "2026-09-02")
                )
                if not disabled:
                    sender = FakeSender()
                    service.sender = sender
                    service.run(dt.datetime(2026, 9, 1, 12, tzinfo=dt.timezone.utc))
                    self.assertEqual(len(sender.messages), 1)
                    self.assertIsNotNone(
                        storage.autopost_fingerprint(1, 0, "11 ис", "2026-09-02")
                    )


class ConcurrentQueueTests(unittest.TestCase):
    def test_slow_chat_does_not_block_another_and_preserves_own_fifo(self) -> None:
        entered = threading.Event()
        release = threading.Event()

        class API(FakeTelegramAPI):
            def send_message(self, chat_id, text, *args):
                if text == "first":
                    entered.set()
                    if not release.wait(3):
                        raise TimeoutError("test release missing")
                super().send_message(chat_id, text, *args)

        api = API()
        sender = TelegramSendQueue(api, messages_per_second=10000, per_chat_interval=0)
        try:
            with ThreadPoolExecutor(max_workers=3) as workers:
                first = workers.submit(sender.send_message, 1, "first", priority=10)
                try:
                    self.assertTrue(entered.wait(2))
                    second = sender._enqueue(1, "second", None, None, 0)
                    other = workers.submit(sender.send_message, 2, "other")
                    other.result(timeout=2)
                    self.assertEqual(api.messages, [(2, "other")])
                    self.assertFalse(second.done())
                finally:
                    release.set()
                first.result(timeout=2)
                second.result(timeout=2)
        finally:
            release.set()
            sender.close()
        self.assertEqual(
            [text for chat, text in api.messages if chat == 1], ["first", "second"]
        )

    def test_per_chat_cooldown_does_not_block_ready_chat(self) -> None:
        api = FakeTelegramAPI()
        sender = TelegramSendQueue(api, messages_per_second=10000, per_chat_interval=1)
        try:
            sender.send_message(1, "first")
            delayed = sender._enqueue(1, "delayed", None, None, 0)
            ready = sender._enqueue(2, "ready", None, None, 0)
            ready.result(timeout=0.5)
            self.assertFalse(delayed.done())
            delayed.result(timeout=2)
        finally:
            sender.close()
        self.assertEqual(api.messages, [(1, "first"), (2, "ready"), (1, "delayed")])

    def test_rate_limit_retries_after_deadline_and_keeps_fifo(self) -> None:
        limited = threading.Event()
        calls: list[tuple[str, float]] = []

        class API(FakeTelegramAPI):
            def send_message(self, chat_id, text, *args):
                calls.append((text, time.monotonic()))
                if len(calls) == 1:
                    limited.set()
                    raise TelegramRateLimitError("sendMessage", 0.15)
                super().send_message(chat_id, text, *args)

        api = API()
        sender = TelegramSendQueue(api, messages_per_second=10000, per_chat_interval=0)
        try:
            first = sender._enqueue(1, "first", None, None, 10)
            self.assertTrue(limited.wait(2))
            second = sender._enqueue(1, "second", None, None, 0)
            first.result(timeout=2)
            second.result(timeout=2)
        finally:
            sender.close()
        self.assertGreaterEqual(calls[1][1] - calls[0][1], 0.15)
        self.assertEqual(api.messages, [(1, "first"), (1, "second")])

    def test_close_drains_work_and_rejects_new_messages(self) -> None:
        api = FakeTelegramAPI()
        sender = TelegramSendQueue(api, messages_per_second=10000, per_chat_interval=0)
        futures = [sender._enqueue(1, str(index), None, None, 0) for index in range(5)]
        sender.close()
        for future in futures:
            future.result(timeout=1)
        self.assertEqual([text for _, text in api.messages], list(map(str, range(5))))
        with self.assertRaisesRegex(RuntimeError, "closed"):
            sender.send_message(1, "late")
        sender.close()

    def test_pending_and_inflight_messages_share_capacity_limit(self) -> None:
        entered = threading.Event()
        release = threading.Event()

        class API(FakeTelegramAPI):
            def send_message(self, *args):
                entered.set()
                if not release.wait(3):
                    raise TimeoutError("test release missing")
                super().send_message(*args)

        sender = TelegramSendQueue(API(), max_size=1)
        try:
            first = sender._enqueue(1, "first", None, None, 0)
            self.assertTrue(entered.wait(2))
            with self.assertRaises(queue.Full):
                sender._enqueue(2, "overflow", None, None, 0, timeout=0)
            release.set()
            first.result(timeout=2)
        finally:
            release.set()
            sender.close()

    def test_repeated_rate_limit_exhausts_retries_and_close_completes(self) -> None:
        class API(FakeTelegramAPI):
            def __init__(self):
                super().__init__()
                self.calls = 0

            def send_message(self, *args):
                self.calls += 1
                raise TelegramRateLimitError("sendMessage", 0)

        api = API()
        sender = TelegramSendQueue(api, messages_per_second=10000, per_chat_interval=0)
        try:
            future = sender._enqueue(1, "limited", None, None, 0)
            with self.assertRaises(TelegramRateLimitError):
                future.result(timeout=2)
        finally:
            sender.close()
        self.assertEqual(api.calls, 3)

    def test_global_send_interval_applies_across_chats(self) -> None:
        calls: list[float] = []

        class API(FakeTelegramAPI):
            def send_message(self, *args):
                calls.append(time.monotonic())
                super().send_message(*args)

        sender = TelegramSendQueue(API(), messages_per_second=10, per_chat_interval=0)
        try:
            futures = [
                sender._enqueue(chat, "message", None, None, 0) for chat in range(3)
            ]
            for future in futures:
                future.result(timeout=2)
        finally:
            sender.close()
        self.assertTrue(
            all(second - first >= 0.09 for first, second in itertools.pairwise(calls))
        )
