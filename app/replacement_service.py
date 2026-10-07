from __future__ import annotations

import copy
import datetime as dt
import io
import logging
import re
import threading
import zipfile
from collections import OrderedDict
from typing import Any

from defusedxml import ElementTree as ET

from .pdf_parser import PAIR_TIMES_DISPLAY, normalize_group
from .yandex_disk import download_public_file, file_fingerprint, list_public_files

W_NS = {"w": "http://schemas.openxmlformats.org/wordprocessingml/2006/main"}
ROMAN_PAIRS = {"I": 1, "II": 2, "III": 3, "IV": 4, "V": 5, "VI": 6, "VII": 7}
MAX_DOCX_ENTRIES = 2048
MAX_DOCX_UNCOMPRESSED_BYTES = 100 * 1024 * 1024
MAX_DOCUMENT_XML_BYTES = 20 * 1024 * 1024
logger = logging.getLogger(__name__)


def _node_text(node: ET.Element) -> str:
    return "".join(item.text or "" for item in node.findall(".//w:t", W_NS)).strip()


def _docx_root(content: bytes) -> ET.Element:
    with zipfile.ZipFile(io.BytesIO(content), "r") as archive:
        entries = archive.infolist()
        if len(entries) > MAX_DOCX_ENTRIES:
            raise ValueError("DOCX contains too many archive entries")
        if sum(entry.file_size for entry in entries) > MAX_DOCX_UNCOMPRESSED_BYTES:
            raise ValueError("DOCX is too large after decompression")
        try:
            document = archive.getinfo("word/document.xml")
        except KeyError:
            raise ValueError("DOCX does not contain word/document.xml") from None
        if document.file_size > MAX_DOCUMENT_XML_BYTES:
            raise ValueError("DOCX document.xml is too large")
        xml_content = archive.read(document)
    upper_xml = xml_content.upper()
    if b"<!DOCTYPE" in upper_xml or b"<!ENTITY" in upper_xml:
        raise ValueError("DOCX contains forbidden XML declarations")
    return ET.fromstring(xml_content)


def parse_replacements_all(content: bytes) -> dict[str, list[dict[str, str]]]:
    root = _docx_root(content)
    result: dict[str, list[dict[str, str]]] = {}
    for table in root.findall(".//w:tbl", W_NS):
        for row in table.findall("./w:tr", W_NS):
            cells = [_node_text(cell) for cell in row.findall("./w:tc", W_NS)]
            if len(cells) < 5:
                continue
            pair, group, old_value, new_value, room = [
                value.strip() for value in cells[:5]
            ]
            normalized_group = normalize_group(group)
            if not re.match(r"^\d{2}\s*[а-яё]", normalized_group, flags=re.IGNORECASE):
                continue
            result.setdefault(normalized_group, []).append(
                {
                    "pair": pair,
                    "group": group,
                    "from": old_value,
                    "to": new_value,
                    "room": room,
                }
            )
    return result


def _pair_number(value: str) -> int | None:
    compact = re.sub(r"[^IVX0-9]", "", value.upper())
    if compact.isdigit():
        number = int(compact)
        return number if 1 <= number <= 7 else None
    return ROMAN_PAIRS.get(compact)


def _replacement_lesson(value: str) -> tuple[str, str]:
    text = re.sub(r"\s+", " ", value).strip()
    match = re.match(r"^(.*?)\s*\((.+)\)\s*$", text)
    if not match:
        return "", text
    return match.group(1).strip(), match.group(2).strip()


class ReplacementConflictError(ValueError):
    """Keep ambiguous source rows available instead of choosing one silently."""

    def __init__(self, group: str, pair: int, rows: list[dict[str, str]]):
        self.rows = copy.deepcopy(rows)
        super().__init__(
            f"В файле замен несколько разных записей для группы {group}, пары {pair}. "
            "Проверьте исходный DOCX: однозначно применить замены нельзя."
        )


def _replacements_by_pair(
    replacements: list[dict[str, str]], group: str
) -> dict[int, dict[str, str]]:
    by_pair: dict[int, dict[str, str]] = {}
    for item in replacements:
        pair = _pair_number(item.get("pair", ""))
        if pair is None:
            continue
        previous = by_pair.get(pair)
        if previous is not None and any(
            previous.get(field, "").strip() != item.get(field, "").strip()
            for field in ("from", "to", "room")
        ):
            rows = [
                row for row in replacements if _pair_number(row.get("pair", "")) == pair
            ]
            raise ReplacementConflictError(group, pair, rows)
        by_pair.setdefault(pair, item)
    return by_pair


