from __future__ import annotations

import datetime as dt
import re
from typing import Any

from .pdf_parser import PAIR_TIMES_DISPLAY
from .replacement_service import _pair_number, _replacement_lesson, apply_replacements
from .schedule_service import (
    WEEKDAYS_BY_NUMBER,
    ScheduleRepository,
    ScheduleSnapshot,
    week_type_for_date,
)

_ZYBINA_PDF_TYPO = re.compile(r"\bЗыбина\s+О\.\s*Ю\.", re.IGNORECASE)


def correct_teacher_name(value: str) -> str:
    return _ZYBINA_PDF_TYPO.sub("Зыбина О. В.", value)


def teacher_key(value: str) -> str:
    return re.sub(
        r"[\s.,]", "", correct_teacher_name(value).casefold().replace("ё", "е")
    )


def teacher_names(value: str) -> list[str]:
    return [
        correct_teacher_name(name.strip())
        for name in value.split("/")
        if len(teacher_key(name)) > 1 and name.strip().casefold() != "нет"
    ]


def has_teacher(value: str, teacher: str) -> bool:
    key = teacher_key(teacher)
    return any(teacher_key(name) == key for name in teacher_names(value))


def available_teachers(schedules: ScheduleRepository) -> list[str]:
    names: dict[str, str] = {}
    for group in schedules.cache.get("groups", {}).values():
        for day in group.get("days", {}).values():
            for lessons in day.values():
                for lesson in lessons:
                    for name in teacher_names(str(lesson.get("teacher", ""))):
                        names.setdefault(teacher_key(name), name)
    return sorted(names.values(), key=lambda name: name.casefold())


def schedule_for_teacher(
    schedules: ScheduleRepository | ScheduleSnapshot,
    teacher: str,
    target_date: dt.date,
    numerator_week_start: dt.date,
    replacements_by_group: dict[str, list[dict[str, str]]] | None = None,
) -> dict[str, Any]:
    schedules = schedules.snapshot()
    active: list[dict[str, Any]] = []
    changes: list[dict[str, Any]] = []
    for group in schedules.groups():
        base = schedules.schedule_for(group, target_date, numerator_week_start)
        replacements = (replacements_by_group or {}).get(group, [])
        final = apply_replacements(base, replacements) if replacements else base
        final_by_pair = {int(lesson["pair"]): lesson for lesson in final["pairs"]}
        removed_pairs: set[int] = set()
        for lesson in final["pairs"]:
            if (
                has_teacher(str(lesson.get("teacher", "")), teacher)
                and lesson.get("status") != "cancelled"
            ):
                active.append({**lesson, "group": group})
        for lesson in base["pairs"]:
            pair = int(lesson["pair"])
            if not has_teacher(str(lesson.get("teacher", "")), teacher):
                continue
            updated = final_by_pair.get(pair)
            if (
                updated
                and has_teacher(str(updated.get("teacher", "")), teacher)
                and updated.get("status") != "cancelled"
            ):
                continue
            if updated and updated.get("status") in {"cancelled", "replaced"}:
                changes.append(
                    {
                        **lesson,
                        "group": group,
                        "status": "cancelled"
                        if updated["status"] == "cancelled"
                        else "removed",
                    }
                )
                removed_pairs.add(pair)
        for replacement in replacements:
            pair = _pair_number(replacement.get("pair", ""))
            if pair is None or pair in removed_pairs:
                continue
            old_teacher, old_subject = _replacement_lesson(replacement.get("from", ""))
            new_teacher, _ = _replacement_lesson(replacement.get("to", ""))
            if has_teacher(old_teacher, teacher) and not has_teacher(
                new_teacher, teacher
            ):
                changes.append(
                    {
                        "pair": pair,
                        "time": PAIR_TIMES_DISPLAY[pair],
                        "group": group,
                        "subject": old_subject,
                        "room": "",
                        "status": "cancelled"
                        if replacement["to"].strip().casefold() == "нет"
                        else "removed",
                    }
                )

    active.sort(key=lambda lesson: (int(lesson["pair"]), str(lesson["group"])))
    changes.sort(key=lambda lesson: (int(lesson["pair"]), str(lesson["group"])))
    return {
        "teacher": correct_teacher_name(teacher),
        "date": target_date.isoformat(),
        "weekday": WEEKDAYS_BY_NUMBER[target_date.weekday()],
        "week_type": week_type_for_date(target_date, numerator_week_start),
        "pairs": active,
        "changes": changes,
    }
