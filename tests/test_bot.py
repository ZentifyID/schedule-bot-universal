from __future__ import annotations

import datetime as dt
import tempfile
import unittest
from concurrent.futures import Future
from pathlib import Path
from unittest.mock import MagicMock, patch

from app.bot import Bot
from app.config import Config


class FakeTelegram:
    def __init__(self, status: str = "member"):
        self.status = status
        self.member_calls = 0

    def get_chat_member(self, chat_id: int, user_id: int) -> dict[str, str]:
        self.member_calls += 1
        return {"status": self.status}

    def answer_callback(self, callback_id: str, text: str = "") -> None:
        return None


class FakeSender:
    def __init__(self) -> None:
        self.messages: list[str] = []

    def send_message(
        self, _chat_id: int, text: str, *_args: object, **_kwargs: object
    ) -> None:
        self.messages.append(text)


class BotPermissionTests(unittest.TestCase):
    def test_submission_filters_spam_and_releases_failed_requests(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            bot = self.make_bot(directory)
            update = {
                "message": {
                    "text": "/week",
                    "from": {"id": 42},
                    "chat": {"id": 42},
                }
            }
            future = Future()
            try:
                with patch.object(
                    bot.executor, "submit", return_value=future
                ) as submit:
                    with patch("app.bot.time.monotonic", return_value=0):
                        bot._submit_update(update)
                        for _ in range(100):
                            bot._submit_update(update)
                    self.assertEqual(submit.call_count, 1)
                    future.set_exception(RuntimeError("send failed"))
                    with patch("app.bot.time.monotonic", return_value=4):
                        bot._submit_update(update)
                    self.assertEqual(submit.call_count, 2)
                with (
                    patch.object(
                        bot.executor, "submit", side_effect=RuntimeError("closed")
                    ),
                    patch("app.bot.time.monotonic", return_value=8),
                    self.assertRaises(RuntimeError),
                ):
                    bot._submit_update(update)
                self.assertEqual(bot.command_guard._active, set())
            finally:
                bot.close()

    def test_full_queue_drops_new_work_without_blocking(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            bot = self.make_bot(directory)
            try:
                with patch.object(bot, "_task_slots") as slots:
                    slots.acquire.return_value = False
                    with patch.object(bot.executor, "submit") as submit:
                        bot._submit_update(
                            {
                                "message": {
                                    "text": "/today",
                                    "from": {"id": 42},
                                    "chat": {"id": 42},
                                }
                            }
                        )
                        submit.assert_not_called()
                    slots.acquire.assert_called_once_with(blocking=False)
                    self.assertEqual(bot.command_guard._active, set())
            finally:
                bot.close()

    def test_refresh_removed_but_automatic_refresh_and_setup_work(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            bot = self.make_bot(directory)
            try:
                bot.handlers.telegram = MagicMock()
                bot.handlers.sender = MagicMock()
                bot.handlers.schedules.groups = MagicMock(return_value=["11 ис"])
                message = {"chat": {"id": 42, "type": "private"}, "from": {"id": 42}}
                for text in ("/refresh", "/refresh force"):
                    bot.handlers.handle_message({**message, "text": text})
                bot.handlers.handle_callback(
                    {
                        "id": "old",
                        "data": "refresh",
                        "message": message,
                        "from": {"id": 42},
                    }
                )
                bot.handlers.telegram.answer_callback.assert_called_once_with("old")
                bot.handlers.sender.send_message.assert_not_called()
                for data in ("course:1", "group:11 ис"):
                    bot.handlers.handle_callback(
                        {
                            "id": data,
                            "data": data,
                            "message": message,
                            "from": {"id": 42},
                        }
                    )
                self.assertEqual(bot.handlers._binding_group(42, None), "11 ис")
                bot.handlers.handle_message({**message, "text": "/help"})
                help_text = bot.handlers.sender.send_message.call_args.args[1]
                self.assertNotIn("/refresh", help_text)
                self.assertIn("Ч — числитель", help_text)
                self.assertIn("автоматически", help_text)
                bot.schedules.refresh = MagicMock(return_value=(False, "unchanged"))
                bot._validate_semester_config = MagicMock()
                bot._maybe_refresh_schedule(3600)
                bot._refresh_future.result(timeout=5)
                bot.schedules.refresh.assert_called_once_with(
                    expected_semester=bot._expected_semester_key()
                )
                bot._maybe_refresh_schedule(3601)
                self.assertEqual(bot.schedules.refresh.call_count, 1)
            finally:
                bot.close()

    def make_bot(self, directory: str) -> Bot:
        config = Config(
            token="test",
            schedule_url="https://example.invalid/schedule",
            replacements_url="https://example.invalid/replacements",
            data_dir=Path(directory),
            timezone="Europe/Saratov",
            numerator_week_start=dt.date(2026, 2, 2),
            refresh_interval_minutes=60,
            replacement_check_minutes=10,
            autopost_enabled=True,
            worker_count=2,
            expected_semester=None,
        )
        return Bot(config)

    def test_private_owner_can_manage(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            bot = self.make_bot(directory)
            try:
                bot.telegram = FakeTelegram()
                self.assertTrue(
                    bot.handlers.can_manage(
                        {"id": 42, "type": "private"},
                        {"id": 42},
                    )
                )
            finally:
                bot.close()

    def test_group_requires_admin_and_caches_result(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            bot = self.make_bot(directory)
            try:
                fake = FakeTelegram("administrator")
                bot.telegram = fake
                bot.handlers.telegram = fake
                chat = {"id": -1001, "type": "supergroup"}
                user = {"id": 7}
                self.assertTrue(bot.handlers.can_manage(chat, user))
                self.assertTrue(bot.handlers.can_manage(chat, user))
                self.assertEqual(fake.member_calls, 1)
                bot.handlers.admin_cache.clear()
                fake.status = "member"
                self.assertFalse(bot.handlers.can_manage(chat, user))
            finally:
                bot.close()

    def test_expected_semester_is_derived_from_week_start(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            bot = self.make_bot(directory)
            try:
                self.assertEqual(
                    bot._expected_semester_key(),
                    "2025-2026:semester-2",
                )
            finally:
                bot.close()

    def test_invalid_course_callback_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            bot = self.make_bot(directory)
            try:
                telegram = FakeTelegram()
                sender = FakeSender()
                bot.handlers.telegram = telegram
                bot.handlers.sender = sender
                bot.handlers.handle_callback(
                    {
                        "id": "callback",
                        "data": "course:999",
                        "from": {"id": 42},
                        "message": {"chat": {"id": 42, "type": "private"}},
                    }
                )
                self.assertEqual(sender.messages, ["Некорректный номер курса."])
            finally:
                bot.close()

    def test_week_combines_week_types_and_includes_saturday(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            bot = self.make_bot(directory)
            try:
                bot.storage.set_binding(42, None, "11 ис")
                sender = FakeSender()
                bot.handlers.sender = sender
                bot.handlers.validate_semester = lambda: None
                include_saturday = True

                def fake_schedule(
                    group: str, target_date: dt.date, week_start: dt.date
                ) -> dict:
                    week_type = (
                        "числитель"
                        if ((target_date - week_start).days // 7) % 2 == 0
                        else "знаменатель"
                    )
                    pairs = []
                    if target_date.weekday() == 0:
                        pairs = [
                            {
                                "pair": 1,
                                "subject": "Общая пара",
                                "teacher": "",
                                "room": "101",
                            }
                        ]
                    elif target_date.weekday() == 5 and include_saturday:
                        pairs = [
                            {
                                "pair": 1,
                                "subject": (
                                    "Математика"
                                    if week_type == "числитель"
                                    else "Физика"
                                ),
                                "teacher": "",
                                "room": "101",
                            }
                        ]
                    return {
                        "group": group,
                        "date": target_date.isoformat(),
                        "weekday": [
                            "понедельник",
                            "вторник",
                            "среда",
                            "четверг",
                            "пятница",
                            "суббота",
                        ][target_date.weekday()],
                        "week_type": week_type,
                        "pairs": pairs,
                        "replacements_count": 0,
                        "source": "pdf",
                    }

                bot.handlers.schedules.schedule_for = MagicMock(
                    side_effect=fake_schedule
                )
                bot.handlers.schedules.snapshot = MagicMock(
                    return_value=bot.handlers.schedules
                )
                bot.handlers.replacements.apply_for_date = MagicMock()
                message = {
                    "chat": {"id": 42, "type": "private"},
                    "from": {"id": 42},
                    "text": "/week",
                }

                bot.handlers.handle_message(message)

                self.assertEqual(bot.handlers.schedules.schedule_for.call_count, 12)
                bot.handlers.replacements.apply_for_date.assert_not_called()
                self.assertEqual(len(sender.messages), 6)
                self.assertEqual(sender.messages[0].count("Общая пара"), 1)
                self.assertNotIn("Ч:", sender.messages[0])
                self.assertIn("<b>Суббота</b>", sender.messages[-1])
                self.assertIn("Группа: <b>11 ИС</b>", sender.messages[-1])
                self.assertIn("Ч: Математика — 101", sender.messages[-1])
                self.assertIn("З: Физика — 101", sender.messages[-1])
                self.assertNotIn("2026", sender.messages[-1])

                include_saturday = False
                sender.messages.clear()
                bot.handlers.handle_message(message)
                self.assertEqual(len(sender.messages), 5)
            finally:
                bot.close()
