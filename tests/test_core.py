from __future__ import annotations

import datetime as dt
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from app.config import Config
from app.pdf_parser import (
    PAIR_TIMES,
    WEEKDAYS,
    _is_complete_lesson,
    _lesson_from_raw,
    _repair_pair_boundaries,
    normalize_group,
    parse_schedule_pdf,
)
from app.replacement_service import apply_replacements
from app.schedule_service import (
    WEEKDAYS_BY_NUMBER,
    ScheduleSnapshot,
    _select_course_files,
    _semester_key,
    parse_flexible_date,
    week_type_for_date,
)
from app.storage import Storage
from app.teacher_schedule import available_teachers, schedule_for_teacher


class CoreTests(unittest.TestCase):
    def test_group_normalization(self) -> None:
        self.assertEqual(normalize_group("31ИС"), "31 ис")
        self.assertEqual(normalize_group(" 31   ИС "), "31 ис")

    def test_week_parity(self) -> None:
        start = dt.date(2026, 2, 2)
        self.assertEqual(week_type_for_date(start, start), "числитель")
        self.assertEqual(
            week_type_for_date(start + dt.timedelta(days=7), start), "знаменатель"
        )
        self.assertEqual(
            week_type_for_date(start + dt.timedelta(days=14), start), "числитель"
        )

    def test_weekday_mapping_includes_saturday_once(self) -> None:
        self.assertEqual(WEEKDAYS[-1], "суббота")
        self.assertEqual(WEEKDAYS_BY_NUMBER, [*WEEKDAYS, "воскресенье"])

    def test_command_date_format(self) -> None:
        self.assertEqual(parse_flexible_date("07.08.2026"), dt.date(2026, 8, 7))
        with self.assertRaises(ValueError):
            parse_flexible_date("2026-08-07")

    def test_pdf_lesson_tolerates_source_typos(self) -> None:
        self.assertTrue(_is_complete_lesson("Митрофанова А. А.\nМатематика"))
        self.assertTrue(_is_complete_lesson("Терехова И.И\nОбществознание"))
        self.assertTrue(_is_complete_lesson("Егорова\nИнформатика\n207 каб."))
        self.assertTrue(
            _is_complete_lesson("Умбеткалиев Г. А.\nФизическая культура\nспорт.ззал")
        )
        lesson = _lesson_from_raw(
            "Курченок И. Л.\nНачертательная геометрия",
            5,
            "unresolved_pdf_fragment",
        )
        self.assertEqual(lesson["room"], "")
        self.assertEqual(lesson["subject"], "Начертательная геометрия")
        self.assertIn("parse_warning", lesson)

    def test_pdf_pair_boundary_repair(self) -> None:
        fragments = {pair: [] for pair in range(1, 8)}
        fragments[1] = [
            "Попова С. А.\nРусский язык\n417 каб.",
            "Постникова Л. А.\nФизика\n422 каб.",
            "Оленич Д. Л.\nОсновы безопасности и защиты",
        ]
        fragments[2] = ["Родины\n405 каб."]

        _repair_pair_boundaries(fragments)

        self.assertEqual(len(fragments[1]), 2)
        self.assertEqual(
            fragments[2],
            ["Оленич Д. Л.\nОсновы безопасности и защиты\nРодины\n405 каб."],
        )

    def test_pdf_parser_accepts_five_and_six_days(self) -> None:
        for day_count in (5, 6):
            table: list[list[str | None]] = []
            for _ in range(day_count):
                table.append(["", "", "", "11 г", "12 г", "13 г", "14 г"])
                for time in PAIR_TIMES:
                    table.append(
                        [
                            "",
                            "",
                            time,
                            "Иванов И. И.\nПредмет",
                            "Иванов И. И.\nПредмет",
                            "Иванов И. И.\nПредмет",
                            "Иванов И. И.\nПредмет",
                        ]
                    )
            page = MagicMock()
            page.extract_tables.return_value = [table]
            document = MagicMock()
            document.__enter__.return_value.pages = [page]

            with patch("app.pdf_parser.pdfplumber.open", return_value=document):
                parsed = parse_schedule_pdf(Path("schedule.pdf"), 1)

            saturday = parsed["11 г"]["days"]["суббота"]
            expected = 7 if day_count == 6 else 0
            self.assertEqual(len(saturday["числитель"]), expected)
            self.assertEqual(len(saturday["знаменатель"]), expected)

    def test_semester_file_selection(self) -> None:
        files = []
        for semester, modified in (("1", "2025-09-01"), ("2", "2026-02-01")):
            for course in range(1, 5):
                files.append(
                    {
                        "name": f"{course} курс {semester} семестр 2025-2026.pdf",
                        "modified": modified,
                    }
                )
        semester, selected = _select_course_files(files)
        self.assertEqual(semester, "2025-2026:semester-2")
        self.assertEqual([course for course, _ in selected], [1, 2, 3, 4])
        self.assertEqual(_semester_key("1 курс 2 семестр 2025-2026.pdf"), semester)

    def test_replacement_merge(self) -> None:
        schedule = {
            "pairs": [
                {
                    "pair": 2,
                    "time": "09:40-11:10",
                    "teacher": "Старый",
                    "subject": "Старый предмет",
                    "room": "100",
                }
            ],
            "replacements_count": 0,
        }
        replacements = [
            {
                "pair": "II",
                "group": "31 ИС",
                "from": "",
                "to": "Иванов И.И. (Новый предмет)",
                "room": "200",
            }
        ]
        result = apply_replacements(schedule, replacements)
        self.assertEqual(result["pairs"][0]["subject"], "Новый предмет")
        self.assertEqual(result["pairs"][0]["teacher"], "Иванов И.И.")
        self.assertEqual(result["pairs"][0]["room"], "200")
        self.assertEqual(result["pairs"][0]["status"], "replaced")


