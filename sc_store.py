from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass
import json
from pathlib import Path
import sqlite3
from typing import Iterator, Sequence


DEFAULT_DATABASE_PATH = Path("data") / "sc_monitor.sqlite3"


@dataclass(frozen=True)
class UserSCStats:
    count: int
    total_amount: float


@dataclass(frozen=True)
class StoredSC:
    id: int
    stats: UserSCStats


class SCStore:
    """SQLite-backed SC history and aggregate statistics."""

    def __init__(self, database_path: str | Path = DEFAULT_DATABASE_PATH):
        self.database_path = Path(database_path)
        self.database_path.parent.mkdir(parents=True, exist_ok=True)
        self._initialize()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.database_path, timeout=5)
        connection.execute("PRAGMA busy_timeout = 5000")
        return connection

    @contextmanager
    def _connection(self) -> Iterator[sqlite3.Connection]:
        connection = self._connect()
        try:
            with connection:
                yield connection
        finally:
            connection.close()

    def _initialize(self) -> None:
        with self._connection() as connection:
            connection.execute("PRAGMA journal_mode = WAL")
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS super_chats (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    user_uid INTEGER NOT NULL,
                    nickname TEXT NOT NULL,
                    content TEXT NOT NULL,
                    amount REAL NOT NULL,
                    sent_at INTEGER NOT NULL,
                    bv TEXT,
                    video_title TEXT,
                    video_tags TEXT NOT NULL DEFAULT '[]',
                    blacklisted INTEGER NOT NULL DEFAULT 0,
                    blacklist_matches TEXT NOT NULL DEFAULT '[]'
                );

                CREATE INDEX IF NOT EXISTS idx_super_chats_user_uid
                ON super_chats(user_uid);

                CREATE INDEX IF NOT EXISTS idx_super_chats_bv
                ON super_chats(bv);
                """
            )

    def record_sc(
        self,
        *,
        user_uid: int,
        nickname: str,
        content: str,
        amount: float,
        sent_at: int,
        bv: str | None,
    ) -> StoredSC:
        with self._connection() as connection:
            cursor = connection.execute(
                """
                INSERT INTO super_chats (
                    user_uid, nickname, content, amount, sent_at, bv
                ) VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    int(user_uid),
                    str(nickname),
                    str(content),
                    float(amount),
                    int(sent_at),
                    bv,
                ),
            )
            row = connection.execute(
                """
                SELECT COUNT(*), COALESCE(SUM(amount), 0)
                FROM super_chats
                WHERE user_uid = ?
                """,
                (int(user_uid),),
            ).fetchone()
            assert row is not None
            return StoredSC(
                id=int(cursor.lastrowid),
                stats=UserSCStats(count=int(row[0]), total_amount=float(row[1])),
            )

    def get_user_stats(self, user_uid: int) -> UserSCStats:
        with self._connection() as connection:
            row = connection.execute(
                """
                SELECT COUNT(*), COALESCE(SUM(amount), 0)
                FROM super_chats
                WHERE user_uid = ?
                """,
                (int(user_uid),),
            ).fetchone()
        assert row is not None
        return UserSCStats(count=int(row[0]), total_amount=float(row[1]))

    def reassign_user_uid_for_nickname(self, nickname: str, user_uid: int) -> int:
        """Repair historical rows created by a source that used unstable user IDs."""
        with self._connection() as connection:
            cursor = connection.execute(
                """
                UPDATE super_chats
                SET user_uid = ?
                WHERE nickname = ? AND user_uid != ?
                """,
                (int(user_uid), str(nickname), int(user_uid)),
            )
            return int(cursor.rowcount)

    def update_video_metadata(
        self,
        record_id: int,
        *,
        title: str,
        tags: Sequence[str],
        blacklisted: bool,
        blacklist_matches: Sequence[str],
    ) -> None:
        with self._connection() as connection:
            connection.execute(
                """
                UPDATE super_chats
                SET video_title = ?,
                    video_tags = ?,
                    blacklisted = ?,
                    blacklist_matches = ?
                WHERE id = ?
                """,
                (
                    str(title),
                    json.dumps(list(tags), ensure_ascii=False),
                    int(bool(blacklisted)),
                    json.dumps(list(blacklist_matches), ensure_ascii=False),
                    int(record_id),
                ),
            )
