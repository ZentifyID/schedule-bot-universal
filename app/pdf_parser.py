from __future__ import annotations

import re
from pathlib import Path
from typing import Any

import pdfplumber

PARSER_VERSION = 5
WEEKDAYS = [
    "понедельник",
    "вторник",
    "среда",
    "четверг",
    "пятница",
    "суббота",
]
PAIR_TIMES = {
    "8.00-9.30": 1,
    "9.40-11.10": 2,
    "11.20-12.50": 3,
    "13.20-14.50": 4,
    "15.00-16.30": 5,
    "16.40-18.10": 6,
    "18.20-19.50": 7,
}
PAIR_TIMES_DISPLAY = {
    pair: value.replace(".", ":") for value, pair in PAIR_TIMES.items()
}
TABLE_SETTINGS = {
    "vertical_strategy": "lines",
    # Background rectangles can split a merged time cell at the week boundary.
    "horizontal_strategy": "lines_strict",
    "snap_tolerance": 3,
    "join_tolerance": 3,
    "intersection_tolerance": 3,
}


def normalize_group(value: str) -> str:
    text = re.sub(r"\s+", " ", value.strip().casefold())
    match = re.match(r"^(\d{2})\s*(.*)$", text)
    if not match:
        return text
    suffix = match.group(2).strip()
    return f"{match.group(1)} {suffix}".strip()


def _collapse_overprinted(value: str) -> str:
    """Fix Excel/PDF text occasionally extracted as 'ШШиишшккиинн'."""
    text = value.strip()
    compact = re.sub(r"\s+", "", text)
    if len(compact) < 6:
        return text
    pairs = [compact[index : index + 2] for index in range(0, len(compact) - 1, 2)]
    if (
        pairs
        and sum(len(pair) == 2 and pair[0] == pair[1] for pair in pairs) / len(pairs)
        > 0.7
    ):
        return re.sub(r"(.)\1", r"\1", text)
    return text


def _clean_cell(value: str | None) -> str:
    if not value:
        return ""
    lines = []
    for raw_line in value.replace("\r", "\n").split("\n"):
        line = re.sub(r"\s+", " ", _collapse_overprinted(raw_line)).strip()
        if line:
            lines.append(line)
    return "\n".join(lines)


def _normalized_time(value: str | None) -> str:
    text = re.sub(r"\s+", "", value or "")
    for copies in (3, 2):
        if len(text) % copies == 0 and all(
            len(set(text[index : index + copies])) == 1
            for index in range(0, len(text), copies)
        ):
            text = "".join(text[index] for index in range(0, len(text), copies))
            break
    return text


def _lesson_from_raw(
    raw: str, pair: int, parse_warning: str | None = None
) -> dict[str, Any]:
    lines = [line.strip() for line in raw.splitlines() if line.strip()]
    teacher_end = 1
    if len(lines) >= 4 and re.fullmatch(r"[А-ЯЁA-Z]\.[А-ЯЁA-Z]\.?,?", lines[1]):
        teacher_end = 2
    teacher = " ".join(lines[:teacher_end]) if lines else ""
    room = lines[-1] if len(lines) >= 3 and _looks_like_room(lines[-1]) else ""
    subject_end = -1 if room else len(lines)
    subject = " ".join(lines[teacher_end:subject_end])
    lesson = {
        "pair": pair,
        "time": PAIR_TIMES_DISPLAY[pair],
        "teacher": teacher,
        "subject": subject,
        "room": room,
        "raw": raw,
    }
    if parse_warning:
        lesson["parse_warning"] = parse_warning
    return lesson


def _looks_like_teacher(line: str) -> bool:
    text = line.strip()
    if not re.match(r"^[А-ЯЁ][а-яё-]+", text):
        return False
    return bool(
        "/" in text
        or re.search(r"\b[А-ЯЁA-Z]\.?\s*[А-ЯЁA-Z](?:[.,])?$", text)
        or re.fullmatch(r"[А-ЯЁ][а-яё-]+(?:\s*/\s*[А-ЯЁ][а-яё-]+)+", text)
        or re.fullmatch(r"[А-ЯЁ][а-яё-]+", text)
    )


