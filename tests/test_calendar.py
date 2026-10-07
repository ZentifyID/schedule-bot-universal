from __future__ import annotations

import copy
import datetime as dt
import json
import os
import tempfile
import unittest
from dataclasses import replace
from http.client import HTTPConnection
from pathlib import Path
from unittest.mock import MagicMock, patch

from app.bot import Bot
from app.calendar_service import CalendarService, calendar_events, render_calendar
from app.config import Config
from app.schedule_service import ScheduleRepository
from app.storage import Storage

ZONE = dt.timezone(dt.timedelta(hours=4))
DAY = dt.date(2026, 10, 7)


def config(directory: str) -> Config:
    return Config(
        token="test",
        schedule_url="https://example.invalid/schedule",
        replacements_url="https://example.invalid/replacements",
        data_dir=Path(directory),
        timezone="Europe/Saratov",
        numerator_week_start=dt.date(2026, 10, 5),
        refresh_interval_minutes=60,
        replacement_check_minutes=10,
        autopost_enabled=False,
        worker_count=2,
        expected_semester=None,
        calendar_public_url="https://schedule.example.org",
        calendar_port=0,
    )


def event_blocks(content: bytes) -> list[str]:
    unfolded = content.decode().replace("\r\n ", "")
    return [
        block.split("END:VEVENT")[0] for block in unfolded.split("BEGIN:VEVENT")[1:]
    ]


