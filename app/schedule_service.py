from __future__ import annotations

import copy
import datetime as dt
import hashlib
import re
import threading
from typing import Any

from .pdf_parser import PARSER_VERSION, WEEKDAYS, normalize_group, parse_schedule_pdf
from .storage import Storage
from .yandex_disk import download_public_file, file_fingerprint, list_public_files

WEEKDAYS_BY_NUMBER = [*WEEKDAYS, "воскресенье"]
CACHE_SCHEMA_VERSION = 2


def _semester_key(name: str) -> str | None:
    semester = re.search(r"\b([12])\s*семестр\b", name, flags=re.IGNORECASE)
    years = re.search(r"(20\d{2})\s*[-/]\s*(20\d{2})", name)
    if not semester or not years:
        return None
    return f"{years.group(1)}-{years.group(2)}:semester-{semester.group(1)}"


def _select_course_files(
    files: list[dict[str, Any]],
) -> tuple[str | None, list[tuple[int, dict[str, Any]]]]:
    by_semester: dict[str | None, dict[int, dict[str, Any]]] = {}
    for item in files:
        name = str(item.get("name", ""))
        course_match = re.match(r"^([1-4])\s*курс\b.*\.pdf$", name, flags=re.IGNORECASE)
        if not course_match:
            continue
        course = int(course_match.group(1))
        semester = _semester_key(name)
        current = by_semester.setdefault(semester, {}).get(course)
        if current is None or str(item.get("modified", "")) > str(
            current.get("modified", "")
        ):
            by_semester[semester][course] = item

    complete = [
        (semester, items)
        for semester, items in by_semester.items()
        if set(items) == {1, 2, 3, 4}
    ]
    if not complete:
        raise ValueError("No complete four-course semester set found on Yandex Disk")

    def semester_order(
        value: tuple[str | None, dict[int, dict[str, Any]]],
    ) -> tuple[int, int, int, str]:
        semester, items = value
        match = re.fullmatch(r"(20\d{2})-(20\d{2}):semester-([12])", semester or "")
        modified = max(str(item.get("modified", "")) for item in items.values())
        if not match:
            return (0, 0, 0, modified)
        return (1, int(match.group(1)), int(match.group(3)), modified)

    semester, items = max(complete, key=semester_order)
    return semester, sorted(items.items())


def week_type_for_date(target_date: dt.date, numerator_week_start: dt.date) -> str:
    week_delta = (target_date - numerator_week_start).days // 7
    return "числитель" if week_delta % 2 == 0 else "знаменатель"


def parse_flexible_date(value: str) -> dt.date:
    try:
        day, month, year = map(int, value.strip().split("."))
        return dt.date(year, month, day)
    except ValueError:
        raise ValueError("Используй формат DD.MM.YYYY") from None


def _validated_pdf(content: bytes, name: str) -> bytes:
    if not content.lstrip().startswith(b"%PDF-"):
        raise ValueError(f"Downloaded file is not a PDF: {name}")
    return content


class ScheduleSnapshot:
    """One independent cache version for the entire calculation."""

    def __init__(self, cache: dict[str, Any]):
        self._cache = copy.deepcopy(cache)

    @property
    def cache(self) -> dict[str, Any]:
        return self._cache

    def snapshot(self) -> ScheduleSnapshot:
        return self

    def groups(self, course: int | None = None) -> list[str]:
        groups = self.cache.get("groups", {})
        values = [
            group
            for group, payload in groups.items()
            if course is None or int(payload.get("course", 0)) == course
        ]
        return sorted(values, key=lambda value: (int(value[:2]), value.casefold()))

    def schedule_for(
        self, group: str, target_date: dt.date, numerator_week_start: dt.date
    ) -> dict[str, Any]:
        canonical = normalize_group(group)
        group_data = self.cache.get("groups", {}).get(canonical)
        if not group_data:
            raise KeyError(f"Группа {group} не найдена")
        weekday = WEEKDAYS_BY_NUMBER[target_date.weekday()]
        week_type = week_type_for_date(target_date, numerator_week_start)
        lessons: list[dict[str, Any]] = []
        if weekday in WEEKDAYS:
            lessons = copy.deepcopy(
                group_data["days"].get(weekday, {}).get(week_type, [])
            )
        return {
            "group": canonical,
            "date": target_date.isoformat(),
            "weekday": weekday,
            "week_type": week_type,
            "pairs": lessons,
            "replacements_count": 0,
            "source": "pdf",
        }


