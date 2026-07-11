import csv
import json

import pytest

from hs_crawler.exporter import export_workbook
from hs_crawler.normalize import MAIN_COLUMNS
from hs_crawler.store import CheckpointStore


def _record(code: str, name: str) -> dict[str, object]:
    row = {column: "" for column in MAIN_COLUMNS}
    row.update(
        {
            "hs_code": code,
            "chapter_code": code[:2],
            "product_name": name,
            "raw_sections_json": "{}",
            "source_url": f"https://example.test/Code/{code}.html",
            "scraped_at_utc": "2026-07-11T10:00:00Z",
        }
    )
    return row


@pytest.mark.asyncio
async def test_export_builds_one_workbook_with_total_sheet_then_chapter_sheets(tmp_path) -> None:
    store = CheckpointStore(tmp_path / "checkpoint.sqlite3")
    await store.open()
    await store.enqueue_page("https://example.test/search?keywords=01", "search", "01")
    await store.enqueue_page("https://example.test/search?keywords=02", "search", "02")
    await store.save_record(_record("0101210010", "horse"))
    await store.save_record(_record("0101210090", "other horse"))
    await store.save_record(_record("0201100010", "beef"))
    captured: dict[str, object] = {}

    async def fake_builder(payload_path, output_path) -> None:
        captured.update(json.loads(payload_path.read_text(encoding="utf-8")))
        output_path.write_bytes(b"fake-xlsx")

    summary = await export_workbook(store, tmp_path / "data", builder=fake_builder)

    workbook = tmp_path / "data" / "hs_codes.xlsx"
    assert workbook.read_bytes() == b"fake-xlsx"
    assert summary.chapter_sheets == 2
    assert summary.record_count == 3
    assert captured["sheet_order"] == ["总表", "01", "02"]
    assert captured["chapters"] == ["01", "02"]
    headers = [column["header"] for column in captured["columns"]]
    assert headers == [
        "HS编码",
        "商品名称",
        "商品描述",
        "编码状态",
        "更新时间",
        "第一计量单位",
        "第二计量单位",
        "出口税率(%)",
        "出口退税率(%)",
        "增值税率(%)",
        "最惠国税率(%)",
        "普通进口税率(%)",
        "消费税率(%)",
        "暂定进口税率(%)",
        "申报要素",
        "监管条件",
        "检验检疫",
        "协定税率",
        "RCEP税率",
        "归类路径",
        "CIQ代码",
        "CIQ名称",
        "来源网址",
        "采集时间",
        "原始数据(JSON)",
    ]
    assert captured["columns"][0]["kind"] == "identifier"
    assert [row["hs_code"] for row in captured["records"]] == [
        "0101210010",
        "0101210090",
        "0201100010",
    ]
    assert not (tmp_path / "data" / ".hs_codes.payload.json").exists()
    assert not (tmp_path / "data" / ".hs_codes.xlsx.tmp").exists()
    await store.close()


@pytest.mark.asyncio
async def test_export_escapes_site_values_that_excel_could_execute(tmp_path) -> None:
    store = CheckpointStore(tmp_path / "checkpoint.sqlite3")
    await store.open()
    await store.enqueue_page("https://example.test/search?keywords=01", "search", "01")
    await store.save_record(_record("0101210010", "=DANGEROUS()"))
    captured: dict[str, object] = {}

    async def fake_builder(payload_path, output_path) -> None:
        captured.update(json.loads(payload_path.read_text(encoding="utf-8")))
        output_path.write_bytes(b"fake-xlsx")

    await export_workbook(store, tmp_path / "data", builder=fake_builder)

    assert captured["records"][0]["hs_code"] == "0101210010"
    assert captured["records"][0]["product_name"] == "'=DANGEROUS()"
    await store.close()


@pytest.mark.asyncio
async def test_export_keeps_technical_errors_in_a_separate_csv(tmp_path) -> None:
    store = CheckpointStore(tmp_path / "checkpoint.sqlite3")
    await store.open()
    url = "https://example.test/Code/0101210010.html"
    await store.enqueue_page(url, "detail", "01")
    await store.mark_in_progress(url)
    await store.mark_error(url, "HTTP 404", http_status=404)

    async def fake_builder(payload_path, output_path) -> None:
        output_path.write_bytes(b"fake-xlsx")

    summary = await export_workbook(store, tmp_path / "data", builder=fake_builder)

    with (tmp_path / "data" / "errors.csv").open(
        encoding="utf-8-sig", newline=""
    ) as handle:
        errors = list(csv.DictReader(handle))
    assert summary.error_count == 1
    assert errors[0]["http_status"] == "404"
    assert errors[0]["error"] == "HTTP 404"
    await store.close()