def _looks_like_room(line: str) -> bool:
    return bool(
        re.search(
            r"каб|этаж|лектор|[см]?порт\.?\s*\.?(?:з+)?ал|при?т?строй|притрой|мастер|ков\.?\s*мас|"
            r"полигон|архив|библиот|территор|актов|стадион|гараж|кор\.?\s*\d|"
            r"^\s*\d{2,4}[а-яё]?\s*$",
            line,
            flags=re.IGNORECASE,
        )
    )


def _is_complete_lesson(raw: str) -> bool:
    lines = [line for line in raw.splitlines() if line.strip()]
    return len(lines) >= 2 and _looks_like_teacher(lines[0])


def _find_group_header(
    table: list[list[str | None]],
) -> tuple[int, list[int], list[str]]:
    best: tuple[int, list[int], list[str]] | None = None
    for row_index, row in enumerate(table):
        positions: list[int] = []
        groups: list[str] = []
        for column, value in enumerate(row):
            cell = _clean_cell(value)
            if re.match(r"^\d{2}\s*[а-яё]", cell, flags=re.IGNORECASE):
                positions.append(column)
                groups.append(normalize_group(cell))
        if best is None or len(groups) > len(best[2]):
            best = (row_index, positions, groups)
    if best is None or len(best[2]) < 4:
        raise ValueError("Could not find group columns in PDF table")
    return best


