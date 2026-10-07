from __future__ import annotations

import unittest

from app.command_guard import CommandGuard


def message(text: str, user: int = 1, chat: int = 1, thread: int = 0) -> dict:
    return {
        "message": {
            "text": text,
            "from": {"id": user},
            "chat": {"id": chat},
            "message_thread_id": thread,
        }
    }


class CommandGuardTests(unittest.TestCase):
    def test_calendar_format_can_switch_without_duplicate_suppression(self) -> None:
        guard = CommandGuard()
        key = guard.admit(message("/calendar_format short"), 0)
        self.assertIsNotNone(key)
        guard.finish(key)
        key = guard.admit(message("/calendar_format full"), 1)
        self.assertIsNotNone(key)
        guard.finish(key)
        self.assertIsNone(guard.admit(message("/calendar_format full"), 1.5))

    def test_burst_refills_and_users_are_independent(self) -> None:
        guard = CommandGuard()
        for command in ("/help", "/start", "/setup"):
            key = guard.admit(message(command), 0)
            self.assertIsNotNone(key)
            guard.finish(key)
        self.assertIsNone(guard.admit(message("/today"), 1.9))
        self.assertIsNotNone(guard.admit(message("/today", user=2), 1.9))
        self.assertIsNotNone(guard.admit(message("/tomorrow"), 2))

    def test_duplicates_and_active_week_are_scoped_to_topic(self) -> None:
        guard = CommandGuard()
        key = guard.admit(message("/week"), 0)
        for now in range(1, 100):
            self.assertIsNone(guard.admit(message("/week", user=2), now))
        self.assertIsNotNone(guard.admit(message("/week", user=2, thread=9), 100))
        self.assertIsNotNone(guard.admit(message("/week", user=3, chat=2), 100))
        guard.finish(key)
        key = guard.admit(message("/week"), 100)
        self.assertIsNotNone(key)
        guard.finish(key)
        self.assertIsNone(guard.admit(message("/WEEK@ExampleBot"), 102))
        self.assertIsNotNone(guard.admit(message("/week"), 103))

    def test_buttons_allow_setup_flow_and_limit_repeated_clicks(self) -> None:
        guard = CommandGuard()
        key = guard.admit(message("/setup"), 0)
        guard.finish(key)
        for index, data in enumerate(
            (
                "course:1",
                "group:11 ис",
                "setup:teacher",
                "teachers:1",
                "teacher:1234567890abcdef",
            )
        ):
            update = {
                "callback_query": {
                    "id": data,
                    "from": {"id": 1},
                    "data": data,
                    "message": {"chat": {"id": 1}},
                }
            }
            key = guard.admit(update, index * 2)
            self.assertIsNotNone(key)
            guard.finish(key)
        for _ in range(100):
            self.assertIsNone(guard.admit(update, 8))

    def test_unknown_commands_are_ignored_and_old_entries_expire(self) -> None:
        guard = CommandGuard()
        for text in (
            "hello",
            "/refresh",
            "/refresh force",
            "/teacher Иванова",
            "/unknown",
            "/status",
            "/status@ExampleBot",
        ):
            self.assertIsNone(guard.admit(message(text), 0))
        key = guard.admit(message("/help"), 0)
        guard.finish(key)
        guard.admit(message("/help", user=2), 61)
        self.assertNotIn(1, guard._users)
        self.assertEqual(len(guard._recent), 1)
