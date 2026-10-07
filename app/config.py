from __future__ import annotations

import datetime as dt
import os
import re
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlsplit

DEFAULT_SCHEDULE_URL = "https://disk.yandex.ru/d/tF4sAFicQhzBdA"
DEFAULT_REPLACEMENTS_URL = "https://disk.yandex.ru/d/F_GFm6_Qi9GYAQ"


def _load_env_file(path: Path = Path(".env")) -> None:
    if not path.exists():
        return
    for raw_line in path.read_text(encoding="utf-8-sig").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


def _env_bool(name: str, default: bool) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip().casefold() in {"1", "true", "yes", "on", "да"}


@dataclass(frozen=True)
class Config:
    token: str
    schedule_url: str
    replacements_url: str
    data_dir: Path
    timezone: str
    numerator_week_start: dt.date
    refresh_interval_minutes: int
    replacement_check_minutes: int
    autopost_enabled: bool
    worker_count: int
    expected_semester: str | None
    log_level: str = "INFO"
    telegram_messages_per_second: float = 20.0
    telegram_queue_size: int = 1000
    calendar_public_url: str = ""
    calendar_port: int = 8765
    calendar_publish_time: dt.time = dt.time(22, 0)

    @classmethod
    def from_env(cls) -> Config:
        _load_env_file()
        token = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
        if not token:
            raise ValueError("Set TELEGRAM_BOT_TOKEN")

        week_start_raw = os.getenv("NUMERATOR_WEEK_START", "").strip()
        if not week_start_raw:
            raise ValueError(
                "Set NUMERATOR_WEEK_START to a Monday of a numerator week (YYYY-MM-DD)"
            )
        numerator_week_start = dt.date.fromisoformat(week_start_raw)
        if numerator_week_start.weekday() != 0:
            raise ValueError("NUMERATOR_WEEK_START must be a Monday")
        calendar_url = os.getenv("CALENDAR_PUBLIC_URL", "").strip().rstrip("/")
        parsed = urlsplit(calendar_url)
        if calendar_url and (
            parsed.scheme != "https"
            or not parsed.hostname
            or parsed.username is not None
            or parsed.password is not None
            or parsed.path
            or parsed.query
            or parsed.fragment
        ):
            raise ValueError(
                "CALENDAR_PUBLIC_URL must be an HTTPS origin without a path"
            )
        calendar_time = os.getenv("CALENDAR_PUBLISH_TIME", "22:00").strip()
        if not re.fullmatch(r"\d{2}:\d{2}", calendar_time):
            raise ValueError("CALENDAR_PUBLISH_TIME must be HH:MM")
        publish_time = dt.time.fromisoformat(calendar_time)
        calendar_port = int(os.getenv("CALENDAR_PORT", "8765"))
        if not 1 <= calendar_port <= 65535:
            raise ValueError("CALENDAR_PORT must be between 1 and 65535")
        return cls(
            token=token,
            schedule_url=os.getenv("SCHEDULE_YANDEX_URL", DEFAULT_SCHEDULE_URL).strip(),
            replacements_url=os.getenv(
                "REPLACEMENTS_YANDEX_URL", DEFAULT_REPLACEMENTS_URL
            ).strip(),
            data_dir=Path(os.getenv("DATA_DIR", "data")),
            timezone=os.getenv("BOT_TIMEZONE", "Europe/Saratov").strip(),
            numerator_week_start=numerator_week_start,
            refresh_interval_minutes=max(
                int(os.getenv("SCHEDULE_REFRESH_MINUTES", "60")), 5
            ),
            replacement_check_minutes=max(
                int(os.getenv("REPLACEMENT_CHECK_MINUTES", "10")), 1
            ),
            autopost_enabled=_env_bool("AUTOPOST_ENABLED", True),
            worker_count=max(2, min(int(os.getenv("BOT_WORKERS", "4")), 16)),
            expected_semester=os.getenv("EXPECTED_SEMESTER", "").strip() or None,
            log_level=os.getenv("LOG_LEVEL", "INFO").strip().upper(),
            calendar_public_url=calendar_url,
            calendar_port=calendar_port,
            calendar_publish_time=publish_time,
            telegram_messages_per_second=max(
                1.0,
                min(float(os.getenv("TELEGRAM_MESSAGES_PER_SECOND", "20")), 25.0),
            ),
            telegram_queue_size=max(
                100, min(int(os.getenv("TELEGRAM_QUEUE_SIZE", "1000")), 10000)
            ),
        )
