from __future__ import annotations

from datetime import datetime, timezone
import json
import re
from typing import Any, Iterable

from .parser import RawDetailRecord, SectionRow


RATE_FIELDS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("export_tax_rate", ("出口税率",)),
    ("export_rebate_rate", ("出口退税税率", "出口退税率")),
    ("vat_rate", ("增值税率",)),
    ("mfn_import_rate", ("最惠国税率",)),
    ("general_import_rate", ("进口普通税率",)),
    ("consumption_tax_rate", ("消费税率",)),
    ("provisional_import_rate", ("进口暂定税率", "暂定税率")),
)

MAIN_COLUMNS: tuple[str, ...] = (
    "hs_code",
    "chapter_code",
    "heading_code",
    "subheading_code",
    "hs8_code",
    "product_name",
    "product_description",
    "code_status",
    "is_expired",
    "updated_date",
    "unit_raw",
    "unit_primary",
    "unit_secondary",
    "export_tax_rate_raw",
    "export_tax_rate_pct",
    "export_rebate_rate_raw",
    "export_rebate_rate_pct",
    "vat_rate_raw",
    "vat_rate_pct",
    "mfn_import_rate_raw",
    "mfn_import_rate_pct",
    "general_import_rate_raw",
    "general_import_rate_pct",
    "consumption_tax_rate_raw",
    "consumption_tax_rate_pct",
    "provisional_import_rate_raw",
    "provisional_import_rate_pct",
    "declaration_elements_text",
    "declaration_elements_json",
    "supervision_codes",
    "supervision_names",
    "supervision_json",
    "inspection_codes",
    "inspection_names",
    "inspection_json",
    "agreement_rates_text",
    "agreement_rates_json",
    "rcep_rates_text",
    "rcep_rates_json",
    "category_name",
    "chapter_name",
    "heading_name",
    "subheading_name",
    "hs8_name",
    "hierarchy_text",
    "hierarchy_json",
    "ciq_codes",
    "ciq_names",
    "ciq_json",
    "raw_sections_json",
    "source_url",
    "scraped_at_utc",
)

_UNAVAILABLE = {"", "无", "-", "--", "n/a", "N/A", "null", "None"}


def parse_rate(value: str) -> float | None:
    text = value.strip()
    if text in _UNAVAILABLE:
        return None
    match = re.fullmatch(r"(-?\d+(?:\.\d+)?)\s*%?", text)
    return float(match.group(1)) if match else None


def normalize_date(value: str) -> str:
    text = value.strip()
    if not text:
        return ""
    for pattern in ("%Y/%m/%d", "%Y-%m-%d", "%Y.%m.%d"):
        try:
            return datetime.strptime(text, pattern).date().isoformat()
        except ValueError:
            continue
    match = re.fullmatch(r"(\d{4})年(\d{1,2})月(\d{1,2})日", text)
    if match:
        return datetime(*(int(part) for part in match.groups())).date().isoformat()
    return text


def _first_value(rows: Iterable[SectionRow], labels: Iterable[str]) -> str:
    accepted = set(labels)
    for row in rows:
        if row.label in accepted:
            return row.value
    return ""


def _rows_json(rows: Iterable[SectionRow]) -> str:
    return json.dumps(
        [{"label": row.label, "value": row.value} for row in rows],
        ensure_ascii=False,
        separators=(",", ":"),
    )


def _summary(rows: Iterable[SectionRow]) -> str:
    return " | ".join(
        f"{row.label}:{row.value}" if row.value else row.label for row in rows
    )


def _coded_section(rows: list[SectionRow]) -> tuple[str, str, str]:
    useful = [row for row in rows if row.label != "无" and (row.label or row.value)]
    return (
        "|".join(row.label for row in useful),
        "|".join(row.value for row in useful),
        _rows_json(useful),
    )


def _rate_section(rows: list[SectionRow]) -> tuple[str, str]:
    values = [
        {
            "name": row.label,
            "rate_raw": row.value,
            "rate_pct": parse_rate(row.value),
        }
        for row in rows
        if row.label != "无" and (row.label or row.value)
    ]
    text = " | ".join(f"{item['name']}:{item['rate_raw']}" for item in values)
    return text, json.dumps(values, ensure_ascii=False, separators=(",", ":"))


def _hierarchy(rows: list[SectionRow]) -> tuple[dict[str, str], str, str]:
    names = {
        "category_name": "",
        "chapter_name": "",
        "heading_name": "",
        "subheading_name": "",
        "hs8_name": "",
    }
    parsed: list[dict[str, str]] = []
    length_to_level = {
        2: ("chapter", "chapter_name"),
        4: ("heading", "heading_name"),
        6: ("subheading", "subheading_name"),
        8: ("hs8", "hs8_name"),
    }

    for row in rows:
        label = row.label.strip()
        value = row.value.strip()
        combined = f"{label} {value}".strip()
        category = re.fullmatch(r"第(\d+)类(?:\s+(.+))?", combined)
        if category:
            name = value or (category.group(2) or "")
            names["category_name"] = name
            parsed.append(
                {
                    "level": "category",
                    "code": category.group(1),
                    "name": name,
                    "raw_label": label,
                    "raw_value": value,
                }
            )
            continue

        code = label if re.fullmatch(r"\d{2,10}", label) else ""
        name = value
        if not code:
            match = re.fullmatch(r"(\d{2,10})\s+(.+)", combined)
            if match:
                code, name = match.groups()
        level_info = length_to_level.get(len(code))
        if code and level_info:
            level, name_field = level_info
            names[name_field] = name
        else:
            level = "other"
        parsed.append(
            {
                "level": level,
                "code": code,
                "name": name,
                "raw_label": label,
                "raw_value": value,
            }
        )

    return (
        names,
        _summary(rows),
        json.dumps(parsed, ensure_ascii=False, separators=(",", ":")),
    )