class CalendarTests(unittest.TestCase):
    def setUp(self) -> None:
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.config = config(self.directory.name)
        self.storage = Storage(self.config.data_dir)
        self.storage.save_cache(
            {
                "groups": {
                    group: {
                        "course": 1,
                        "days": {
                            day: {"числитель": lessons, "знаменатель": lessons}
                            for day in (
                                "понедельник",
                                "вторник",
                                "среда",
                                "четверг",
                                "пятница",
                                "суббота",
                            )
                        },
                    }
                    for group, lessons in {
                        "11 ис": [
                            {
                                "pair": 1,
                                "subject": "Алгебра",
                                "teacher": "Иванова И.И.",
                                "room": "101",
                            },
                            {
                                "pair": 2,
                                "subject": "Физика",
                                "teacher": "Петров П.П.",
                                "room": "102",
                            },
                        ],
                        "12 ис": [
                            {
                                "pair": 3,
                                "subject": "Практика",
                                "teacher": "Иванова И. И.",
                                "room": "201",
                            },
                        ],
                    }.items()
                }
            }
        )
        self.schedules = ScheduleRepository(self.storage, self.config.schedule_url)
        self.replacements = MagicMock()
        self.replacements.find_for_date.return_value = None
        self.service = CalendarService(
            self.config,
            self.storage,
            self.schedules,
            self.replacements,
            ZONE,
            lambda: None,
        )
        self.storage.set_binding(42, None, "11 ис")
        self.subscription = self.storage.calendar_subscription(42, None)
        self.token = self.subscription["token"]

    def now(self, hour: int = 22, minute: int = 0, day: dt.date = DAY) -> dt.datetime:
        return dt.datetime.combine(day, dt.time(hour, minute), ZONE)

    def feed(self, hour: int = 22, minute: int = 0) -> bytes:
        return self.service.feed(self.token, self.now(hour, minute))

    def test_22_boundary_group_filter_and_utc(self) -> None:
        self.service.run(self.now(21, 59))
        before = event_blocks(self.feed(21, 59))
        self.assertEqual(len(before), 2)
        self.assertFalse(any("20261008" in event for event in before))
        self.service.run(self.now())
        events = event_blocks(self.feed())
        self.assertEqual(len(events), 4)
        self.assertIn(
            "DTSTART:20261008T040000Z\r\n",
            events[0] + events[1] + events[2] + events[3],
        )
        self.assertIn("DTEND:20261008T053000Z", self.feed().decode())
        self.assertNotIn("Практика", self.feed().decode())
        self.assertNotIn("12 ИС", self.feed().decode())

    def test_global_time_is_respected_in_bot_timezone(self) -> None:
        self.service.config = replace(self.config, calendar_publish_time=dt.time(23))
        # 18:30 UTC is 22:30 Saratov; tomorrow must still be withheld.
        self.service.run(self.now(22, 30).astimezone(dt.timezone.utc))
        self.assertEqual(len(event_blocks(self.feed(22, 30))), 2)
        self.service.run(self.now(23))
        self.assertEqual(len(event_blocks(self.feed(23))), 4)

    def test_publisher_does_not_wait_for_poll_interval_at_22(self) -> None:
        moments = [self.now(21, 59), self.now(21, 59), self.now(22)]
        with (
            patch("app.calendar_service.dt.datetime") as clock,
            patch("app.calendar_service.time.monotonic", return_value=100),
            patch.object(self.service, "run") as publish,
            patch.object(self.service._stop, "wait", side_effect=[False, False, True]),
        ):
            clock.now.side_effect = moments
            self.service._publish_loop()
        self.assertEqual(
            [call.args[0] for call in publish.call_args_list], [moments[0], moments[2]]
        )

    def test_new_subscription_is_published_without_waiting_for_interval(self) -> None:
        wait_calls = 0
        moment = self.now()

        def wait(_seconds: int) -> bool:
            nonlocal wait_calls
            wait_calls += 1
            if wait_calls == 1:
                self.storage.set_binding(9, None, "12 ис")
                self.storage.calendar_subscription(9, None)
                return False
            return True

        with (
            patch("app.calendar_service.dt.datetime") as clock,
            patch("app.calendar_service.time.monotonic", return_value=100),
            patch.object(self.service, "run") as publish,
            patch.object(self.service._stop, "wait", side_effect=wait),
        ):
            clock.now.return_value = moment
            self.service._publish_loop()
        self.assertEqual(publish.call_count, 2)

    def test_teacher_only_including_replacements(self) -> None:
        self.storage.set_binding(7, 2, "Иванова И. И.", "teacher")
        token = self.storage.calendar_subscription(7, 2)["token"]
        self.service.run(self.now())
        content = self.service.feed(token, self.now())
        self.assertEqual(len(event_blocks(content)), 4)
        self.assertIn("Алгебра", content.decode())
        self.assertIn("Практика", content.decode())
        self.assertNotIn("Физика", content.decode())
        self.replacements.find_for_date.return_value = {"name": "replacements.docx"}
        self.replacements.replacements_for_item.return_value = {
            "11 ис": [
                {
                    "pair": "I",
                    "from": "Иванова И.И.(Алгебра)",
                    "to": "Петров П.П.(Замена)",
                    "room": "305",
                }
            ],
            "12 ис": [
                {
                    "pair": "II",
                    "from": "нет",
                    "to": "Иванова И.И.(Новая пара)",
                    "room": "202",
                }
            ],
        }
        self.service.run(self.now(23))
        content = self.service.feed(token, self.now(23))
        active = [
            event for event in event_blocks(content) if "STATUS:CONFIRMED" in event
        ]
        self.assertEqual(len(active), 4)
        self.assertTrue(any("Новая пара" in event for event in active))
        self.assertFalse(
            any("Алгебра" in event or "Замена" in event for event in active)
        )
        self.assertIn("STATUS:CANCELLED", content.decode())

    def test_restart_late_cancellation_and_restoration_keep_uid(self) -> None:
        self.service.run(self.now())
        original = self.feed()
        self.service.storage = Storage(self.config.data_dir)
        self.service.run(self.now(22, 20))
        self.assertEqual(self.feed(), original)
        self.replacements.find_for_date.return_value = {"name": "replacements.docx"}
        self.replacements.replacements_for_item.return_value = {
            "11 ис": [
                {"pair": "I", "from": "Иванова И.И.(Алгебра)", "to": "нет", "room": "-"}
            ]
        }
        self.service.run(self.now(23))
        cancelled = event_blocks(self.feed(23))
        original_uids = {
            event.split("UID:")[1].split("\r\n")[0] for event in event_blocks(original)
        }
        new_uids = {event.split("UID:")[1].split("\r\n")[0] for event in cancelled}
        self.assertEqual(original_uids, new_uids)
        self.assertEqual(sum("STATUS:CANCELLED" in event for event in cancelled), 2)
        self.assertTrue(all("SEQUENCE:1" in event for event in cancelled))
        self.replacements.replacements_for_item.return_value = {}
        self.service.run(self.now(23, 30))
        restored = self.feed(23, 30)
        self.assertNotIn(b"STATUS:CANCELLED", restored)
        self.assertIn(b"SEQUENCE:2", restored)

    def test_binding_changes_topics_tokens_and_revocation_are_isolated(self) -> None:
        self.assertEqual(
            self.storage.calendar_subscription(42, None)["token"], self.token
        )
        self.storage.set_binding(42, None, "12 ис")
        other_token = self.storage.calendar_subscription(42, None)["token"]
        self.storage.set_binding(42, 8, "Иванова И.И.", "teacher")
        topic_token = self.storage.calendar_subscription(42, 8)["token"]
        self.service.run(self.now())
        self.assertNotIn("Практика", self.feed().decode())
        other = self.service.feed(other_token, self.now())
        self.assertIn("Практика", other.decode())
        self.assertNotIn("Алгебра", other.decode())
        self.assertNotEqual(other_token, self.token)
        self.storage.revoke_calendars(42, None)
        self.assertIsNone(self.service.feed(self.token, self.now()))
        self.assertIsNone(self.service.feed(other_token, self.now()))
        self.assertIsNotNone(self.service.feed(topic_token, self.now()))
        self.assertNotEqual(
            self.storage.calendar_subscription(42, None)["token"], other_token
        )

    def test_unavailable_source_preserves_previous_snapshot(self) -> None:
        self.service.run(self.now())
        previous = self.feed()
        self.replacements.find_for_date.side_effect = RuntimeError("offline")
        with self.assertLogs("app.calendar_service", "ERROR"):
            self.service.run(self.now(23))
        self.assertEqual(self.feed(23), previous)

    def test_conflicting_replacements_preserve_feed_and_other_group_updates(
        self,
    ) -> None:
        self.storage.set_binding(8, None, "12 ис")
        token = self.storage.calendar_subscription(8, None)["token"]
        self.service.run(self.now())
        previous = self.feed()
        self.replacements.find_for_date.return_value = {"name": "replacements.docx"}
        self.replacements.replacements_for_item.return_value = {
            "11 ис": [
                {"pair": "I", "from": "Алгебра", "to": "нет", "room": "-"},
                {
                    "pair": "I",
                    "from": "Алгебра",
                    "to": "Иванова И.И.(Замена)",
                    "room": "101",
                },
            ],
            "12 ис": [
                {
                    "pair": "III",
                    "from": "Практика",
                    "to": "Иванова И.И.(Обновлено)",
                    "room": "305",
                }
            ],
        }
        with self.assertLogs("app.calendar_service", "ERROR"):
            self.service.run(self.now(23))
        self.assertEqual(self.feed(23), previous)
        self.assertIn("Обновлено", self.service.feed(token, self.now(23)).decode())

    def test_calendar_publication_uses_one_schedule_snapshot(self) -> None:
        self.storage.set_binding(8, None, "12 ис")
        token = self.storage.calendar_subscription(8, None)["token"]

        def refresh_during_publication(_date: dt.date) -> None:
            updated = copy.deepcopy(self.schedules.cache)
            for day in updated["groups"]["12 ис"]["days"].values():
                for lessons in day.values():
                    lessons[0]["subject"] = "Новое расписание"
            self.schedules._cache = updated

        self.replacements.find_for_date.side_effect = refresh_during_publication
        self.service.run(self.now())
        content = self.service.feed(token, self.now()).decode()
        self.assertIn("Практика", content)
        self.assertNotIn("Новое расписание", content)

    def test_unknown_target_does_not_block_other_calendars(self) -> None:
        self.storage.set_binding(8, None, "unknown")
        self.storage.calendar_subscription(8, None)
        with self.assertLogs("app.calendar_service", "ERROR"):
            self.service.run(self.now())
        self.assertEqual(len(event_blocks(self.feed())), 4)

    def test_midnight_catchup_and_history_survive(self) -> None:
        # Even if the bot missed 22:00, the current day is caught up on restart.
        self.service.run(self.now(0, day=DAY + dt.timedelta(days=1)))
        content = self.service.feed(
            self.token, self.now(0, day=DAY + dt.timedelta(days=1))
        )
        self.assertEqual(len(event_blocks(content)), 2)
        self.assertNotIn(b"20261009", content)
        self.service.run(self.now(22, day=DAY + dt.timedelta(days=1)))
        content = self.service.feed(
            self.token, self.now(0, day=DAY + dt.timedelta(days=2))
        )
        self.assertEqual(len(event_blocks(content)), 4)

    def test_rfc_folding_escaping_and_distinct_teacher_uids(self) -> None:
        lesson = {
            "pair": 1,
            "subject": "Я" * 100 + ",;\\\r\nEND:VEVENT",
            "teacher": "Иванова И.И.",
            "room": "А;1",
        }
        schedule = {"date": DAY.isoformat(), "group": "11 ис", "pairs": [lesson]}
        events = calendar_events("group", "11 ис", schedule, ZONE)
        day = {
            "events": json.dumps(events),
            "sequence": 0,
            "updated_at": "20261007T180000Z",
        }
        content = render_calendar("11 ис", [day])
        for line in content.split(b"\r\n"):
            self.assertLessEqual(len(line), 75)
            line.decode("utf-8")
        self.assertEqual(len(event_blocks(content)), 1)
        unfolded = content.decode().replace("\r\n ", "")
        self.assertIn("\\,\\;\\\\\\nEND:VEVENT", unfolded)
        parallel = {
            "date": DAY.isoformat(),
            "pairs": [{**lesson, "group": "11 ис"}, {**lesson, "group": "12 ис"}],
        }
        teacher_events = calendar_events("teacher", "Иванова И.И.", parallel, ZONE)
        self.assertEqual(len({event["uid"] for event in teacher_events}), 2)

    def test_http_get_head_etag_and_invalid_paths(self) -> None:
        now = dt.datetime.now(ZONE)
        self.service.run(now)
        with patch.object(self.service, "_publish_loop"):
            self.service.start()
        self.addCleanup(self.service.close)
        connection = HTTPConnection(
            "127.0.0.1", self.service._server.server_port, timeout=3
        )
        self.addCleanup(connection.close)
        path = f"/calendar/{self.token}.ics"
        connection.request("GET", path)
        response = connection.getresponse()
        self.assertEqual(response.status, 200)
        self.assertEqual(
            response.getheader("Content-Type"), "text/calendar; charset=utf-8"
        )
        etag = response.getheader("ETag")
        self.assertEqual(response.read(), self.service.feed(self.token, now))
        connection.request("HEAD", path)
        response = connection.getresponse()
        self.assertEqual(response.status, 200)
        self.assertEqual(response.read(), b"")
        connection.request("GET", path, headers={"If-None-Match": etag})
        response = connection.getresponse()
        self.assertEqual(response.status, 304)
        self.assertEqual(response.read(), b"")
        for path in ("/", "/../.env", "/calendar/" + "0" * 48 + ".ics"):
            connection.request("GET", path)
            response = connection.getresponse()
            self.assertEqual(response.status, 404)
            response.read()

    def test_disabled_endpoint_and_calendar_commands(self) -> None:
        bot = Bot(self.config)
        self.addCleanup(bot.close)
        bot.handlers.sender = MagicMock()
        message = {
            "chat": {"id": 42, "type": "private"},
            "from": {"id": 42},
            "text": "/calendar",
        }
        bot.handlers.handle_message(message)
        text = bot.handlers.sender.send_message.call_args.args[1]
        self.assertIn(self.token, text)
        self.assertIn("22:00", text)
        self.assertIn("Группа: 11 ис", text)
        message["text"] = "/calendar_off"
        bot.handlers.handle_message(message)
        self.assertIsNone(self.feed())
        bot.handlers.config = replace(self.config, calendar_public_url="")
        message["text"] = "/calendar"
        bot.handlers.handle_message(message)
        self.assertIn(
            "пока не настроен", bot.handlers.sender.send_message.call_args.args[1]
        )
        self.service.config = replace(self.config, calendar_public_url="")
        self.service.start()
        self.assertIsNone(self.service._server)

    def test_group_member_can_revoke_calendar_without_admin_lookup(self) -> None:
        bot = Bot(self.config)
        self.addCleanup(bot.close)
        bot.handlers.sender = MagicMock()
        bot.handlers.telegram.get_chat_member = MagicMock()
        bot.handlers.handle_message(
            {
                "chat": {"id": 42, "type": "supergroup"},
                "from": {"id": 5},
                "text": "/calendar_off",
            }
        )
        self.assertIsNone(self.feed())
        bot.handlers.telegram.get_chat_member.assert_not_called()

    def test_config_validates_calendar_settings(self) -> None:
        env = {"TELEGRAM_BOT_TOKEN": "test", "NUMERATOR_WEEK_START": "2026-10-05"}
        with (
            patch("app.config._load_env_file"),
            patch.dict(os.environ, env, clear=True),
        ):
            default = Config.from_env()
            self.assertEqual(default.calendar_publish_time, dt.time(22))
            self.assertEqual(default.calendar_public_url, "")
            for key, value in (
                ("CALENDAR_PUBLIC_URL", "http://example.org"),
                ("CALENDAR_PUBLIC_URL", "https://user:pass@example.org"),
                ("CALENDAR_PUBLIC_URL", "https://example.org/path"),
                ("CALENDAR_PUBLISH_TIME", "25:00"),
                ("CALENDAR_PUBLISH_TIME", "22:00:30"),
                ("CALENDAR_PORT", "65536"),
            ):
                with (
                    self.subTest(key=key, value=value),
                    patch.dict(os.environ, {key: value}),
                    self.assertRaises(ValueError),
                ):
                    Config.from_env()
