from __future__ import annotations

import datetime as dt
import json
import logging
import secrets
import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)


class Storage:
    def __init__(self, data_dir: Path):
        self.data_dir = data_dir
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.cache_path = self.data_dir / "schedule_cache.json"
        self.db_path = self.data_dir / "bot.sqlite3"
        self._init_database()

    @contextmanager
    def connect(self) -> Iterator[sqlite3.Connection]:
        connection = sqlite3.connect(self.db_path)
        connection.row_factory = sqlite3.Row
        try:
            with connection:
                yield connection
        finally:
            connection.close()

    def _init_database(self) -> None:
        with self.connect() as db:
            db.executescript(
                """
                CREATE TABLE IF NOT EXISTS bindings (
                    chat_id INTEGER NOT NULL,
                    thread_id INTEGER NOT NULL DEFAULT 0,
                    target_type TEXT NOT NULL DEFAULT 'group',
                    target_name TEXT NOT NULL,
                    autopost INTEGER NOT NULL DEFAULT 0,
                    autopost_failures INTEGER NOT NULL DEFAULT 0,
                    autopost_last_error TEXT,
                    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    PRIMARY KEY (chat_id, thread_id)
                );
                CREATE TABLE IF NOT EXISTS sent_autoposts (
                    chat_id INTEGER NOT NULL,
                    thread_id INTEGER NOT NULL DEFAULT 0,
                    target_type TEXT NOT NULL DEFAULT 'group',
                    target_name TEXT NOT NULL,
                    target_date TEXT NOT NULL,
                    fingerprint TEXT NOT NULL,
                    sent_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    PRIMARY KEY (chat_id, thread_id, target_type, target_name, target_date)
                );
                CREATE TABLE IF NOT EXISTS calendar_subscriptions (
                    token TEXT PRIMARY KEY,
                    chat_id INTEGER NOT NULL,
                    thread_id INTEGER NOT NULL,
                    target_type TEXT NOT NULL,
                    target_name TEXT NOT NULL,
                    user_id INTEGER NOT NULL DEFAULT 0,
                    UNIQUE(chat_id, thread_id, target_type, target_name, user_id)
                );
                CREATE TABLE IF NOT EXISTS calendar_preferences (
                    user_id INTEGER PRIMARY KEY,
                    title_format TEXT NOT NULL,
                    revision INTEGER NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS calendar_days (
                    target_type TEXT NOT NULL,
                    target_name TEXT NOT NULL,
                    target_date TEXT NOT NULL,
                    events TEXT NOT NULL,
                    sequence INTEGER NOT NULL,
                    updated_at TEXT NOT NULL,
                    PRIMARY KEY(target_type, target_name, target_date)
                );
                """
            )
            db.execute("BEGIN IMMEDIATE")
            self._migrate_database(db)

    def _migrate_database(self, db: sqlite3.Connection) -> None:
        self._ensure_column(
            db, "bindings", "autopost_failures", "INTEGER NOT NULL DEFAULT 0"
        )
        self._ensure_column(db, "bindings", "autopost_last_error", "TEXT")
        if "user_id" not in self._columns(db, "calendar_subscriptions"):
            db.execute("ALTER TABLE calendar_subscriptions RENAME TO old_calendars")
            db.execute("""
                CREATE TABLE calendar_subscriptions (
                    token TEXT PRIMARY KEY, chat_id INTEGER NOT NULL,
                    thread_id INTEGER NOT NULL, target_type TEXT NOT NULL,
                    target_name TEXT NOT NULL, user_id INTEGER NOT NULL DEFAULT 0,
                    UNIQUE(chat_id, thread_id, target_type, target_name, user_id)
                )
            """)
            # Existing shared URLs remain valid and keep their default long titles.
            db.execute("""
                INSERT INTO calendar_subscriptions
                SELECT token, chat_id, thread_id, target_type, target_name, 0
                FROM old_calendars
            """)
            db.execute("DROP TABLE old_calendars")
        if "group_name" in self._columns(db, "bindings"):
            db.execute("ALTER TABLE bindings RENAME TO old_bindings")
            db.execute("""
                CREATE TABLE bindings (
                    chat_id INTEGER NOT NULL, thread_id INTEGER NOT NULL DEFAULT 0,
                    target_type TEXT NOT NULL DEFAULT 'group', target_name TEXT NOT NULL,
                    autopost INTEGER NOT NULL DEFAULT 0,
                    autopost_failures INTEGER NOT NULL DEFAULT 0,
                    autopost_last_error TEXT,
                    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    PRIMARY KEY (chat_id, thread_id)
                )
            """)
            db.execute("""
                INSERT INTO bindings
                SELECT chat_id, thread_id, 'group', group_name, autopost,
                       autopost_failures, autopost_last_error, updated_at
                FROM old_bindings
            """)
            db.execute("DROP TABLE old_bindings")
        if "group_name" in self._columns(db, "sent_autoposts"):
            db.execute("ALTER TABLE sent_autoposts RENAME TO old_sent_autoposts")
            db.execute("""
                CREATE TABLE sent_autoposts (
                    chat_id INTEGER NOT NULL, thread_id INTEGER NOT NULL DEFAULT 0,
                    target_type TEXT NOT NULL DEFAULT 'group', target_name TEXT NOT NULL,
                    target_date TEXT NOT NULL, fingerprint TEXT NOT NULL,
                    sent_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    PRIMARY KEY (chat_id, thread_id, target_type, target_name, target_date)
                )
            """)
            db.execute("""
                INSERT INTO sent_autoposts
                SELECT chat_id, thread_id, 'group', group_name, target_date,
                       fingerprint, sent_at FROM old_sent_autoposts
            """)
            db.execute("DROP TABLE old_sent_autoposts")

        if db.execute("PRAGMA user_version").fetchone()[0] < 1:
            # Existing short subscriptions must refresh after the title/description change.
            db.execute(
                """
                UPDATE calendar_preferences SET revision=revision+1, updated_at=?
                WHERE title_format='short'
                """,
                (dt.datetime.now(dt.timezone.utc).strftime("%Y%m%dT%H%M%SZ"),),
            )
            db.execute("PRAGMA user_version=1")

        if db.execute("PRAGMA user_version").fetchone()[0] < 2:
            # Refresh locations for all formats, including legacy URLs and cancelled events.
            db.execute(
                "UPDATE calendar_days SET sequence=sequence+1, updated_at=?",
                (dt.datetime.now(dt.timezone.utc).strftime("%Y%m%dT%H%M%SZ"),),
            )
            db.execute("PRAGMA user_version=2")

    @staticmethod
    def _columns(db: sqlite3.Connection, table: str) -> set[str]:
        return {str(row[1]) for row in db.execute(f"PRAGMA table_info({table})")}

    @staticmethod
    def _ensure_column(
        db: sqlite3.Connection, table: str, column: str, declaration: str
    ) -> None:
        columns = Storage._columns(db, table)
        if column not in columns:
            db.execute(f"ALTER TABLE {table} ADD COLUMN {column} {declaration}")

    def load_cache(self) -> dict[str, Any] | None:
        if not self.cache_path.exists():
            return None
        try:
            with self.cache_path.open("r", encoding="utf-8") as file:
                value = json.load(file)
            return value if isinstance(value, dict) else None
        except (OSError, json.JSONDecodeError):
            logger.warning("Ignoring unreadable schedule cache at %s", self.cache_path)
            return None

    def save_cache(self, cache: dict[str, Any]) -> None:
        temporary = self.cache_path.with_suffix(".tmp")
        try:
            with temporary.open("w", encoding="utf-8") as file:
                json.dump(cache, file, ensure_ascii=False, indent=2)
            temporary.replace(self.cache_path)
        finally:
            temporary.unlink(missing_ok=True)

    def set_binding(
        self, chat_id: int, thread_id: int | None, name: str, target_type: str = "group"
    ) -> None:
        with self.connect() as db:
            db.execute(
                """
                INSERT INTO bindings(chat_id, thread_id, target_type, target_name)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(chat_id, thread_id) DO UPDATE SET
                    target_type=excluded.target_type,
                    target_name=excluded.target_name,
                    updated_at=CURRENT_TIMESTAMP
                """,
                (chat_id, thread_id or 0, target_type, name),
            )

    def get_binding(self, chat_id: int, thread_id: int | None) -> sqlite3.Row | None:
        with self.connect() as db:
            return db.execute(
                "SELECT * FROM bindings WHERE chat_id=? AND thread_id=?",
                (chat_id, thread_id or 0),
            ).fetchone()

    def set_autopost(self, chat_id: int, thread_id: int | None, enabled: bool) -> bool:
        with self.connect() as db:
            cursor = db.execute(
                """
                UPDATE bindings
                SET autopost=?,
                    autopost_failures=CASE WHEN ?=1 THEN 0 ELSE autopost_failures END,
                    autopost_last_error=CASE WHEN ?=1 THEN NULL ELSE autopost_last_error END,
                    updated_at=CURRENT_TIMESTAMP
                WHERE chat_id=? AND thread_id=?
                """,
                (int(enabled), int(enabled), int(enabled), chat_id, thread_id or 0),
            )
            return cursor.rowcount > 0

    def autopost_bindings(self) -> list[sqlite3.Row]:
        with self.connect() as db:
            return db.execute("SELECT * FROM bindings WHERE autopost=1").fetchall()

    def calendar_subscription(
        self, chat_id: int, thread_id: int | None, user_id: int = 0
    ) -> sqlite3.Row | None:
        """Pin a URL to the selected target, never to a mutable chat binding."""
        with self.connect() as db:
            binding = db.execute(
                "SELECT * FROM bindings WHERE chat_id=? AND thread_id=?",
                (chat_id, thread_id or 0),
            ).fetchone()
            if binding is None:
                return None
            key = (
                chat_id,
                thread_id or 0,
                binding["target_type"],
                binding["target_name"],
                user_id,
            )
            db.execute(
                "INSERT OR IGNORE INTO calendar_subscriptions VALUES (?, ?, ?, ?, ?, ?)",
                (secrets.token_hex(24), *key),
            )
            return db.execute(
                "SELECT * FROM calendar_subscriptions WHERE chat_id=? AND thread_id=? "
                "AND target_type=? AND target_name=? AND user_id=?",
                key,
            ).fetchone()

    def revoke_calendars(
        self, chat_id: int, thread_id: int | None, user_id: int = 0
    ) -> None:
        with self.connect() as db:
            db.execute(
                "DELETE FROM calendar_subscriptions WHERE chat_id=? AND thread_id=? AND user_id=?",
                (chat_id, thread_id or 0, user_id),
            )

    def set_calendar_format(self, user_id: int, title_format: str) -> None:
        if user_id <= 0 or title_format not in {"short", "full"}:
            raise ValueError("Invalid calendar user or title format")
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            previous = db.execute(
                "SELECT * FROM calendar_preferences WHERE user_id=?", (user_id,)
            ).fetchone()
            if title_format == (previous["title_format"] if previous else "full"):
                return
            revision = int(previous["revision"]) + 1 if previous else 1
            stamp = dt.datetime.now(dt.timezone.utc).strftime("%Y%m%dT%H%M%SZ")
            db.execute(
                "INSERT INTO calendar_preferences VALUES (?, ?, ?, ?) "
                "ON CONFLICT(user_id) DO UPDATE SET title_format=excluded.title_format, "
                "revision=excluded.revision, updated_at=excluded.updated_at",
                (user_id, title_format, revision, stamp),
            )

    def calendar_targets(self) -> list[sqlite3.Row]:
        with self.connect() as db:
            return db.execute(
                "SELECT DISTINCT target_type, target_name FROM calendar_subscriptions"
            ).fetchall()

    def save_calendar_day(
        self,
        target_type: str,
        name: str,
        date: dt.date,
        events: list[dict[str, str]],
        now: dt.datetime,
    ) -> None:
        key = (target_type, name, date.isoformat())
        with self.connect() as db:
            previous = db.execute(
                "SELECT * FROM calendar_days WHERE target_type=? AND target_name=? "
                "AND target_date=?",
                key,
            ).fetchone()
            by_uid = {event["uid"]: event for event in events}
            if previous:
                for event in json.loads(previous["events"]):
                    if event["uid"] not in by_uid:
                        by_uid[event["uid"]] = {**event, "status": "CANCELLED"}
            encoded = json.dumps(
                sorted(by_uid.values(), key=lambda event: event["uid"]),
                ensure_ascii=False,
                sort_keys=True,
            )
            if previous and previous["events"] == encoded:
                return
            sequence = int(previous["sequence"]) + 1 if previous else 0
            stamp = now.astimezone(dt.timezone.utc).strftime("%Y%m%dT%H%M%SZ")
            db.execute(
                "INSERT INTO calendar_days VALUES (?, ?, ?, ?, ?, ?) "
                "ON CONFLICT(target_type, target_name, target_date) DO UPDATE SET "
                "events=excluded.events, sequence=excluded.sequence, "
                "updated_at=excluded.updated_at",
                (*key, encoded, sequence, stamp),
            )

    def calendar_feed_data(
        self, token: str, first_date: dt.date, last_date: dt.date
    ) -> tuple[sqlite3.Row, list[sqlite3.Row]] | None:
        with self.connect() as db:
            subscription = db.execute(
                "SELECT s.*, COALESCE(p.title_format, 'full') AS title_format, "
                "COALESCE(p.revision, 0) AS title_revision, "
                "COALESCE(p.updated_at, '') AS title_updated_at "
                "FROM calendar_subscriptions s LEFT JOIN calendar_preferences p "
                "ON s.user_id=p.user_id WHERE s.token=?",
                (token,),
            ).fetchone()
            if subscription is None:
                return None
            days = db.execute(
                "SELECT * FROM calendar_days WHERE target_type=? AND target_name=? "
                "AND target_date BETWEEN ? AND ? ORDER BY target_date",
                (
                    subscription["target_type"],
                    subscription["target_name"],
                    first_date.isoformat(),
                    last_date.isoformat(),
                ),
            ).fetchall()
            return subscription, days

    def cleanup_calendar_days(self, first_date: dt.date) -> None:
        with self.connect() as db:
            db.execute(
                "DELETE FROM calendar_days WHERE target_date < ?",
                (first_date.isoformat(),),
            )

    def autopost_fingerprint(
        self,
        chat_id: int,
        thread_id: int,
        name: str,
        target_date: str,
        target_type: str = "group",
    ) -> str | None:
        with self.connect() as db:
            row = db.execute(
                """
                SELECT fingerprint FROM sent_autoposts
                WHERE chat_id=? AND thread_id=? AND target_type=?
                  AND target_name=? AND target_date=?
                """,
                (chat_id, thread_id, target_type, name, target_date),
            ).fetchone()
            return str(row[0]) if row else None

    def mark_autopost(
        self,
        chat_id: int,
        thread_id: int,
        name: str,
        target_date: str,
        fingerprint: str,
        target_type: str = "group",
    ) -> None:
        with self.connect() as db:
            db.execute(
                """
                INSERT INTO sent_autoposts(chat_id, thread_id, target_type, target_name, target_date, fingerprint)
                VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(chat_id, thread_id, target_type, target_name, target_date) DO UPDATE SET
                    fingerprint=excluded.fingerprint,
                    sent_at=CURRENT_TIMESTAMP
                """,
                (chat_id, thread_id, target_type, name, target_date, fingerprint),
            )

    def record_autopost_success(self, chat_id: int, thread_id: int) -> None:
        with self.connect() as db:
            db.execute(
                """
                UPDATE bindings
                SET autopost_failures=0, autopost_last_error=NULL, updated_at=CURRENT_TIMESTAMP
                WHERE chat_id=? AND thread_id=?
                  AND (autopost_failures != 0 OR autopost_last_error IS NOT NULL)
                """,
                (chat_id, thread_id),
            )

    def record_autopost_failure(
        self, chat_id: int, thread_id: int, error: str, disable_after: int = 3
    ) -> bool:
        with self.connect() as db:
            db.execute(
                """
                UPDATE bindings
                SET autopost_failures=autopost_failures + 1,
                    autopost_last_error=?,
                    updated_at=CURRENT_TIMESTAMP
                WHERE chat_id=? AND thread_id=?
                """,
                (error[:500], chat_id, thread_id),
            )
            row = db.execute(
                "SELECT autopost_failures FROM bindings WHERE chat_id=? AND thread_id=?",
                (chat_id, thread_id),
            ).fetchone()
            failures = int(row[0]) if row else 0
            disabled = failures >= disable_after
            if disabled:
                db.execute(
                    "UPDATE bindings SET autopost=0 WHERE chat_id=? AND thread_id=?",
                    (chat_id, thread_id),
                )
            return disabled

    def cleanup_autopost_history(self, keep_days: int = 60) -> int:
        today = dt.datetime.now(dt.timezone.utc).date()
        cutoff = (today - dt.timedelta(days=keep_days)).isoformat()
        with self.connect() as db:
            cursor = db.execute(
                "DELETE FROM sent_autoposts WHERE target_date < ?",
                (cutoff,),
            )
            return cursor.rowcount
