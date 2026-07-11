import json

from hs_crawler.normalize import chapter_code, normalize_date, normalize_detail, parse_rate
from hs_crawler.parser import RawDetailRecord, SectionRow


def _raw_record() -> RawDetailRecord:
    return RawDetailRecord(
        code="0101210010",
        source_url="https://www.hsbianma.com/Code/0101210010.html",
        sections={
            "基本信息": [
                SectionRow("商品编码", "0101210010"),
                SectionRow("商品名称", "改良种用濒危野马"),
                SectionRow("商品描述", "用于改良种用"),
                SectionRow("编码状态", "正常"),
                SectionRow("更新时间", "2026/4/2"),
            ],
            "税率信息": [
                SectionRow("计量单位", "千克/头"),
                SectionRow("出口税率", "0%"),
                SectionRow("出口退税税率", "9.5%"),
                SectionRow("增值税率", "9%"),
                SectionRow("最惠国税率", "0%"),
                SectionRow("进口普通税率", "20%"),
                SectionRow("消费税率", "无"),
            ],
            "申报要素": [
                SectionRow("0", "品牌类型"),
                SectionRow("1", "出口享惠情况"),
            ],
            "监管条件": [
                SectionRow("A", "入境货物通关单"),
                SectionRow("F", "濒危物种允许进口证明书"),
            ],
            "检验检疫类别": [
                SectionRow("P", "进境动植物、动植物产品检疫"),
                SectionRow("Q", "出境动植物、动植物产品检疫"),
            ],
            "协定税率": [
                SectionRow("东盟", "0%"),
                SectionRow("智利", "2.5%"),
            ],
            "RCEP税率": [SectionRow("澳大利亚", "0%")],
            "所属章节": [
                SectionRow("第1类", "活动物;动物产品"),
                SectionRow("01", "活动物"),
                SectionRow("0101", "马、驴、骡"),
                SectionRow("010121", "改良种用"),
                SectionRow("01012100", "马"),
            ],
            "CIQ代码(13位海关编码)": [
                SectionRow("0101210010999", "改良种用濒危野马")
            ],
        },
    )


def test_parse_rate_keeps_unavailable_values_empty() -> None:
    assert parse_rate("0%") == 0.0
    assert parse_rate("9.5 %") == 9.5
    assert parse_rate("无") is None
    assert parse_rate("") is None


def test_normalize_date_returns_iso_or_original_text() -> None:
    assert normalize_date("2026/4/2") == "2026-04-02"
    assert normalize_date("2026-04-02") == "2026-04-02"
    assert normalize_date("unknown") == "unknown"


def test_normalize_detail_builds_filterable_fields_and_lossless_json() -> None:
    row = normalize_detail(_raw_record(), scraped_at_utc="2026-07-11T10:00:00Z")

    assert row["hs_code"] == "0101210010"
    assert row["chapter_code"] == "01"
    assert row["heading_code"] == "0101"
    assert row["subheading_code"] == "010121"
    assert row["hs8_code"] == "01012100"
    assert row["product_name"] == "改良种用濒危野马"
    assert row["updated_date"] == "2026-04-02"
    assert row["is_expired"] == 0
    assert row["unit_primary"] == "千克"
    assert row["unit_secondary"] == "头"
    assert row["export_rebate_rate_pct"] == 9.5
    assert row["consumption_tax_rate_pct"] == ""
    assert row["supervision_codes"] == "A|F"
    assert row["inspection_codes"] == "P|Q"
    assert row["category_name"] == "活动物;动物产品"
    assert row["chapter_name"] == "活动物"
    assert row["ciq_codes"] == "0101210010999"
    assert row["declaration_elements_text"] == "0:品牌类型 | 1:出口享惠情况"

    raw_sections = json.loads(row["raw_sections_json"])
    assert raw_sections["协定税率"][1] == {"label": "智利", "value": "2.5%"}
    assert json.loads(row["agreement_rates_json"])[1]["rate_pct"] == 2.5
    assert row["source_url"].endswith("/Code/0101210010.html")
    assert row["scraped_at_utc"] == "2026-07-11T10:00:00Z"


def test_expired_status_and_chapter_code_are_derived_from_code() -> None:
    raw = _raw_record()
    raw.sections["基本信息"][3] = SectionRow("编码状态", "过期")

    row = normalize_detail(raw, scraped_at_utc="2026-07-11T10:00:00Z")

    assert row["is_expired"] == 1
    assert chapter_code(row) == "01"
