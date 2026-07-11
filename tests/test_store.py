import pytest

from hs_crawler.store import CheckpointStore


@pytest.mark.asyncio
async def test_store_deduplicates_pages_and_resets_interrupted_work(tmp_path) -> None:
    database = tmp_path / "checkpoint.sqlite3"
    store = CheckpointStore(database)
    await store.open()

    assert await store.enqueue_page("https://example.test/search?keywords=01", "search", "01")
    assert not await store.enqueue_page("https://example.test/search?keywords=01", "search", "01")
    await store.mark_in_progress("https://example.test/search?keywords=01")
    await store.close()

    reopened = CheckpointStore(database)
    await reopened.open()
    tasks = await reopened.pending_pages("search")

    assert len(tasks) == 1
    assert tasks[0].chapter == "01"
    assert tasks[0].attempts == 1
    await reopened.close()


@pytest.mark.asyncio
async def test_store_upserts_unique_records_and_validates_chapter(tmp_path) -> None:
    store = CheckpointStore(tmp_path / "checkpoint.sqlite3")
    await store.open()
    record = {
        "hs_code": "0101210010",
        "chapter_code": "01",
        "product_name": "first",
        "source_url": "https://example.test/Code/0101210010.html",
        "scraped_at_utc": "2026-07-11T10:00:00Z",
    }

    await store.save_record(record)
    await store.save_record({**record, "product_name": "updated"})

    rows = await store.records("01")
    assert len(rows) == 1
    assert rows[0]["product_name"] == "updated"
    with pytest.raises(ValueError, match="chapter"):
        await store.save_record({**record, "chapter_code": "02"})
    await store.close()


@pytest.mark.asyncio
async def test_store_tracks_terminal_errors_and_stats(tmp_path) -> None:
    store = CheckpointStore(tmp_path / "checkpoint.sqlite3")
    await store.open()
    url = "https://example.test/Code/0101210010.html"
    await store.enqueue_page(url, "detail", "01")
    await store.mark_in_progress(url)
    await store.mark_error(url, "HTTP 404", http_status=404)

    stats = await store.stats()
    errors = await store.errors()

    assert stats["detail_error"] == 1
    assert stats["records"] == 0
    assert errors[0]["url"] == url
    assert errors[0]["http_status"] == 404
    assert errors[0]["attempts"] == 1
    await store.close()


@pytest.mark.asyncio
async def test_store_can_requeue_terminal_errors_explicitly(tmp_path) -> None:
    store = CheckpointStore(tmp_path / "checkpoint.sqlite3")
    await store.open()
    url = "https://example.test/Code/0101210010.html"
    await store.enqueue_page(url, "detail", "01")
    await store.mark_in_progress(url)
    await store.mark_error(url, "timeout")

    assert await store.requeue_errors("detail") == 1
    assert [task.url for task in await store.pending_pages("detail")] == [url]
    await store.close()


@pytest.mark.asyncio
async def test_read_only_style_open_does_not_reset_live_in_progress_rows(tmp_path) -> None:
    database = tmp_path / "checkpoint.sqlite3"
    writer = CheckpointStore(database)
    await writer.open()
    url = "https://example.test/Code/0101210010.html"
    await writer.enqueue_page(url, "detail", "01")
    await writer.mark_in_progress(url)
    await writer.close()

    monitor = CheckpointStore(database, reset_in_progress_on_open=False)
    await monitor.open()

    assert (await monitor.stats())["detail_in_progress"] == 1
    assert await monitor.pending_pages("detail") == []
    await monitor.close()
