from __future__ import annotations

import datetime as dt
import hashlib
import json
import logging
from collections.abc import Callable
from typing import Any

from .config import Config
from .replacement_service import ReplacementRepository
from .schedule_calculator import schedule_for_target
from .schedule_formatter import format_schedule, format_teacher_schedule
from .schedule_service import ScheduleRepository
from .storage import Storage
from .telegram_api import TelegramAPIError
from .telegram_queue import TelegramSendQueue
from .yandex_disk import file_fingerprint

logger = logging.getLogger(__name__)


def _legacy_group_fingerprint(replacements: list[dict[str, str]]) -> str:
    content = json.dumps(
        replacements, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    )
    return "group:" + hashlib.sha256(content.encode("utf-8")).hexdigest()


def _schedule_fingerprint(message: str) -> str:
    return "schedule:" + hashlib.sha256(message.encode("utf-8")).hexdigest()


class AutopostService:
    def __init__(
        self,
        config: Config,
        storage: Storage,
        schedules: ScheduleRepository,
        replacements: ReplacementRepository,
        sender: TelegramSendQueue,
        validate_semester: Callable[[], None],
    ) -> None:
        self.config = config
        self.storage = storage
        self.schedules = schedules
        self.replacements = replacements
        self.sender = sender
        self.validate_semester = validate_semester

    def run(self, now: dt.datetime) -> None:
        self.validate_semester()
        bindings = self.storage.autopost_bindings()
        if not bindings:
            return

        target_date = now.date() + dt.timedelta(days=1)
        if target_date.weekday() == 6:
            return
        replacement_item = self.replacements.find_for_date(target_date)
        if replacement_item is None:
            return

        replacements_by_group = self.replacements.replacements_for_item(
            replacement_item
        )
        legacy_fingerprint = "replacement:" + file_fingerprint(replacement_item)
        snapshot = self.schedules.snapshot()
        messages: dict[tuple[str, str], str | None] = {}
        for binding in bindings:
            kind, name = binding["target_type"], binding["target_name"]
            key = (kind, name)
            if key not in messages:
                try:
                    schedule = schedule_for_target(
                        snapshot,
                        kind,
                        name,
                        target_date,
                        self.config.numerator_week_start,
                        replacements_by_group,
                    )
                    messages[key] = (
                        format_teacher_schedule(schedule)
                        if kind == "teacher"
                        else format_schedule(schedule)
                    )
                except Exception:
                    logger.exception(
                        "Autopost calculation failed for %s %s", kind, name
                    )
                    messages[key] = None
            message = messages[key]
            if message is not None:
                self._send_binding(
                    binding,
                    target_date,
                    message,
                    replacements_by_group.get(name, []),
                    legacy_fingerprint,
                )

    def _send_binding(
        self,
        binding: Any,
        target_date: dt.date,
        message: str,
        group_replacements: list[dict[str, str]],
        legacy_fingerprint: str,
    ) -> None:
        chat_id = int(binding["chat_id"])
        thread_id = int(binding["thread_id"])
        target_type = str(binding["target_type"])
        name = str(binding["target_name"])
        try:
            fingerprint = _schedule_fingerprint(message)
            old_fingerprints = (
                {
                    _legacy_group_fingerprint(group_replacements),
                    legacy_fingerprint,
                }
                if target_type == "group"
                else set()
            )
            date_key = target_date.isoformat()
            previous = self.storage.autopost_fingerprint(
                chat_id, thread_id, name, date_key, target_type
            )
            if previous == fingerprint or previous in old_fingerprints:
                if previous != fingerprint:
                    self.storage.mark_autopost(
                        chat_id, thread_id, name, date_key, fingerprint, target_type
                    )
                if binding["autopost_failures"] or binding["autopost_last_error"]:
                    self.storage.record_autopost_success(chat_id, thread_id)
                return
            self.sender.send_message(
                chat_id,
                message,
                thread_id or None,
                priority=10,
            )
            self.storage.mark_autopost(
                chat_id, thread_id, name, date_key, fingerprint, target_type
            )
            if binding["autopost_failures"] or binding["autopost_last_error"]:
                self.storage.record_autopost_success(chat_id, thread_id)
            logger.info(
                "Autopost sent chat_id=%s thread_id=%s target=%s:%s date=%s",
                chat_id,
                thread_id,
                target_type,
                name,
                date_key,
            )
        except Exception as error:
            disabled = False
            if isinstance(error, TelegramAPIError) and error.delivery_unavailable:
                disabled = self.storage.record_autopost_failure(
                    chat_id, thread_id, str(error)
                )
            logger.exception(
                "Autopost failed chat_id=%s thread_id=%s target=%s:%s disabled=%s",
                chat_id,
                thread_id,
                target_type,
                name,
                disabled,
            )
