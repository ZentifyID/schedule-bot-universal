from __future__ import annotations

import datetime as dt
from typing import Any

from .replacement_service import apply_replacements
from .schedule_service import ScheduleRepository, ScheduleSnapshot
from .teacher_schedule import schedule_for_teacher


def schedule_for_target(
    schedules: ScheduleRepository | ScheduleSnapshot,
    target_type: str,
    name: str,
    date: dt.date,
    numerator_week_start: dt.date,
    replacements: dict[str, list[dict[str, str]]] | None = None,
) -> dict[str, Any]:
    if target_type == "teacher":
        return schedule_for_teacher(
            schedules, name, date, numerator_week_start, replacements
        )
    if target_type != "group":
        raise ValueError(f"Unknown schedule target type: {target_type}")
    base = schedules.schedule_for(name, date, numerator_week_start)
    rows = (replacements or {}).get(base["group"], [])
    return apply_replacements(base, rows) if rows else base
