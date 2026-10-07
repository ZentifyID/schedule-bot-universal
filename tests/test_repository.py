from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from app.pdf_parser import PARSER_VERSION
from app.schedule_service import ScheduleRepository
from app.storage import Storage


def source_files(course_two_modified: str = "2026-02-01") -> list[dict[str, object]]:
    return [
        {
            "name": f"{course} курс 2 семестр 2025-2026.pdf",
            "path": f"/{course}.pdf",
            "modified": course_two_modified if course == 2 else "2026-02-01",
            "size": 100 + course,
            "type": "file",
        }
        for course in range(1, 5)
    ]


def parsed_course(_path: Path, course: int) -> dict[str, object]:
    return {
        f"{course}1 тест": {
            "course": course,
            "warnings": [],
            "days": {},
        }
    }


class RepositoryRefreshTests(unittest.TestCase):
    def test_failed_pdf_rebuild_preserves_cache_and_previous_source(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            storage = Storage(Path(directory))
            repository = ScheduleRepository(storage, "https://example.invalid")
            with (
                patch(
                    "app.schedule_service.list_public_files",
                    return_value=source_files(),
                ),
                patch(
                    "app.schedule_service.download_public_file",
                    return_value=b"%PDF-1.4 old",
                ),
                patch(
                    "app.schedule_service.parse_schedule_pdf", side_effect=parsed_course
                ),
            ):
                repository.refresh()
            previous = storage.load_cache()
            with (
                patch(
                    "app.schedule_service.list_public_files",
                    return_value=source_files("2026-02-02"),
                ),
                patch(
                    "app.schedule_service.download_public_file",
                    return_value=b"%PDF-1.4 broken",
                ),
                patch(
                    "app.schedule_service.parse_schedule_pdf",
                    side_effect=ValueError("bad table"),
                ),
                self.assertRaisesRegex(ValueError, "bad table"),
            ):
                repository.refresh()
            self.assertEqual(storage.load_cache(), previous)
            self.assertEqual(repository.cache, previous)
            self.assertEqual(
                (storage.data_dir / "source_pdfs/course_2.pdf").read_bytes(),
                b"%PDF-1.4 old",
            )
            self.assertEqual(
                list((storage.data_dir / "source_pdfs").glob("*.tmp.pdf")), []
            )

    def test_failed_json_write_keeps_previous_cache_and_removes_temporary(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            storage = Storage(Path(directory))
            storage.save_cache({"groups": {}})
            with self.assertRaises(TypeError):
                storage.save_cache({"invalid": object()})
            self.assertEqual(storage.load_cache(), {"groups": {}})
            self.assertFalse(storage.cache_path.with_suffix(".tmp").exists())

    def test_only_changed_course_is_reparsed(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            repository = ScheduleRepository(
                Storage(Path(directory)), "https://example.invalid"
            )
            with (
                patch(
                    "app.schedule_service.list_public_files",
                    return_value=source_files(),
                ),
                patch(
                    "app.schedule_service.download_public_file",
                    return_value=b"%PDF-1.4",
                ) as download,
                patch(
                    "app.schedule_service.parse_schedule_pdf", side_effect=parsed_course
                ) as parse,
            ):
                changed, _ = repository.refresh(
                    expected_semester="2025-2026:semester-2"
                )
                self.assertTrue(changed)
                self.assertEqual(download.call_count, 4)
                self.assertEqual(parse.call_count, 4)

            with (
                patch(
                    "app.schedule_service.list_public_files",
                    return_value=source_files(),
                ),
                patch("app.schedule_service.download_public_file") as download,
                patch("app.schedule_service.parse_schedule_pdf") as parse,
            ):
                changed, _ = repository.refresh(
                    expected_semester="2025-2026:semester-2"
                )
                self.assertFalse(changed)
                download.assert_not_called()
                parse.assert_not_called()

            with (
                patch(
                    "app.schedule_service.list_public_files",
                    return_value=source_files(course_two_modified="2026-02-02"),
                ),
                patch(
                    "app.schedule_service.download_public_file",
                    return_value=b"%PDF-1.4",
                ) as download,
                patch(
                    "app.schedule_service.parse_schedule_pdf", side_effect=parsed_course
                ) as parse,
            ):
                changed, _ = repository.refresh(
                    expected_semester="2025-2026:semester-2"
                )
                self.assertTrue(changed)
                self.assertEqual(download.call_count, 1)
                self.assertEqual(parse.call_count, 1)
                self.assertEqual(parse.call_args.kwargs["course"], 2)

    def test_parser_version_change_rebuilds_from_local_pdfs(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            repository = ScheduleRepository(
                Storage(Path(directory)), "https://example.invalid"
            )
            with (
                patch(
                    "app.schedule_service.list_public_files",
                    return_value=source_files(),
                ),
                patch(
                    "app.schedule_service.download_public_file",
                    return_value=b"%PDF-1.4",
                ),
                patch(
                    "app.schedule_service.parse_schedule_pdf", side_effect=parsed_course
                ),
            ):
                repository.refresh(expected_semester="2025-2026:semester-2")

            repository.cache["parser_version"] = PARSER_VERSION - 1
            with (
                patch(
                    "app.schedule_service.list_public_files",
                    return_value=source_files(),
                ),
                patch("app.schedule_service.download_public_file") as download,
                patch(
                    "app.schedule_service.parse_schedule_pdf", side_effect=parsed_course
                ) as parse,
            ):
                changed, _ = repository.refresh(
                    expected_semester="2025-2026:semester-2"
                )
                self.assertTrue(changed)
                download.assert_not_called()
                self.assertEqual(parse.call_count, 4)