def _ciq(rows: list[SectionRow]) -> tuple[str, str, str]:
    parsed: list[dict[str, str]] = []
    for row in rows:
        code = row.label.strip()
        name = row.value.strip()
        if not re.fullmatch(r"\d{13}", code):
            match = re.fullmatch(r"(\d{13})\s+(.+)", f"{code} {name}".strip())
            if match:
                code, name = match.groups()
        if re.fullmatch(r"\d{13}", code):
            parsed.append({"code": code, "name": name})
    return (
        "|".join(item["code"] for item in parsed),
        "|".join(item["name"] for item in parsed),
        json.dumps(parsed, ensure_ascii=False, separators=(",", ":")),
    )


def _raw_sections_json(raw: RawDetailRecord) -> str:
    payload = {
        title: [{"label": row.label, "value": row.value} for row in rows]
        for title, rows in raw.sections.items()
    }
    return json.dumps(payload, ensure_ascii=False, separators=(",", ":"))


def normalize_detail(
    raw: RawDetailRecord,
    *,
    scraped_at_utc: str | None = None,
) -> dict[str, Any]:
    code = raw.code
    basic = raw.sections.get("基本信息", [])
    tax = raw.sections.get("税率信息", [])
    declarations = raw.sections.get("申报要素", [])
    supervision = raw.sections.get("监管条件", [])
    inspection = raw.sections.get("检验检疫类别", [])
    agreement = raw.sections.get("协定税率", [])
    rcep = raw.sections.get("RCEP税率", [])
    hierarchy_rows = raw.sections.get("所属章节", [])
    ciq_rows = raw.sections.get("CIQ代码(13位海关编码)", [])

    status = _first_value(basic, ("编码状态",))
    unit_raw = _first_value(tax, ("计量单位",))
    units = [part.strip() for part in re.split(r"[/／]", unit_raw) if part.strip()]
    supervision_codes, supervision_names, supervision_json = _coded_section(supervision)
    inspection_codes, inspection_names, inspection_json = _coded_section(inspection)
    agreement_text, agreement_json = _rate_section(agreement)
    rcep_text, rcep_json = _rate_section(rcep)
    hierarchy_names, hierarchy_text, hierarchy_json = _hierarchy(hierarchy_rows)
    ciq_codes, ciq_names, ciq_json = _ciq(ciq_rows)

    row: dict[str, Any] = {column: "" for column in MAIN_COLUMNS}
    row.update(
        {
            "hs_code": code,
            "chapter_code": code[:2],
            "heading_code": code[:4],
            "subheading_code": code[:6],
            "hs8_code": code[:8],
            "product_name": _first_value(basic, ("商品名称",)),
            "product_description": _first_value(basic, ("商品描述",)),
            "code_status": status,
            "is_expired": int("过期" in status),
            "updated_date": normalize_date(_first_value(basic, ("更新时间",))),
            "unit_raw": unit_raw,
            "unit_primary": units[0] if units else "",
            "unit_secondary": units[1] if len(units) > 1 else "",
            "declaration_elements_text": _summary(declarations),
            "declaration_elements_json": _rows_json(declarations),
            "supervision_codes": supervision_codes,
            "supervision_names": supervision_names,
            "supervision_json": supervision_json,
            "inspection_codes": inspection_codes,
            "inspection_names": inspection_names,
            "inspection_json": inspection_json,
            "agreement_rates_text": agreement_text,
            "agreement_rates_json": agreement_json,
            "rcep_rates_text": rcep_text,
            "rcep_rates_json": rcep_json,
            **hierarchy_names,
            "hierarchy_text": hierarchy_text,
            "hierarchy_json": hierarchy_json,
            "ciq_codes": ciq_codes,
            "ciq_names": ciq_names,
            "ciq_json": ciq_json,
            "raw_sections_json": _raw_sections_json(raw),
            "source_url": raw.source_url,
            "scraped_at_utc": scraped_at_utc
            or datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z"),
        }
    )

    for field, aliases in RATE_FIELDS:
        raw_value = _first_value(tax, aliases)
        row[f"{field}_raw"] = raw_value
        parsed = parse_rate(raw_value)
        row[f"{field}_pct"] = parsed if parsed is not None else ""

    return row


def chapter_code(record: dict[str, Any]) -> str:
    value = str(record.get("chapter_code") or str(record.get("hs_code", ""))[:2])
    if re.fullmatch(r"\d{2}", value) is None:
        raise ValueError(f"invalid chapter code: {value!r}")
    return value