def _header_rows(table: list[list[str | None]], group_columns: list[int]) -> list[int]:
    result: list[int] = []
    threshold = max(4, len(group_columns) // 2)
    for index, row in enumerate(table):
        matches = 0
        for column in group_columns:
            if column < len(row) and re.match(
                r"^\d{2}\s*[а-яё]", _clean_cell(row[column]), flags=re.IGNORECASE
            ):
                matches += 1
        if matches >= threshold:
            result.append(index)
    return result


def _repair_pair_boundaries(fragments: dict[int, list[str]]) -> None:
    """Move lesson text that begins before the matching time cell in the PDF grid."""
    for pair in range(1, 7):
        current = fragments[pair]
        following = fragments[pair + 1]

        while sum(_looks_like_teacher(value.splitlines()[0]) for value in current) > 2:
            moved = current.pop()
            if following:
                first_lines = following[0].splitlines()
                continuation = not _looks_like_teacher(first_lines[0]) or (
                    len(first_lines) <= 2 and _looks_like_room(first_lines[-1])
                )
                moved_lines = moved.splitlines()
                if len(moved_lines) == 1 or (
                    continuation and not _looks_like_room(moved_lines[-1])
                ):
                    following[0] = f"{moved}\n{following[0]}"
                    continue
            following.insert(0, moved)

        if not current or not following:
            continue
        last_lines = current[-1].splitlines()
        first_lines = following[0].splitlines()
        continuation = not _looks_like_teacher(first_lines[0]) or (
            len(first_lines) <= 2 and _looks_like_room(first_lines[-1])
        )
        if len(last_lines) == 1 or (
            continuation and not _looks_like_room(last_lines[-1])
        ):
            following[0] = f"{current.pop()}\n{following[0]}"


def parse_schedule_pdf(path: Path, course: int | None = None) -> dict[str, Any]:
    with pdfplumber.open(path) as document:
        tables: list[list[list[str | None]]] = []
        for page in document.pages:
            tables.extend(page.extract_tables(TABLE_SETTINGS))
    if not tables:
        raise ValueError(f"No tables found in {path.name}")
    table = max(
        tables,
        key=lambda value: len(value) * max((len(row) for row in value), default=0),
    )

    _, group_columns, groups = _find_group_header(table)
    header_rows = _header_rows(table, group_columns)
    if len(header_rows) not in (5, 6):
        raise ValueError(
            f"Expected five or six weekday blocks in {path.name}, "
            f"found {len(header_rows)}"
        )

    result: dict[str, Any] = {
        group: {
            "course": course or int(group[:1]),
            "warnings": [],
            "days": {
                weekday: {"числитель": [], "знаменатель": []} for weekday in WEEKDAYS
            },
        }
        for group in groups
    }

    for day_index, header_row in enumerate(header_rows):
        block_end = (
            header_rows[day_index + 1]
            if day_index + 1 < len(header_rows)
            else len(table)
        )
        pair_starts: list[tuple[int, int]] = []
        for row_index in range(header_row + 1, block_end):
            row = table[row_index]
            time_value = _normalized_time(row[2] if len(row) > 2 else None)
            pair = PAIR_TIMES.get(time_value)
            if pair:
                pair_starts.append((row_index, pair))
        if [pair for _, pair in pair_starts] != list(range(1, 8)):
            raise ValueError(
                f"Unexpected pair rows in {path.name}, {WEEKDAYS[day_index]}: {pair_starts}"
            )

        pair_ranges = []
        for pair_index, (row_start, pair) in enumerate(pair_starts):
            row_end = (
                pair_starts[pair_index + 1][0] if pair_index + 1 < 7 else block_end
            )
            pair_ranges.append((pair, row_start, row_end))

        for group, column in zip(groups, group_columns):
            variants_by_pair: dict[int, list[str]] = {pair: [] for pair in range(1, 8)}
            warning_values: set[str] = set()
            fragments_by_pair: dict[int, list[str]] = {}
            for pair, row_start, row_end in pair_ranges:
                fragments_by_pair[pair] = [
                    raw
                    for row_index in range(row_start, row_end)
                    if (
                        raw := _clean_cell(
                            table[row_index][column]
                            if column < len(table[row_index])
                            else None
                        )
                    )
                ]
            _repair_pair_boundaries(fragments_by_pair)

            for pair in range(1, 8):
                pending_fragment = ""
                for raw in fragments_by_pair[pair]:
                    if _is_complete_lesson(raw):
                        if raw not in variants_by_pair[pair]:
                            variants_by_pair[pair].append(raw)
                        continue

                    variants = variants_by_pair[pair]
                    if (
                        variants
                        and _looks_like_room(raw)
                        and not _looks_like_room(variants[-1].splitlines()[-1])
                    ):
                        variants[-1] = f"{variants[-1]}\n{raw}"
                        continue

                    candidate = (
                        f"{pending_fragment}\n{raw}".strip()
                        if pending_fragment
                        else raw
                    )
                    if _is_complete_lesson(candidate):
                        if candidate not in variants_by_pair[pair]:
                            variants_by_pair[pair].append(candidate)
                        pending_fragment = ""
                    else:
                        pending_fragment = candidate

                if pending_fragment:
                    # Never silently lose source text. An unusual teacher spelling is
                    # safer to expose as a warning than to remove from the timetable.
                    if pending_fragment not in variants_by_pair[pair]:
                        variants_by_pair[pair].append(pending_fragment)
                    warning_values.add(pending_fragment)
                    result[group]["warnings"].append(
                        {
                            "weekday": WEEKDAYS[day_index],
                            "pair": pair,
                            "raw": pending_fragment,
                            "reason": "unresolved_pdf_fragment",
                        }
                    )

            for pair in range(1, 8):
                variants = variants_by_pair[pair]
                if len(variants) > 2:
                    raise ValueError(
                        f"More than two week variants in {path.name}: "
                        f"{group}, {WEEKDAYS[day_index]}, pair {pair}"
                    )

                numerator = variants[0] if variants else ""
                denominator = variants[1] if len(variants) > 1 else numerator
                weekday = WEEKDAYS[day_index]
                if numerator:
                    result[group]["days"][weekday]["числитель"].append(
                        _lesson_from_raw(
                            numerator,
                            pair,
                            "unresolved_pdf_fragment"
                            if numerator in warning_values
                            else None,
                        )
                    )
                if denominator:
                    result[group]["days"][weekday]["знаменатель"].append(
                        _lesson_from_raw(
                            denominator,
                            pair,
                            "unresolved_pdf_fragment"
                            if denominator in warning_values
                            else None,
                        )
                    )
    return result
