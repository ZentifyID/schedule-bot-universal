from __future__ import annotations

import datetime as dt
import sqlite3
import tempfile
import unittest
from contextlib import closing
from pathlib import Path
from types import SimpleNamespace

from app.autopost import AutopostService
from app.schedule_formatter import (
    format_schedule,
    format_teacher_schedule,
    format_teacher_weekday_schedule,
)
from app.schedule_service import ScheduleRepository
from app.storage import Storage
from app.teacher_schedule import (
    available_teachers,
    schedule_for_teacher,
    teacher_key,
    teacher_names,
)
from app.telegram_handlers import TelegramHandlers

WEEK_START = dt.date(2026, 9, 21)
TUESDAY = dt.date(2026, 9, 22)


def lesson(pair: int, teacher: str, subject: str) -> dict:
    return {"pair": pair, "teacher": teacher, "subject": subject, "room": "101"}


def repository(directory: str) -> ScheduleRepository:
    storage = Storage(Path(directory))
    groups = {}
    for group, lessons in {
        "11 ис": [
            lesson(1, "Иванова И. И.", "Алгебра"),
            lesson(2, "Петров П.П.", "Физика"),
        ],
        "12 ис": [lesson(1, "Иванова И.И./Сидоров С.С.", "Практика")],
        "13 ис": [],
    }.items():
        groups[group] = {
            "course": 1,
            "days": {"вторник": {"числитель": lessons, "знаменатель": []}},
        }
    storage.save_cache({"groups": groups})
    return ScheduleRepository(storage, "https://example.invalid")