class ScheduleRepository(ScheduleSnapshot):
    def __init__(self, storage: Storage, public_url: str):
        self.storage = storage
        self.public_url = public_url
        self._lock = threading.Lock()
        self._cache = storage.load_cache()

    @property
    def cache(self) -> dict[str, Any]:
        if self._cache is None:
            raise RuntimeError("Schedule cache has not been initialized")
        return self._cache

    @property
    def has_cache(self) -> bool:
        return self._cache is not None

    def snapshot(self) -> ScheduleSnapshot:
        return ScheduleSnapshot(self.cache)

    def refresh(
        self, force: bool = False, expected_semester: str | None = None
    ) -> tuple[bool, str]:
        if not self._lock.acquire(blocking=False):
            return False, "Обновление уже выполняется"
        try:
            files = list_public_files(self.public_url)
            semester_key, selected = _select_course_files(files)
            if expected_semester and semester_key != expected_semester:
                raise ValueError(
                    f"Yandex Disk semester is {semester_key}, expected {expected_semester}"
                )

            fingerprint = hashlib.sha256(
                "\n".join(file_fingerprint(item) for _, item in selected).encode(
                    "utf-8"
                )
            ).hexdigest()
            versions_match = bool(
                self._cache
                and self._cache.get("cache_schema_version") == CACHE_SCHEMA_VERSION
                and self._cache.get("parser_version") == PARSER_VERSION
            )
            if (
                not force
                and versions_match
                and self._cache
                and self._cache.get("source_fingerprint") == fingerprint
            ):
                return False, "Файлы расписания не изменились"

            source_dir = self.storage.data_dir / "source_pdfs"
            source_dir.mkdir(parents=True, exist_ok=True)
            all_groups: dict[str, Any] = {}
            source_files: list[dict[str, Any]] = []
            rebuilt_courses: list[int] = []
            previous_sources = {
                int(item["course"]): item
                for item in (self._cache or {}).get("source_files", [])
                if item.get("course")
            }
            previous_groups = (self._cache or {}).get("groups", {})
            for course, item in selected:
                name = str(item["name"])
                local_path = source_dir / f"course_{course}.pdf"
                item_fingerprint = file_fingerprint(item)
                previous = previous_sources.get(course) or {}
                source_unchanged = previous.get("fingerprint") == item_fingerprint
                reusable = {
                    group: payload
                    for group, payload in previous_groups.items()
                    if int(payload.get("course", 0)) == course
                }

                if not force and versions_match and source_unchanged and reusable:
                    parsed = reusable
                else:
                    if not source_unchanged or not local_path.exists():
                        content = download_public_file(self.public_url, item)
                        local_path.write_bytes(_validated_pdf(content, name))
                    parsed = parse_schedule_pdf(local_path, course=course)
                    rebuilt_courses.append(course)
                duplicates = set(all_groups).intersection(parsed)
                if duplicates:
                    raise ValueError(f"Duplicate groups in PDFs: {sorted(duplicates)}")
                all_groups.update(parsed)
                source_files.append(
                    {
                        "course": course,
                        "name": name,
                        "modified": item.get("modified"),
                        "fingerprint": item_fingerprint,
                    }
                )

            parser_warnings = sum(
                len(payload.get("warnings", [])) for payload in all_groups.values()
            )
            new_cache = {
                "cache_schema_version": CACHE_SCHEMA_VERSION,
                "parser_version": PARSER_VERSION,
                "generated_at": dt.datetime.now(dt.timezone.utc).isoformat(),
                "source_fingerprint": fingerprint,
                "semester_key": semester_key,
                "parser_warnings": parser_warnings,
                "source_files": source_files,
                "groups": all_groups,
            }
            self.storage.save_cache(new_cache)
            self._cache = new_cache
            rebuilt = ", ".join(map(str, rebuilt_courses)) or "нет"
            return True, (
                f"Готово: {len(all_groups)} групп; переработаны курсы: {rebuilt}; "
                f"предупреждений парсера: {parser_warnings}"
            )
        finally:
            self._lock.release()
