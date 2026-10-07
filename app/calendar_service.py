from __future__ import annotations

import datetime as dt
import hashlib
import json
import logging
import re
import threading
import time
from collections.abc import Callable
from http.server import BaseHTTPRequestHandler, HTTPServer
from typing import Any
from urllib.parse import urlsplit

from .config import Config
from .pdf_parser import PAIR_TIMES_DISPLAY
from .replacement_service import ReplacementRepository
from .schedule_calculator import schedule_for_target
from .schedule_service import ScheduleRepository
from .storage import Storage
from .teacher_schedule import correct_teacher_name, teacher_names

logger = logging.getLogger(__name__)


def _text(value: str) -> str:
    return (
        value.replace("\\", "\\\\")
        .replace("\r\n", "\n")
        .replace("\r", "\n")
        .replace("\n", "\\n")
        .replace(";", "\\;")
        .replace(",", "\\,")
    )


def _fold(line: str) -> str:
    """RFC 5545: fold at 75 UTF-8 octets without splitting a character."""
    parts: list[str] = []
    current = ""
    size = 0
    for char in line:
        length = len(char.encode("utf-8"))
        if size + length > 75:
            parts.append(current)
            current, size = " ", 1
        current += char
        size += length
    parts.append(current)
    return "\r\n".join(parts)


def calendar_events(
    target_type: str, name: str, schedule: dict[str, Any], timezone: dt.tzinfo
) -> list[dict[str, str]]:
    events = []
    date = dt.date.fromisoformat(schedule["date"])
    for lesson in schedule["pairs"]:
        if lesson.get("status") == "cancelled":
            continue
        pair = int(lesson["pair"])
        group = str(lesson.get("group", schedule.get("group", "")))
        identity = json.dumps([target_type, name, date.isoformat(), pair, group])
        uid = hashlib.sha256(identity.encode()).hexdigest() + "@schedule-bot"
        start, end = PAIR_TIMES_DISPLAY[pair].split("-")

        def utc_time(value: str) -> str:
            hour, minute = map(int, value.split(":"))
            local = dt.datetime.combine(date, dt.time(hour, minute), timezone)
            return local.astimezone(dt.timezone.utc).strftime("%Y%m%dT%H%M%SZ")

        subject = str(lesson.get("subject") or lesson.get("raw") or "Пара")
        summary = f"{pair}. {subject}"
        if target_type == "teacher":
            summary += f" · {group.upper()}"
        description = (
            f"Группа: {group.upper()}\n"
            f"Преподаватель: {correct_teacher_name(str(lesson.get('teacher', '')))}"
        )
        if lesson.get("status") in {"replaced", "replacement_only"}:
            description += "\nС учётом замены"
        if lesson.get("parse_warning"):
            description += (
                "\nПроверьте исходное расписание: есть предупреждение разбора"
            )
        events.append(
            {
                "uid": uid,
                "start": utc_time(start),
                "end": utc_time(end),
                "summary": summary,
                "description": description,
                "location": str(lesson.get("room", "")),
                "status": "CONFIRMED",
            }
        )
    return events


def _short_title(event: dict[str, str], name: str, target_type: str) -> str:
    teacher = (
        name
        if target_type == "teacher"
        else (event["description"].partition("Преподаватель: ")[2].split("\n", 1)[0])
    )
    surnames = dict.fromkeys(person.split()[0] for person in teacher_names(teacher))
    return "/".join(surnames) or "Пара"


def render_calendar(
    name: str,
    days: list[Any],
    *,
    target_type: str = "group",
    title_format: str = "full",
    revision: int = 0,
    updated_at: str = "",
) -> bytes:
    lines = [
        "BEGIN:VCALENDAR",
        "VERSION:2.0",
        "PRODID:-//Schedule Bot//Учебное расписание//RU",
        "CALSCALE:GREGORIAN",
        "X-WR-CALNAME:" + _text("Расписание · " + name),
        "X-PUBLISHED-TTL:PT1H",
    ]
    for day in days:
        for event in json.loads(day["events"]):
            short = title_format == "short"
            summary = (
                _short_title(event, name, target_type) if short else event["summary"]
            )
            stamp = max(day["updated_at"], updated_at)
            lines.extend(
                [
                    "BEGIN:VEVENT",
                    "UID:" + event["uid"],
                    "DTSTAMP:" + stamp,
                    "LAST-MODIFIED:" + stamp,
                    "SEQUENCE:" + str(day["sequence"] + revision),
                    "DTSTART:" + event["start"],
                    "DTEND:" + event["end"],
                    "SUMMARY:" + _text(summary),
                ]
            )
            if not short:
                lines.append("DESCRIPTION:" + _text(event["description"]))
            lines.extend(
                [
                    "LOCATION:" + _text(event["location"]),
                    "STATUS:" + event["status"],
                    "END:VEVENT",
                ]
            )
    lines.append("END:VCALENDAR")
    return ("\r\n".join(_fold(line) for line in lines) + "\r\n").encode("utf-8")


