from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import datetime, timezone
import json
from pathlib import Path
import re
import sqlite3
from typing import Any, Iterable


PAGE_KINDS = {"search", "detail"}


def _utcnow() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


@dataclass(frozen=True, slots=True)
class PageTask:
    url: str
    kind: str
    chapter: str
    attempts: int


class CheckpointStore:
    def __init__(self, path: Path, *, reset_in_progress_on_open: bool = True) -> None:
        self.path = Path(path)
        self.reset_in_progress_on_open = reset_in_progress_on_open
        self._connection: sqlite3.Connection | None = None
        self._lock = asyncio.Lock()

    async def __aenter__(self) -> CheckpointStore:
        await self.open()
        return self

    async def __aexit__(self, *_: object) -> None:
        await self.close()

    def _require_connection(self) -> sqlite3.Connection:
        if self._connection is None:
            raise RuntimeError("CheckpointStore.open() must be called first")
        return self._connection

    async def open(self) -> None:
        if self._connection is not None:
            return
        self.path.parent.mkdir(parents=True, exist_ok=True)
        connection = sqlite3.connect(self.path)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA journal_mode=WAL")
        connection.execute("PRAGMA synchronous=NORMAL")
        connection.execute("PRAGMA foreign_keys=ON")
        connection.executescript(
            """
            CREATE TABLE IF NOT EXISTS pages (
                url TEXT PRIMARY KEY,
                kind TEXT NOT NULL CHECK (kind IN ('search', 'detail')),
                chapter TEXT NOT NULL DEFAULT '',
                status TEXT NOT NULL DEFAULT 'pending'
                    CHECK (status IN ('pending', 'in_progress', 'done', 'error')),
                attempts INTEGER NOT NULL DEFAULT 0,
                http_status INTEGER NOT NULL DEFAULT 0,
                error TEXT NOT NULL DEFAULT '',
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );

            CREATE INDEX IF NOT EXISTS idx_pages_kind_status
                ON pages(kind, status, chapter, url);

            CREATE TABLE IF NOT EXISTS records (
                hs_code TEXT PRIMARY KEY,
                chapter TEXT NOT NULL,
                source_url TEXT NOT NULL,
                data_json TEXT NOT NULL,
                scraped_at_utc TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );

            CREATE INDEX IF NOT EXISTS idx_records_chapter_code
                ON records(chapter, hs_code);
            """
        )
        if self.reset_in_progress_on_open:
            now = _utcnow()
            connection.execute(
                "UPDATE pages SET status = 'pending', updated_at = ? WHERE status = 'in_progress'",
                (now,),
            )
        connection.commit()
        self._connection = connection

    async def close(self) -> None:
        if self._connection is None:
            return
        async with self._lock:
            self._connection.commit()
            self._connection.close()
            self._connection = None

    @staticmethod
    def _validate_page(kind: str, chapter: str) -> None:
        if kind not in PAGE_KINDS:
            raise ValueError(f"invalid page kind: {kind!r}")
        if chapter and re.fullmatch(r"\d{2}", chapter) is None:
            raise ValueError(f"invalid chapter: {chapter!r}")

    async def enqueue_page(self, url: str, kind: str, chapter: str = "") -> bool:
        self._validate_page(kind, chapter)
        connection = self._require_connection()
        now = _utcnow()
        async with self._lock:
            cursor = connection.execute(
                """
                INSERT OR IGNORE INTO pages
                    (url, kind, chapter, status, attempts, http_status, error, created_at, updated_at)
                VALUES (?, ?, ?, 'pending', 0, 0, '', ?, ?)
                """,
                (url, kind, chapter, now, now),
            )
            connection.commit()
            return cursor.rowcount == 1

    async def enqueue_pages(self, pages: Iterable[tuple[str, str, str]]) -> int:
        connection = self._require_connection()
        now = _utcnow()
        values: list[tuple[str, str, str, str, str]] = []
        for url, kind, chapter in pages:
            self._validate_page(kind, chapter)
            values.append((url, kind, chapter, now, now))
        if not values:
            return 0
        async with self._lock:
            before = connection.total_changes
            connection.executemany(
                """
                INSERT OR IGNORE INTO pages
                    (url, kind, chapter, status, attempts, http_status, error, created_at, updated_at)
                VALUES (?, ?, ?, 'pending', 0, 0, '', ?, ?)
                """,
                values,
            )
            inserted = connection.total_changes - before
            connection.commit()
            return inserted

    async def pending_pages(self, kind: str, *, limit: int | None = None) -> list[PageTask]:
        if kind not in PAGE_KINDS:
            raise ValueError(f"invalid page kind: {kind!r}")
        connection = self._require_connection()
        sql = (
            "SELECT url, kind, chapter, attempts FROM pages "
            "WHERE kind = ? AND status = 'pending' ORDER BY chapter, url"
        )
        parameters: list[Any] = [kind]
        if limit is not None:
            sql += " LIMIT ?"
            parameters.append(limit)
        async with self._lock:
            rows = connection.execute(sql, parameters).fetchall()
        return [PageTask(**dict(row)) for row in rows]

    async def mark_in_progress(self, url: str) -> None:
        connection = self._require_connection()
        async with self._lock:
            cursor = connection.execute(
                """
                UPDATE pages
                SET status = 'in_progress', attempts = attempts + 1,
                    http_status = 0, error = '', updated_at = ?
                WHERE url = ?
                """,
                (_utcnow(), url),
            )
            if cursor.rowcount != 1:
                raise KeyError(f"unknown page: {url}")
            connection.commit()

    async def mark_done(self, url: str, *, http_status: int = 200) -> None:
        connection = self._require_connection()
        async with self._lock:
            cursor = connection.execute(
                """
                UPDATE pages
                SET status = 'done', http_status = ?, error = '', updated_at = ?
                WHERE url = ?
                """,
                (http_status, _utcnow(), url),
            )
            if cursor.rowcount != 1:
                raise KeyError(f"unknown page: {url}")
            connection.commit()

    async def mark_error(self, url: str, error: str, *, http_status: int = 0) -> None:
        connection = self._require_connection()
        async with self._lock:
            cursor = connection.execute(
                """
                UPDATE pages
                SET status = 'error', http_status = ?, error = ?, updated_at = ?
                WHERE url = ?
                """,
                (http_status, error[:4000], _utcnow(), url),
            )
            if cursor.rowcount != 1:
                raise KeyError(f"unknown page: {url}")
            connection.commit()

    async def requeue_errors(self, kind: str | None = None) -> int:
        if kind is not None and kind not in PAGE_KINDS:
            raise ValueError(f"invalid page kind: {kind!r}")
        connection = self._require_connection()
        where = "status = 'error'"
        parameters: list[Any] = [_utcnow()]
        if kind is not None:
            where += " AND kind = ?"
            parameters.append(kind)
        async with self._lock:
            cursor = connection.execute(
                f"""
                UPDATE pages
                SET status = 'pending', http_status = 0, error = '', updated_at = ?
                WHERE {where}
                """,
                parameters,
            )
            connection.commit()
            return cursor.rowcount

    async def save_record(self, record: dict[str, Any]) -> None:
        code = str(record.get("hs_code", ""))
        chapter = str(record.get("chapter_code", ""))
        if re.fullmatch(r"\d{10}", code) is None:
            raise ValueError(f"invalid HS code: {code!r}")
        if re.fullmatch(r"\d{2}", chapter) is None or not code.startswith(chapter):
            raise ValueError(f"record chapter {chapter!r} does not match HS code {code!r}")
        source_url = str(record.get("source_url", ""))
        scraped_at = str(record.get("scraped_at_utc", ""))
        payload = json.dumps(record, ensure_ascii=False, separators=(",", ":"))
        connection = self._require_connection()
        async with self._lock:
            connection.execute(
                """
                INSERT INTO records
                    (hs_code, chapter, source_url, data_json, scraped_at_utc, updated_at)
                VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(hs_code) DO UPDATE SET
                    chapter = excluded.chapter,
                    source_url = excluded.source_url,
                    data_json = excluded.data_json,
                    scraped_at_utc = excluded.scraped_at_utc,
                    updated_at = excluded.updated_at
                """,
                (code, chapter, source_url, payload, scraped_at, _utcnow()),
            )
            connection.commit()

    async def records(self, chapter: str | None = None) -> list[dict[str, Any]]:
        connection = self._require_connection()
        if chapter is None:
            sql = "SELECT data_json FROM records ORDER BY hs_code"
            parameters: tuple[str, ...] = ()
        else:
            if re.fullmatch(r"\d{2}", chapter) is None:
                raise ValueError(f"invalid chapter: {chapter!r}")
            sql = "SELECT data_json FROM records WHERE chapter = ? ORDER BY hs_code"
            parameters = (chapter,)
        async with self._lock:
            rows = connection.execute(sql, parameters).fetchall()
        return [json.loads(row["data_json"]) for row in rows]

    async def chapters(self) -> list[str]:
        connection = self._require_connection()
        async with self._lock:
            rows = connection.execute(
                """
                SELECT chapter FROM pages WHERE length(chapter) = 2
                UNION
                SELECT chapter FROM records WHERE length(chapter) = 2
                ORDER BY chapter
                """
            ).fetchall()
        return [str(row["chapter"]) for row in rows]

    async def errors(self) -> list[dict[str, Any]]:
        connection = self._require_connection()
        async with self._lock:
            rows = connection.execute(
                """
                SELECT url, kind, chapter, attempts, http_status, error, updated_at
                FROM pages WHERE status = 'error'
                ORDER BY kind, chapter, url
                """
            ).fetchall()
        return [dict(row) for row in rows]

    async def stats(self) -> dict[str, int]:
        connection = self._require_connection()
        async with self._lock:
            grouped = connection.execute(
                "SELECT kind, status, count(*) AS count FROM pages GROUP BY kind, status"
            ).fetchall()
            record_count = connection.execute("SELECT count(*) FROM records").fetchone()[0]
        stats = {f"{row['kind']}_{row['status']}": int(row["count"]) for row in grouped}
        stats["records"] = int(record_count)
        return stats