def apply_replacements(
    schedule: dict[str, Any], replacements: list[dict[str, str]]
) -> dict[str, Any]:
    result = copy.deepcopy(schedule)
    by_pair = _replacements_by_pair(replacements, str(schedule.get("group", "?")))
    found: set[int] = set()
    for lesson in result["pairs"]:
        pair = int(lesson["pair"])
        replacement = by_pair.get(pair)
        if not replacement:
            continue
        found.add(pair)
        new_value = replacement["to"].strip()
        lesson["replacement"] = replacement
        if new_value.casefold() == "нет":
            lesson["status"] = "cancelled"
            continue
        teacher, subject = _replacement_lesson(new_value)
        lesson.update(
            {
                "teacher": teacher,
                "subject": subject,
                "room": replacement["room"]
                if replacement["room"] != "-"
                else lesson.get("room", ""),
                "raw": new_value,
                "status": "replaced",
            }
        )
        lesson.pop("parse_warning", None)
    for pair, replacement in by_pair.items():
        if pair in found or replacement["to"].strip().casefold() == "нет":
            continue
        teacher, subject = _replacement_lesson(replacement["to"])
        result["pairs"].append(
            {
                "pair": pair,
                "time": PAIR_TIMES_DISPLAY[pair],
                "teacher": teacher,
                "subject": subject,
                "room": replacement["room"],
                "raw": replacement["to"],
                "status": "replacement_only",
                "replacement": replacement,
            }
        )
    result["pairs"].sort(key=lambda item: int(item["pair"]))
    result["replacements_count"] = len(replacements)
    if replacements:
        result["source"] = "pdf+docx"
    return result


class ReplacementRepository:
    def __init__(self, public_url: str, cache_size: int = 16):
        self.public_url = public_url
        self._files: list[dict[str, Any]] = []
        self._listed_at: dt.datetime | None = None
        self._documents: OrderedDict[str, bytes] = OrderedDict()
        self._parsed_documents: OrderedDict[str, dict[str, list[dict[str, str]]]] = (
            OrderedDict()
        )
        self._cache_size = max(cache_size, 1)
        # ponytail: one lock prevents duplicate downloads; split by file if latency grows.
        self._lock = threading.RLock()

    def _remember(self, cache: OrderedDict[str, Any], key: str, value: Any) -> Any:
        cache[key] = value
        cache.move_to_end(key)
        while len(cache) > self._cache_size:
            cache.popitem(last=False)
        return value

    def refresh_files(self, max_age_seconds: int = 60) -> list[dict[str, Any]]:
        with self._lock:
            now = dt.datetime.now(dt.timezone.utc)
            if (
                self._listed_at
                and (now - self._listed_at).total_seconds() < max_age_seconds
            ):
                return list(self._files)
            self._files = list_public_files(self.public_url)
            self._listed_at = now
            return list(self._files)

    def find_for_date(self, target_date: dt.date) -> dict[str, Any] | None:
        date_token = target_date.strftime("%d.%m.%Y")
        matches = [
            item
            for item in self.refresh_files()
            if date_token in str(item.get("name", ""))
            and str(item.get("name", "")).casefold().endswith(".docx")
        ]
        return max(
            matches, key=lambda item: str(item.get("modified", "")), default=None
        )

    def document(self, item: dict[str, Any]) -> bytes:
        fingerprint = file_fingerprint(item)
        with self._lock:
            existing = self._documents.get(fingerprint)
            if existing is not None:
                self._documents.move_to_end(fingerprint)
                return existing
            content = download_public_file(self.public_url, item)
            return self._remember(self._documents, fingerprint, content)

    def replacements_for_item(
        self, item: dict[str, Any]
    ) -> dict[str, list[dict[str, str]]]:
        fingerprint = file_fingerprint(item)
        with self._lock:
            existing = self._parsed_documents.get(fingerprint)
            if existing is not None:
                self._parsed_documents.move_to_end(fingerprint)
                return existing
            parsed = parse_replacements_all(self.document(item))
            return self._remember(self._parsed_documents, fingerprint, parsed)

    def recent_teachers(self, limit: int = 7) -> list[str]:
        files = sorted(
            (
                item
                for item in self.refresh_files()
                if str(item.get("name", "")).casefold().endswith(".docx")
            ),
            key=lambda item: str(item.get("modified", "")),
            reverse=True,
        )[:limit]
        names: list[str] = []
        for item in files:
            try:
                groups = self.replacements_for_item(item)
            except Exception:
                logger.exception(
                    "Could not inspect replacement teachers in %s", item.get("name")
                )
                continue
            for rows in groups.values():
                for row in rows:
                    for field in ("from", "to"):
                        teacher, _ = _replacement_lesson(row[field])
                        if teacher:
                            names.append(teacher)
        return names

    def apply_for_date(
        self, schedule: dict[str, Any], target_date: dt.date
    ) -> tuple[dict[str, Any], str | None]:
        item = self.find_for_date(target_date)
        if not item:
            return schedule, None
        replacements = self.replacements_for_item(item).get(
            normalize_group(schedule["group"]), []
        )
        return apply_replacements(schedule, replacements), file_fingerprint(item)
