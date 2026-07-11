import os
import sqlite3
from pathlib import Path
from threading import Event
import time

import pytest

from hs_crawler.monitor import StatusCache, collect_status
from hs_crawler.store import CheckpointStore


@pytest.mark.asyncio
async def test_collect_status_reports_progress_without_mutating_live_work(tmp_path) -> None:
    data_dir = tmp_path / "data"
    database = data_dir / "checkpoint.sqlite3"
    store = CheckpointStore(database)
    await store.open()

    search_url = "https://example.test/search?keywords=01"
    done_url = "https://example.test/Code/0101210010.html"
    active_url = "https://example.test/Code/0101210020.html"
    pending_url = "https://example.test/Code/0101210090.html"
    await store.enqueue_page(search_url, "search", "01")
    await store.mark_done(search_url)
    await store.enqueue_pages(
        [
            (done_url, "detail", "01"),
            (active_url, "detail", "01"),
            (pending_url, "detail", "01"),
        ]
    )
    await store.mark_done(done_url)
    await store.mark_in_progress(active_url)
    await store.save_record(
        {
            "hs_code": "0101210010",
            "chapter_code": "01",
            "product_name": "纯种繁殖用马",
            "source_url": done_url,
            "scraped_at_utc": "2026-07-11T10:00:00Z",
        }
    )
    await store.close()

    status = collect_status(data_dir, log_path=tmp_path / "missing.log")

    assert status["ok"] is True
    assert status["state"] == "running"
    assert status["counts"]["records"] == 1
    assert status["counts"]["detail_done"] == 1
    assert status["counts"]["detail_in_progress"] == 1
    assert status["counts"]["detail_pending"] == 1
    assert status["counts"]["detail_total"] == 3
    assert status["progress"]["detail_percent"] == pytest.approx(33.33)
    assert status["latest"]["hs_code"] == "0101210010"

    with sqlite3.connect(database) as connection:
        row = connection.execute(
            "SELECT status FROM pages WHERE url = ?", (active_url,)
        ).fetchone()
    assert row == ("in_progress",)


def test_collect_status_handles_a_missing_database(tmp_path) -> None:
    status = collect_status(tmp_path / "data")

    assert status["ok"] is False
    assert status["state"] == "waiting"
    assert status["database"]["exists"] is False
    assert status["counts"]["records"] == 0


@pytest.mark.asyncio
async def test_collect_status_rejects_completion_when_a_search_chapter_has_no_records(
    tmp_path,
) -> None:
    data_dir = tmp_path / "data"
    store = CheckpointStore(data_dir / "checkpoint.sqlite3")
    await store.open()
    for chapter in ("01", "02"):
        search = f"https://example.test/search?keywords={chapter}"
        await store.enqueue_page(search, "search", chapter)
        await store.mark_done(search)

    detail = "https://example.test/Code/0101210010.html"
    await store.enqueue_page(detail, "detail", "01")
    await store.mark_done(detail)
    await store.save_record(
        {
            "hs_code": "0101210010",
            "chapter_code": "01",
            "product_name": "horse",
            "source_url": detail,
            "scraped_at_utc": "2026-07-11T10:00:00Z",
        }
    )
    await store.close()

    status = collect_status(data_dir)

    assert status["state"] == "error"
    assert status["state_label"] == "章节缺失"
    assert status["coverage"]["expected_chapters"] == 2
    assert status["coverage"]["covered_chapters"] == 1
    assert status["coverage"]["missing_chapters"] == ["02"]
    assert status["progress"]["detail_percent"] == 50.0


@pytest.mark.asyncio
async def test_collect_status_waits_for_final_workbook_before_completion(tmp_path) -> None:
    data_dir = tmp_path / "data"
    store = CheckpointStore(data_dir / "checkpoint.sqlite3")
    await store.open()

    search = "https://example.test/search?keywords=01"
    detail = "https://example.test/Code/0101210010.html"
    await store.enqueue_page(search, "search", "01")
    await store.mark_done(search)
    await store.enqueue_page(detail, "detail", "01")
    await store.mark_done(detail)
    await store.save_record(
        {
            "hs_code": "0101210010",
            "chapter_code": "01",
            "product_name": "horse",
            "source_url": detail,
            "scraped_at_utc": "2026-07-11T10:00:00Z",
        }
    )
    await store.close()

    exporting = collect_status(data_dir)

    assert exporting["state"] == "running"
    assert exporting["state_label"] == "导出中"
    assert exporting["workbook"]["fresh"] is False

    workbook = data_dir / "hs_codes.xlsx"
    workbook.write_bytes(b"placeholder")
    fresh_timestamp = time.time() + 5
    os.utime(workbook, (fresh_timestamp, fresh_timestamp))

    complete = collect_status(data_dir)

    assert complete["state"] == "complete"
    assert complete["state_label"] == "已完成"
    assert complete["workbook"]["fresh"] is True


def test_status_cache_serves_previous_snapshot_while_refresh_is_blocked() -> None:
    refresh_started = Event()
    release_refresh = Event()

    def slow_collector() -> dict[str, object]:
        refresh_started.set()
        release_refresh.wait(timeout=2)
        return {"ok": True, "records": 12}

    cache = StatusCache(
        slow_collector,
        initial={"ok": False, "records": 0},
        interval_seconds=60,
    )
    cache.start()
    assert refresh_started.wait(timeout=1)

    started = time.monotonic()
    assert cache.snapshot() == {"ok": False, "records": 0}
    assert time.monotonic() - started < 0.1

    release_refresh.set()
    deadline = time.monotonic() + 1
    while cache.snapshot()["records"] != 12 and time.monotonic() < deadline:
        time.sleep(0.01)

    assert cache.snapshot() == {"ok": True, "records": 12}
    cache.close()


def test_monitor_html_polls_incrementally_every_five_seconds() -> None:
    html_path = Path(__file__).parents[1] / "monitor.html"
    html = html_path.read_text(encoding="utf-8")

    assert "/api/status" in html
    assert "POLL_INTERVAL_MS = 5000" in html
    assert "document.hidden" in html
    assert "AbortController" in html
    assert '<meta http-equiv="refresh"' not in html.lower()