class CalendarService:
    def __init__(
        self,
        config: Config,
        storage: Storage,
        schedules: ScheduleRepository,
        replacements: ReplacementRepository,
        timezone: dt.tzinfo,
        validate_semester: Callable[[], None],
    ) -> None:
        self.config = config
        self.storage = storage
        self.schedules = schedules
        self.replacements = replacements
        self.timezone = timezone
        self.validate_semester = validate_semester
        self._stop = threading.Event()
        self._server: HTTPServer | None = None
        self._http_thread: threading.Thread | None = None
        self._publisher_thread: threading.Thread | None = None

    def run(self, now: dt.datetime) -> None:
        now = now.astimezone(self.timezone)
        targets = self.storage.calendar_targets()
        if not targets:
            return
        self.validate_semester()
        schedules = self.schedules.snapshot()
        dates = [now.date(), now.date() + dt.timedelta(days=1)]
        for date in dates:
            if self._stop.is_set():
                return
            if date.weekday() == 6:
                continue
            try:
                item = self.replacements.find_for_date(date)
                if item is None:
                    continue
                by_group = self.replacements.replacements_for_item(item)
            except Exception:
                # An unavailable source is not the same as an absent replacement file.
                logger.exception("Calendar replacements unavailable for %s", date)
                continue
            for target in targets:
                if self._stop.is_set():
                    return
                kind, name = target["target_type"], target["target_name"]
                try:
                    schedule = schedule_for_target(
                        schedules,
                        kind,
                        name,
                        date,
                        self.config.numerator_week_start,
                        by_group,
                    )
                    events = calendar_events(kind, name, schedule, self.timezone)
                    self.storage.save_calendar_day(kind, name, date, events, now)
                except Exception:
                    logger.exception(
                        "Calendar publication failed for %s %s", kind, name
                    )
        self.storage.cleanup_calendar_days(now.date() - dt.timedelta(days=30))

    def feed(self, token: str, now: dt.datetime) -> bytes | None:
        now = now.astimezone(self.timezone)
        last_date = now.date() + dt.timedelta(days=1)
        data = self.storage.calendar_feed_data(
            token, now.date() - dt.timedelta(days=30), last_date
        )
        if data is None:
            return None
        subscription, days = data
        return render_calendar(
            subscription["target_name"],
            days,
            target_type=subscription["target_type"],
            title_format=subscription["title_format"],
            revision=subscription["title_revision"],
            updated_at=subscription["title_updated_at"],
        )

    def start(self) -> None:
        if (
            self._stop.is_set()
            or not self.config.calendar_public_url
            or self._server is not None
        ):
            return
        service = self

        class Handler(BaseHTTPRequestHandler):
            def setup(self) -> None:
                self.request.settimeout(5)
                super().setup()

            def do_GET(self) -> None:
                self._respond()

            def do_HEAD(self) -> None:
                self._respond()

            def _respond(self) -> None:
                match = re.fullmatch(
                    r"/calendar/([0-9a-f]{48})\.ics", urlsplit(self.path).path
                )
                if not match:
                    self.send_error(404)
                    return
                try:
                    content = service.feed(match[1], dt.datetime.now(service.timezone))
                except Exception:
                    logger.exception("Could not read calendar feed")
                    self.send_error(503)
                    return
                if content is None:
                    self.send_error(404)
                    return
                etag = '"' + hashlib.sha256(content).hexdigest() + '"'
                unchanged = self.headers.get("If-None-Match") == etag
                self.send_response(304 if unchanged else 200)
                self.send_header("ETag", etag)
                self.send_header("Cache-Control", "private, max-age=60")
                if not unchanged:
                    self.send_header("Content-Type", "text/calendar; charset=utf-8")
                    self.send_header("Content-Length", str(len(content)))
                self.end_headers()
                if self.command != "HEAD" and not unchanged:
                    self.wfile.write(content)

            def log_message(self, _format: str, *args: Any) -> None:
                # Subscription URLs are access tokens; do not put them in logs.
                pass

        # Loopback only. Public HTTPS, request limits and buffering belong to the proxy.
        self._server = HTTPServer(("127.0.0.1", self.config.calendar_port), Handler)
        self._http_thread = threading.Thread(
            target=self._server.serve_forever, name="calendar-http", daemon=True
        )
        self._http_thread.start()
        self._publisher_thread = threading.Thread(
            target=self._publish_loop, name="calendar-publisher", daemon=True
        )
        self._publisher_thread.start()
        logger.info(
            "Calendar endpoint listening on 127.0.0.1:%s", self._server.server_port
        )

    def _publish_loop(self) -> None:
        last_window = None
        next_check = 0.0
        while not self._stop.is_set():
            try:
                now = dt.datetime.now(self.timezone)
                targets = self.storage.calendar_targets()
                window = (
                    now.date(),
                    tuple((row["target_type"], row["target_name"]) for row in targets),
                )
                # Check new subscriptions every 30 seconds;
                # poll external replacement sources only at their configured interval.
                if window != last_window or time.monotonic() >= next_check:
                    self.run(now)
                    last_window = window
                    next_check = (
                        time.monotonic() + self.config.replacement_check_minutes * 60
                    )
            except Exception:
                logger.exception("Calendar background task failed")
            if self._stop.wait(30):
                break

    def close(self) -> None:
        self._stop.set()
        if self._server:
            if self._http_thread and self._http_thread.is_alive():
                self._server.shutdown()
            self._server.server_close()
        for thread in (self._http_thread, self._publisher_thread):
            if thread and thread.ident is not None:
                thread.join()
        self._server = None