class TeacherScheduleTests(unittest.TestCase):
    def test_pdf_typo_zybina_yu_is_zybina_v(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            storage = Storage(Path(directory))
            storage.save_cache(
                {
                    "groups": {
                        "42 иск": {
                            "course": 4,
                            "days": {
                                "четверг": {
                                    "числитель": [
                                        lesson(1, "Зыбина О. Ю.", "Информатика")
                                    ],
                                    "знаменатель": [],
                                }
                            },
                        }
                    }
                }
            )
            schedules = ScheduleRepository(storage, "https://example.invalid")
            self.assertEqual(available_teachers(schedules), ["Зыбина О. В."])
            self.assertEqual(teacher_names("Х/Зыбина О. Ю."), ["Зыбина О. В."])
            self.assertEqual(teacher_names("Х"), [])
            self.assertEqual(teacher_key("Зыбина О. Ю."), teacher_key("Зыбина О. В."))
            day = dt.date(2026, 9, 24)
            result = schedule_for_teacher(schedules, "Зыбина О. В.", day, WEEK_START)
            self.assertEqual(result["teacher"], "Зыбина О. В.")
            self.assertEqual(
                schedule_for_teacher(schedules, "Зыбина О. Ю.", day, WEEK_START)[
                    "teacher"
                ],
                "Зыбина О. В.",
            )
            self.assertEqual(
                [(pair["group"], pair["pair"]) for pair in result["pairs"]],
                [("42 иск", 1)],
            )
            self.assertIn(
                "Зыбина О. В.",
                format_schedule(schedules.schedule_for("42 иск", day, WEEK_START)),
            )
            self.assertNotIn(
                "Зыбина О. Ю.",
                format_schedule(schedules.schedule_for("42 иск", day, WEEK_START)),
            )

    def test_teacher_schedule_keeps_parallel_groups_and_changes(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            schedules = repository(directory)
            self.assertEqual(
                teacher_key(" Иванова И. И. "), teacher_key("Иванова И.И.")
            )
            self.assertEqual(teacher_names("Х/Иванова И.И."), ["Иванова И.И."])
            self.assertIn("Сидоров С.С.", available_teachers(schedules))
            replacements = {
                "11 ис": [
                    {
                        "pair": "I",
                        "from": "Иванова И.И.(Алгебра)",
                        "to": "нет",
                        "room": "-",
                    }
                ],
                "12 ис": [
                    {
                        "pair": "I",
                        "from": "Иванова И.И.(Практика)",
                        "to": "Петров П. П.(Физика)",
                        "room": "205",
                    }
                ],
                "13 ис": [
                    {
                        "pair": "I",
                        "from": "нет",
                        "to": "Иванова И.И.(Консультация)",
                        "room": "303",
                    }
                ],
            }
            base = schedule_for_teacher(schedules, "Иванова И.И.", TUESDAY, WEEK_START)
            self.assertEqual(
                [pair["group"] for pair in base["pairs"]], ["11 ис", "12 ис"]
            )
            result = schedule_for_teacher(
                schedules, "Иванова И.И.", TUESDAY, WEEK_START, replacements
            )
            self.assertEqual([pair["group"] for pair in result["pairs"]], ["13 ис"])
            self.assertEqual(
                [(item["group"], item["status"]) for item in result["changes"]],
                [("11 ис", "cancelled"), ("12 ис", "removed")],
            )
            self.assertIn("Снятые и отменённые пары", format_teacher_schedule(result))
            incoming = schedule_for_teacher(
                schedules, "Петров П.П.", TUESDAY, WEEK_START, replacements
            )
            self.assertEqual(
                [(pair["group"], pair["pair"]) for pair in incoming["pairs"]],
                [("12 ис", 1), ("11 ис", 2)],
            )

    def test_docx_old_teacher_is_visible_even_without_pdf_pair(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            schedules = repository(directory)
            result = schedule_for_teacher(
                schedules,
                "Иванова И.И.",
                TUESDAY,
                WEEK_START,
                {
                    "13 ис": [
                        {
                            "pair": "III",
                            "from": "Иванова И. И.(Практика)",
                            "to": "Сидоров С.С.(Практика)",
                            "room": "201",
                        }
                    ],
                },
            )
            self.assertEqual(result["changes"][0]["group"], "13 ис")
            self.assertEqual(result["changes"][0]["pair"], 3)

    def test_week_format_keeps_two_groups_on_same_pair(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            schedules = repository(directory)
            first = schedule_for_teacher(schedules, "Иванова И.И.", TUESDAY, WEEK_START)
            second = schedule_for_teacher(
                schedules, "Иванова И.И.", TUESDAY + dt.timedelta(days=7), WEEK_START
            )
            text = format_teacher_weekday_schedule(
                "Иванова И.И.", "вторник", first["pairs"], second["pairs"]
            )
            self.assertIn("11 ИС", text)
            self.assertIn("12 ИС", text)
            self.assertIn("Ч:", text)


class TeacherStorageTests(unittest.TestCase):
    def test_legacy_bindings_and_history_are_migrated(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "bot.sqlite3"
            with closing(sqlite3.connect(path)) as db:
                db.executescript("""
                    CREATE TABLE bindings (
                        chat_id INTEGER NOT NULL, thread_id INTEGER NOT NULL DEFAULT 0,
                        group_name TEXT NOT NULL, autopost INTEGER NOT NULL DEFAULT 0,
                        autopost_failures INTEGER NOT NULL DEFAULT 0, autopost_last_error TEXT,
                        updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                        PRIMARY KEY (chat_id, thread_id));
                    CREATE TABLE sent_autoposts (
                        chat_id INTEGER NOT NULL, thread_id INTEGER NOT NULL DEFAULT 0,
                        group_name TEXT NOT NULL, target_date TEXT NOT NULL,
                        fingerprint TEXT NOT NULL, sent_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                        PRIMARY KEY (chat_id, thread_id, group_name, target_date));
                    INSERT INTO bindings(chat_id, thread_id, group_name, autopost) VALUES (1, 0, '11 ис', 1);
                    INSERT INTO sent_autoposts(chat_id, thread_id, group_name, target_date, fingerprint)
                    VALUES (1, 0, '11 ис', '2026-09-22', 'old');
                """)
            storage = Storage(Path(directory))
            self.assertEqual(storage.get_binding(1, None)["target_name"], "11 ис")
            self.assertEqual(
                storage.autopost_fingerprint(1, 0, "11 ис", "2026-09-22"), "old"
            )
            storage = Storage(Path(directory))
            storage.set_binding(1, None, "Иванова И.И.", "teacher")
            storage.mark_autopost(1, 0, "Иванова И.И.", "2026-09-22", "new", "teacher")
            self.assertEqual(storage.get_binding(1, None)["target_type"], "teacher")
            self.assertEqual(
                storage.autopost_fingerprint(
                    1, 0, "Иванова И.И.", "2026-09-22", "teacher"
                ),
                "new",
            )
            self.assertEqual(
                storage.autopost_fingerprint(1, 0, "11 ис", "2026-09-22"), "old"
            )


class FakeReplacements:
    def __init__(self) -> None:
        self.item = {
            "name": "22.09.2026.docx",
            "path": "/22.docx",
            "modified": "1",
            "size": 100,
        }
        self.by_group = {
            "13 ис": [
                {
                    "pair": "I",
                    "from": "нет",
                    "to": "Иванова И.И.(Консультация)",
                    "room": "303",
                }
            ]
        }

    def find_for_date(self, _date: dt.date) -> dict:
        return self.item

    def replacements_for_item(self, _item: dict) -> dict:
        return self.by_group

    def recent_teachers(self) -> list[str]:
        return [
            row["to"].split("(", 1)[0]
            for rows in self.by_group.values()
            for row in rows
            if "(" in row["to"]
        ]


class FakeSender:
    def __init__(self) -> None:
        self.messages: list[tuple[str, dict | None]] = []
        self.edits: list[tuple[int, str, dict | None]] = []

    def send_message(
        self,
        _chat_id: int,
        text: str,
        _thread_id: int | None = None,
        keyboard: dict | None = None,
        **_kwargs: object,
    ) -> None:
        self.messages.append((text, keyboard))

    def edit_message(
        self,
        _chat_id: int,
        message_id: int,
        text: str,
        keyboard: dict | None = None,
    ) -> None:
        self.edits.append((message_id, text, keyboard))


class TeacherIntegrationTests(unittest.TestCase):
    def test_setup_course_and_teacher_pages_edit_the_original_message(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            schedules = repository(directory)
            replacements = FakeReplacements()
            replacements.recent_teachers = lambda: [
                f"Тестов{index:02} А.А." for index in range(20)
            ]
            sender = FakeSender()
            handlers = TelegramHandlers(
                config=SimpleNamespace(numerator_week_start=WEEK_START),
                storage=schedules.storage,
                schedules=schedules,
                replacements=replacements,
                telegram=SimpleNamespace(answer_callback=lambda _id: None),
                sender=sender,
                timezone=dt.timezone.utc,
                validate_semester=lambda: None,
            )
            message = {
                "chat": {"id": 1, "type": "private"},
                "from": {"id": 1},
                "message_id": 42,
            }
            handlers.handle_message({**message, "text": "/setup"})
            for data in (
                "setup:groups",
                "course:1",
                "courses",
                "setup:teacher",
                "teachers:1",
                "teachers:0",
            ):
                handlers.handle_callback(
                    {"id": data, "data": data, "message": message, "from": {"id": 1}}
                )
            self.assertEqual(len(sender.messages), 1)
            self.assertEqual([edit[0] for edit in sender.edits], [42] * 6)
            self.assertIn("группу 1 курса", sender.edits[1][1])
            self.assertIn("страница 2/2", sender.edits[4][1])
            self.assertIn("страница 1/2", sender.edits[5][1])

    def test_button_bind_and_request_date(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            schedules = repository(directory)
            sender = FakeSender()
            handlers = TelegramHandlers(
                config=SimpleNamespace(numerator_week_start=WEEK_START),
                storage=schedules.storage,
                schedules=schedules,
                replacements=FakeReplacements(),
                telegram=SimpleNamespace(answer_callback=lambda _id: None),
                sender=sender,
                timezone=dt.timezone.utc,
                validate_semester=lambda: None,
            )
            message = {"chat": {"id": 1, "type": "private"}, "from": {"id": 1}}
            handlers.handle_message({**message, "text": "/teacher Иванова"})
            self.assertEqual(sender.messages, [])
            handlers.handle_message({**message, "text": "/setup"})
            handlers.handle_callback(
                {
                    "id": "1",
                    "data": "setup:teacher",
                    "message": message,
                    "from": {"id": 1},
                }
            )
            buttons = sender.messages[-1][1]["inline_keyboard"]
            token = next(
                button["callback_data"]
                for row in buttons
                for button in row
                if button["text"].startswith("Иванова")
            )
            handlers.handle_callback(
                {"id": "1", "data": token, "message": message, "from": {"id": 1}}
            )
            self.assertEqual(
                schedules.storage.get_binding(1, None)["target_type"], "teacher"
            )
            handlers.handle_message({**message, "text": "/date 22.09.2026"})
            self.assertIn("Преподаватель: <b>Иванова", sender.messages[-1][0])
            self.assertIn("13 ИС", sender.messages[-1][0])

    def test_replacement_only_teacher_can_be_selected_by_button(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            schedules = repository(directory)
            replacements = FakeReplacements()
            replacements.by_group["13 ис"][0]["to"] = "Муканова К.Ш.(Биология)"
            sender = FakeSender()
            handlers = TelegramHandlers(
                config=SimpleNamespace(numerator_week_start=WEEK_START),
                storage=schedules.storage,
                schedules=schedules,
                replacements=replacements,
                telegram=SimpleNamespace(answer_callback=lambda _id: None),
                sender=sender,
                timezone=dt.timezone.utc,
                validate_semester=lambda: None,
            )
            message = {"chat": {"id": 1, "type": "private"}, "from": {"id": 1}}
            handlers.handle_callback(
                {
                    "id": "1",
                    "data": "setup:teacher",
                    "message": message,
                    "from": {"id": 1},
                }
            )
            token = next(
                button["callback_data"]
                for row in sender.messages[-1][1]["inline_keyboard"]
                for button in row
                if button["text"] == "Муканова К.Ш."
            )
            handlers.handle_callback(
                {"id": "2", "data": token, "message": message, "from": {"id": 1}}
            )
            self.assertEqual(
                schedules.storage.get_binding(1, None)["target_name"], "Муканова К.Ш."
            )
            handlers.handle_message({**message, "text": "/date 22.09.2026"})
            self.assertIn("13 ИС", sender.messages[-1][0])

    def test_teacher_buttons_are_paginated_and_unknown_name_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            schedules = repository(directory)
            replacements = FakeReplacements()
            replacements.recent_teachers = lambda: [
                f"Тестов{index:02} А.А." for index in range(20)
            ]
            sender = FakeSender()
            handlers = TelegramHandlers(
                config=SimpleNamespace(numerator_week_start=WEEK_START),
                storage=schedules.storage,
                schedules=schedules,
                replacements=replacements,
                telegram=SimpleNamespace(answer_callback=lambda _id: None),
                sender=sender,
                timezone=dt.timezone.utc,
                validate_semester=lambda: None,
            )
            message = {"chat": {"id": 1, "type": "private"}, "from": {"id": 1}}
            for token in ("setup:teacher", "teachers:1", "teacher:0000000000000000"):
                handlers.handle_callback(
                    {"id": token, "data": token, "message": message, "from": {"id": 1}}
                )
            self.assertIn("страница 2/2", sender.messages[-2][0])
            self.assertIsNone(schedules.storage.get_binding(1, None))

    def test_teacher_autopost_only_resends_on_own_change(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            schedules = repository(directory)
            storage = schedules.storage
            storage.set_binding(1, None, "Иванова И.И.", "teacher")
            storage.set_autopost(1, None, True)
            replacements = FakeReplacements()
            sender = FakeSender()
            service = AutopostService(
                config=SimpleNamespace(numerator_week_start=WEEK_START),
                storage=storage,
                schedules=schedules,
                replacements=replacements,
                sender=sender,
                validate_semester=lambda: None,
            )
            now = dt.datetime(2026, 9, 21, 12, tzinfo=dt.timezone.utc)
            service.run(now)
            self.assertEqual(len(sender.messages), 1)
            replacements.by_group["13 ис"][0]["room"] = "304"
            service.run(now)
            self.assertEqual(len(sender.messages), 2)
            replacements.by_group["13 ис"][0]["from"] = "Другая запись"
            service.run(now)
            self.assertEqual(len(sender.messages), 2)