class StorageTests(unittest.TestCase):
    def test_autopost_disables_only_failing_binding(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            storage = Storage(Path(directory))
            storage.set_binding(1, 10, "31 ис")
            storage.set_binding(2, 20, "21 ис")
            storage.set_autopost(1, 10, True)
            storage.set_autopost(2, 20, True)
            self.assertFalse(storage.record_autopost_failure(1, 10, "error"))
            self.assertFalse(storage.record_autopost_failure(1, 10, "error"))
            self.assertTrue(storage.record_autopost_failure(1, 10, "error"))
            active = {
                (row["chat_id"], row["thread_id"])
                for row in storage.autopost_bindings()
            }
            self.assertEqual(active, {(2, 20)})

    def test_corrupt_cache_is_ignored(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            storage = Storage(Path(directory))
            storage.cache_path.write_text("{broken", encoding="utf-8")
            self.assertIsNone(storage.load_cache())


class ConfigTests(unittest.TestCase):
    def test_numerator_week_must_be_explicit_monday(self) -> None:
        with (
            patch("app.config._load_env_file"),
            patch.dict(os.environ, {"TELEGRAM_BOT_TOKEN": "test"}, clear=True),
            self.assertRaises(ValueError),
        ):
            Config.from_env()
        with (
            patch("app.config._load_env_file"),
            patch.dict(
                os.environ,
                {
                    "TELEGRAM_BOT_TOKEN": "test",
                    "NUMERATOR_WEEK_START": "2026-02-03",
                },
                clear=True,
            ),
            self.assertRaises(ValueError),
        ):
            Config.from_env()


@unittest.skipUnless(
    os.getenv("SCHEDULE_PDF_DIR"), "Set SCHEDULE_PDF_DIR for PDF integration test"
)
class RealPdfTests(unittest.TestCase):
    def test_all_course_pdfs(self) -> None:
        root = Path(os.environ["SCHEDULE_PDF_DIR"])
        current = root / "1 курс 1 семестр 2026-2027.pdf"
        if current.exists():
            suffix = "1 семестр 2026-2027"
            expected_counts = {1: 25, 2: 24, 3: 20, 4: 14}
        else:
            suffix = "2 семестр 2025-2026"
            expected_counts = {1: 23, 2: 23, 3: 20, 4: 16}
        total = 0
        all_groups = {}
        for course, expected in expected_counts.items():
            path = root / f"{course} курс {suffix}.pdf"
            parsed = parse_schedule_pdf(path, course)
            self.assertEqual(len(parsed), expected)
            all_groups.update(parsed)
            if course == 3 and not current.exists():
                tuesday = parsed["31 ис"]["days"]["вторник"]
                numerator = {
                    item["pair"]: item["subject"] for item in tuesday["числитель"]
                }
                denominator = {
                    item["pair"]: item["subject"] for item in tuesday["знаменатель"]
                }
                self.assertEqual(numerator[2], "МДК 05.01")
                self.assertEqual(denominator[2], "МДК 05.02")
            for group_data in parsed.values():
                warning_raw = {item["raw"] for item in group_data.get("warnings", [])}
                lessons = [
                    lesson
                    for day in group_data["days"].values()
                    for week in day.values()
                    for lesson in week
                ]
                preserved = {
                    lesson["raw"] for lesson in lessons if lesson.get("parse_warning")
                }
                self.assertTrue(warning_raw.issubset(preserved))
            total += len(parsed)
        if current.exists():
            self.assertEqual(total, 83)
            cache = type("Cache", (), {"cache": {"groups": all_groups}})()
            self.assertNotIn("Х", available_teachers(cache))
            schedules = ScheduleSnapshot({"groups": all_groups})
            numerator_start = dt.date(2026, 2, 2)
            for date, group in (
                (dt.date(2026, 10, 1), "22 иск"),
                (dt.date(2026, 10, 8), "21 ис"),
            ):
                result = schedule_for_teacher(
                    schedules, "Ершов Д. Д.", date, numerator_start
                )
                affected = [
                    lesson
                    for lesson in result["pairs"]
                    if lesson["group"] in {"21 ис", "22 иск"}
                ]
                self.assertEqual(
                    [(lesson["group"], lesson["pair"]) for lesson in affected],
                    [(group, 2)],
                )
                self.assertEqual(
                    affected[0]["subject"], "Основы проектирования баз данных"
                )
                self.assertEqual(affected[0]["room"], "111 каб.")
        else:
            self.assertEqual(total, 82)
