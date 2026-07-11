from __future__ import annotations

import argparse
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
import re
import sqlite3
from threading import Event, Lock, Thread
from typing import Any, Callable
from urllib.parse import urlparse


DEFAULT_HTML_PATH = Path(__file__).resolve().parents[2] / "monitor.html"
PAGE_STATUSES = ("pending", "in_progress", "done", "error")
PAGE_KINDS = ("search", "detail")


def _utcnow() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def _iso_from_timestamp(timestamp: float) -> str:
    return (
        datetime.fromtimestamp(timestamp, timezone.utc)
        .isoformat(timespec="seconds")
        .replace("+00:00", "Z")
    )


def _timestamp_from_iso(value: str) -> float:
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()
    except ValueError:
        return 0.0


def _file_metadata(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {
            "exists": False,
            "path": str(path.resolve()),
            "size_bytes": 0,
            "modified_at": "",
        }
    stat = path.stat()
    return {
        "exists": True,
        "path": str(path.resolve()),
        "size_bytes": stat.st_size,
        "modified_at": _iso_from_timestamp(stat.st_mtime),
    }


def _empty_counts() -> dict[str, int]:
    counts = {
        f"{kind}_{status}": 0
        for kind in PAGE_KINDS
        for status in PAGE_STATUSES
    }
    counts.update(
        {
            "records": 0,
            "chapters": 0,
            "expected_chapters": 0,
            "covered_chapters": 0,
            "search_total": 0,
            "detail_total": 0,
        }
    )
    return counts


def _tail_line(path: Path, maximum_bytes: int = 16_384) -> str:
    if not path.exists() or path.stat().st_size == 0:
        return ""
    with path.open("rb") as handle:
        handle.seek(max(0, path.stat().st_size - maximum_bytes))
        chunk = handle.read()
    lines = chunk.decode("utf-8", errors="replace").splitlines()
    return lines[-1].strip() if lines else ""


def _read_connection(database: Path) -> sqlite3.Connection:
    uri = f"{database.resolve().as_uri()}?mode=ro"
    connection = sqlite3.connect(uri, uri=True, timeout=2.0)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA query_only=ON")
    return connection


def _percentage(done: int, total: int) -> float:
    return round((done / total * 100) if total else 0.0, 2)


def _state_for(counts: dict[str, int]) -> tuple[str, str]:
    total = counts["detail_total"]
    pending = counts["detail_pending"] + counts["search_pending"]
    active = counts["detail_in_progress"] + counts["search_in_progress"]
    errors = counts["detail_error"] + counts["search_error"]
    expected = counts["expected_chapters"]
    covered = counts["covered_chapters"]
    if total and expected and pending == 0 and active == 0 and errors == 0:
        if covered == expected:
            return "complete", "已完成"
        return "error", "章节缺失"
    if active or pending:
        return "running", "采集中"
    if errors:
        return "error", "存在错误"
    if expected and covered < expected:
        return "error", "章节缺失"
    return "waiting", "等待数据"


def _status_shell(data_dir: Path, log_path: Path) -> dict[str, Any]:
    data_dir = Path(data_dir)
    database = data_dir / "checkpoint.sqlite3"
    workbook = data_dir / "hs_codes.xlsx"
    counts = _empty_counts()
    database_meta = _file_metadata(database)
    workbook_meta = _file_metadata(workbook)
    workbook_meta["fresh"] = False
    log_meta = _file_metadata(log_path)
    log_meta["last_line"] = _tail_line(log_path)

    payload: dict[str, Any] = {
        "ok": False,
        "state": "waiting",
        "state_label": "等待数据",
        "message": "状态库尚未创建",
        "updated_at": _utcnow(),
        "counts": counts,
        "progress": {"search_percent": 0.0, "detail_percent": 0.0},
        "coverage": {
            "expected_chapters": 0,
            "covered_chapters": 0,
            "missing_chapters": [],
            "percent": 0.0,
        },
        "latest": {
            "hs_code": "",
            "chapter": "",
            "scraped_at_utc": "",
            "updated_at": "",
            "active_code": "",
            "active_url": "",
            "activity_at": "",
        },
        "last_error": {
            "kind": "",
            "chapter": "",
            "url": "",
            "http_status": 0,
            "message": "",
            "updated_at": "",
        },
        "database": database_meta,
        "workbook": workbook_meta,
        "log": log_meta,
    }
    return payload


def collect_status(data_dir: Path, *, log_path: Path | None = None) -> dict[str, Any]:
    data_dir = Path(data_dir)
    database = data_dir / "checkpoint.sqlite3"
    log_path = Path(log_path) if log_path is not None else Path("logs/final.log")
    payload = _status_shell(data_dir, log_path)
    counts = payload["counts"]
    database_meta = payload["database"]
    if not database_meta["exists"]:
        return payload

    try:
        with _read_connection(database) as connection:
            grouped = connection.execute(
                "SELECT kind, status, count(*) AS count FROM pages GROUP BY kind, status"
            ).fetchall()
            for row in grouped:
                key = f"{row['kind']}_{row['status']}"
                if key in counts:
                    counts[key] = int(row["count"])

            records_row = connection.execute(
                "SELECT count(*) AS records, count(DISTINCT chapter) AS chapters FROM records"
            ).fetchone()
            counts["records"] = int(records_row["records"])
            counts["chapters"] = int(records_row["chapters"])
            counts["search_total"] = sum(
                counts[f"search_{status}"] for status in PAGE_STATUSES
            )
            counts["detail_total"] = sum(
                counts[f"detail_{status}"] for status in PAGE_STATUSES
            )

            expected_chapters = {
                str(row["chapter"])
                for row in connection.execute(
                    "SELECT DISTINCT chapter FROM pages WHERE kind = 'search' ORDER BY chapter"
                )
                if re.fullmatch(r"\d{2}", str(row["chapter"]))
            }
            covered_chapters = {
                str(row["chapter"])
                for row in connection.execute(
                    "SELECT DISTINCT chapter FROM records ORDER BY chapter"
                )
                if re.fullmatch(r"\d{2}", str(row["chapter"]))
            }
            missing_chapters = sorted(expected_chapters - covered_chapters)
            counts["expected_chapters"] = len(expected_chapters)
            counts["covered_chapters"] = len(expected_chapters & covered_chapters)

            latest_record = connection.execute(
                """
                SELECT hs_code, chapter, scraped_at_utc, updated_at
                FROM records
                ORDER BY updated_at DESC, hs_code DESC
                LIMIT 1
                """
            ).fetchone()
            active_page = connection.execute(
                """
                SELECT url, chapter, updated_at
                FROM pages
                WHERE status = 'in_progress'
                ORDER BY updated_at DESC, url
                LIMIT 1
                """
            ).fetchone()
            latest_error = connection.execute(
                """
                SELECT kind, chapter, url, http_status, error, updated_at
                FROM pages
                WHERE status = 'error'
                ORDER BY updated_at DESC, url
                LIMIT 1
                """
            ).fetchone()

        if latest_record is not None:
            payload["latest"].update(
                {
                    "hs_code": str(latest_record["hs_code"]),
                    "chapter": str(latest_record["chapter"]),
                    "scraped_at_utc": str(latest_record["scraped_at_utc"]),
                    "updated_at": str(latest_record["updated_at"]),
                }
            )
        if active_page is not None:
            active_url = str(active_page["url"])
            code_match = re.search(r"/Code/(\d{10})\.html", active_url, re.IGNORECASE)
            payload["latest"].update(
                {
                    "active_code": code_match.group(1) if code_match else "",
                    "active_url": active_url,
                    "activity_at": str(active_page["updated_at"]),
                }
            )
        if latest_error is not None:
            payload["last_error"].update(
                {
                    "kind": str(latest_error["kind"]),
                    "chapter": str(latest_error["chapter"]),
                    "url": str(latest_error["url"]),
                    "http_status": int(latest_error["http_status"]),
                    "message": str(latest_error["error"]),
                    "updated_at": str(latest_error["updated_at"]),
                }
            )

        workbook_modified_at = _timestamp_from_iso(
            str(payload["workbook"]["modified_at"])
        )
        latest_record_updated_at = (
            _timestamp_from_iso(str(latest_record["updated_at"]))
            if latest_record is not None
            else 0.0
        )
        workbook_is_fresh = bool(
            payload["workbook"]["exists"]
            and latest_record_updated_at
            and workbook_modified_at >= latest_record_updated_at
        )
        payload["workbook"]["fresh"] = workbook_is_fresh

        state, state_label = _state_for(counts)
        if state == "complete" and not workbook_is_fresh:
            state, state_label = "running", "导出中"
        coverage_percent = _percentage(
            counts["covered_chapters"], counts["expected_chapters"]
        )
        detail_queue_percent = _percentage(
            counts["detail_done"], counts["detail_total"]
        )
        payload.update(
            {
                "ok": True,
                "state": state,
                "state_label": state_label,
                "message": (
                    "最终工作簿正在生成"
                    if state_label == "导出中"
                    else (
                        f"缺少章节数据: {', '.join(missing_chapters)}"
                        if state == "error" and missing_chapters
                        else ""
                    )
                ),
                "progress": {
                    "search_percent": _percentage(
                        counts["search_done"], counts["search_total"]
                    ),
                    "detail_percent": min(detail_queue_percent, coverage_percent),
                },
                "coverage": {
                    "expected_chapters": counts["expected_chapters"],
                    "covered_chapters": counts["covered_chapters"],
                    "missing_chapters": missing_chapters,
                    "percent": coverage_percent,
                },
            }
        )
    except (OSError, sqlite3.Error) as error:
        payload.update(
            {
                "state": "error",
                "state_label": "读取异常",
                "message": str(error),
            }
        )
    return payload


class StatusCache:
    def __init__(
        self,
        collector: Callable[[], dict[str, Any]],
        *,
        initial: dict[str, Any],
        interval_seconds: float = 5.0,
    ) -> None:
        if interval_seconds <= 0:
            raise ValueError("interval_seconds must be positive")
        self._collector = collector
        self._value = initial
        self._interval_seconds = interval_seconds
        self._lock = Lock()
        self._stop = Event()
        self._thread: Thread | None = None

    def start(self) -> None:
        if self._thread is not None and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = Thread(
            target=self._run,
            name="hs-monitor-status-cache",
            daemon=True,
        )
        self._thread.start()

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            return self._value

    def close(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=2.0)

    def _run(self) -> None:
        while not self._stop.is_set():
            try:
                value = self._collector()
            except Exception:
                pass
            else:
                with self._lock:
                    self._value = value
            self._stop.wait(self._interval_seconds)


class MonitorServer(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(
        self,
        address: tuple[str, int],
        *,
        data_dir: Path,
        log_path: Path,
        html_path: Path,
    ) -> None:
        self.data_dir = data_dir
        self.log_path = log_path
        self.html_path = html_path
        super().__init__(address, MonitorRequestHandler)
        self.status_cache = StatusCache(
            lambda: collect_status(self.data_dir, log_path=self.log_path),
            initial=_status_shell(self.data_dir, self.log_path),
        )
        self.status_cache.start()

    def server_close(self) -> None:
        self.status_cache.close()
        super().server_close()


class MonitorRequestHandler(BaseHTTPRequestHandler):
    server: MonitorServer

    def _send(self, status: int, content_type: str, body: bytes) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store, max-age=0")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()
        try:
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError, TimeoutError, OSError):
            self.close_connection = True

    def do_GET(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler API
        path = urlparse(self.path).path
        if path == "/api/status":
            payload = self.server.status_cache.snapshot()
            body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
            self._send(200, "application/json; charset=utf-8", body)
            return
        if path in {"/", "/monitor.html"}:
            try:
                body = self.server.html_path.read_bytes()
            except OSError as error:
                body = f"Monitor page unavailable: {error}".encode("utf-8")
                self._send(500, "text/plain; charset=utf-8", body)
                return
            self._send(200, "text/html; charset=utf-8", body)
            return
        self._send(404, "text/plain; charset=utf-8", b"Not found")

    def log_message(self, _format: str, *_args: object) -> None:
        return


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Serve the live HS crawl monitor")
    parser.add_argument("--data-dir", type=Path, default=Path("data"))
    parser.add_argument("--log-path", type=Path, default=Path("logs/final.log"))
    parser.add_argument("--html-path", type=Path, default=DEFAULT_HTML_PATH)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8765)
    return parser


def main() -> None:
    args = build_parser().parse_args()
    server = MonitorServer(
        (args.host, args.port),
        data_dir=args.data_dir.resolve(),
        log_path=args.log_path.resolve(),
        html_path=args.html_path.resolve(),
    )
    try:
        server.serve_forever(poll_interval=0.5)
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
