from __future__ import annotations

import asyncio
import csv
from dataclasses import asdict, dataclass
import json
import logging
import os
from pathlib import Path
from typing import Any, Awaitable, Callable, Iterable

from .store import CheckpointStore


LOGGER = logging.getLogger(__name__)
ERROR_COLUMNS = (
    "url",
    "kind",
    "chapter",
    "attempts",
    "http_status",
    "error",
    "updated_at",
)
DEFAULT_BUNDLED_NODE = Path(
    r"C:\Users\MemoryLeak\.cache\codex-runtimes\codex-primary-runtime\dependencies\node\bin\node.exe"
)


@dataclass(frozen=True, slots=True)
class WorkbookColumn:
    key: str
    header: str
    kind: str
    width: int


WORKBOOK_COLUMNS: tuple[WorkbookColumn, ...] = (
    WorkbookColumn("hs_code", "HS编码", "identifier", 16),
    WorkbookColumn("product_name", "商品名称", "text", 28),
    WorkbookColumn("product_description", "商品描述", "text", 36),
    WorkbookColumn("code_status", "编码状态", "text", 12),
    WorkbookColumn("updated_date", "更新时间", "date", 14),
    WorkbookColumn("unit_primary", "第一计量单位", "text", 14),
    WorkbookColumn("unit_secondary", "第二计量单位", "text", 14),
    WorkbookColumn("export_tax_rate_pct", "出口税率(%)", "percent", 14),
    WorkbookColumn("export_rebate_rate_pct", "出口退税率(%)", "percent", 16),
    WorkbookColumn("vat_rate_pct", "增值税率(%)", "percent", 14),
    WorkbookColumn("mfn_import_rate_pct", "最惠国税率(%)", "percent", 16),
    WorkbookColumn("general_import_rate_pct", "普通进口税率(%)", "percent", 18),
    WorkbookColumn("consumption_tax_rate_pct", "消费税率(%)", "percent", 14),
    WorkbookColumn("provisional_import_rate_pct", "暂定进口税率(%)", "percent", 18),
    WorkbookColumn("declaration_elements_text", "申报要素", "text", 38),
    WorkbookColumn("supervision_text", "监管条件", "text", 36),
    WorkbookColumn("inspection_text", "检验检疫", "text", 36),
    WorkbookColumn("agreement_rates_text", "协定税率", "text", 40),
    WorkbookColumn("rcep_rates_text", "RCEP税率", "text", 40),
    WorkbookColumn("hierarchy_text", "归类路径", "text", 42),
    WorkbookColumn("ciq_codes", "CIQ代码", "identifier", 24),
    WorkbookColumn("ciq_names", "CIQ名称", "text", 32),
    WorkbookColumn("source_url", "来源网址", "text", 42),
    WorkbookColumn("scraped_at_utc", "采集时间", "datetime", 20),
    WorkbookColumn("raw_sections_json", "原始数据(JSON)", "json", 48),
)


@dataclass(frozen=True, slots=True)
class ExportSummary:
    chapter_sheets: int
    record_count: int
    error_count: int


Builder = Callable[[Path, Path], Awaitable[None]]


def _safe_cell(value: Any) -> Any:
    if isinstance(value, str) and value.startswith(("=", "+", "-", "@")):
        return "'" + value
    return value


def _pair_codes_and_names(codes: Any, names: Any) -> str:
    code_items = str(codes or "").split("|") if codes else []
    name_items = str(names or "").split("|") if names else []
    count = max(len(code_items), len(name_items))
    values: list[str] = []
    for index in range(count):
        code = code_items[index] if index < len(code_items) else ""
        name = name_items[index] if index < len(name_items) else ""
        values.append(" ".join(part for part in (code, name) if part))
    return " | ".join(value for value in values if value)


def _workbook_record(record: dict[str, Any]) -> dict[str, Any]:
    enriched = dict(record)
    enriched["supervision_text"] = _pair_codes_and_names(
        record.get("supervision_codes"), record.get("supervision_names")
    )
    enriched["inspection_text"] = _pair_codes_and_names(
        record.get("inspection_codes"), record.get("inspection_names")
    )
    return {
        column.key: _safe_cell(enriched.get(column.key, ""))
        for column in WORKBOOK_COLUMNS
    }


def _write_csv_atomic(
    path: Path,
    columns: Iterable[str],
    rows: Iterable[dict[str, Any]],
) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.tmp")
    fieldnames = list(columns)
    try:
        with temporary.open("w", encoding="utf-8-sig", newline="") as handle:
            writer = csv.DictWriter(handle, fieldnames=fieldnames, extrasaction="ignore")
            writer.writeheader()
            for row in rows:
                writer.writerow(
                    {column: _safe_cell(row.get(column, "")) for column in fieldnames}
                )
        os.replace(temporary, path)
    finally:
        if temporary.exists():
            temporary.unlink()


async def run_artifact_builder(payload_path: Path, output_path: Path) -> None:
    project_root = Path(__file__).resolve().parents[2]
    builder_path = project_root / "tools" / "build_hs_workbook.mjs"
    node_path = Path(os.environ.get("HS_CRAWLER_NODE_PATH", str(DEFAULT_BUNDLED_NODE)))
    if not node_path.is_file():
        raise RuntimeError(f"bundled Node.js executable not found: {node_path}")
    if not builder_path.is_file():
        raise RuntimeError(f"workbook builder not found: {builder_path}")

    command = [str(node_path), str(builder_path), str(payload_path), str(output_path)]
    preview_dir = os.environ.get("HS_CRAWLER_PREVIEW_DIR")
    if preview_dir:
        command.append(str(Path(preview_dir).resolve()))
    process = await asyncio.create_subprocess_exec(
        *command,
        cwd=str(project_root),
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    stdout, stderr = await process.communicate()
    if stdout:
        LOGGER.info("Workbook builder: %s", stdout.decode("utf-8", errors="replace").strip())
    if process.returncode != 0:
        message = stderr.decode("utf-8", errors="replace").strip()
        raise RuntimeError(f"workbook builder failed with exit {process.returncode}: {message}")


async def export_workbook(
    store: CheckpointStore,
    output_dir: Path,
    *,
    builder: Builder = run_artifact_builder,
) -> ExportSummary:
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    chapters = await store.chapters()
    records = await store.records()
    safe_records = [_workbook_record(record) for record in records]
    payload = {
        "columns": [asdict(column) for column in WORKBOOK_COLUMNS],
        "sheet_order": ["总表", *chapters],
        "chapters": chapters,
        "records": safe_records,
    }
    payload_path = output_dir / ".hs_codes.payload.json"
    temporary_workbook = output_dir / ".hs_codes.xlsx.tmp"
    final_workbook = output_dir / "hs_codes.xlsx"
    try:
        payload_path.write_text(
            json.dumps(payload, ensure_ascii=False, separators=(",", ":")),
            encoding="utf-8",
        )
        await builder(payload_path, temporary_workbook)
        if not temporary_workbook.is_file() or temporary_workbook.stat().st_size == 0:
            raise RuntimeError("workbook builder did not create a non-empty file")
        os.replace(temporary_workbook, final_workbook)
    finally:
        if payload_path.exists():
            payload_path.unlink()
        if temporary_workbook.exists():
            temporary_workbook.unlink()

    errors = await store.errors()
    _write_csv_atomic(output_dir / "errors.csv", ERROR_COLUMNS, errors)
    return ExportSummary(
        chapter_sheets=len(chapters),
        record_count=len(records),
        error_count=len(errors),
    )
