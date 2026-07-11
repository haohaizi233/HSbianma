from __future__ import annotations

from dataclasses import dataclass, field
import re
from urllib.parse import parse_qs, urljoin, urlparse

from bs4 import BeautifulSoup, Tag


DETAIL_PATH_RE = re.compile(r"^/Code/(\d{10})\.html$", re.IGNORECASE)
SEARCH_PATH_RE = re.compile(r"^/search(?:/\d+)?/?$", re.IGNORECASE)


class ParseError(ValueError):
    pass


@dataclass(frozen=True, slots=True)
class SectionRow:
    label: str
    value: str


@dataclass(slots=True)
class SearchPage:
    detail_urls: list[str]
    pagination_urls: list[str]


@dataclass(slots=True)
class RawDetailRecord:
    code: str
    source_url: str
    sections: dict[str, list[SectionRow]]
    neighbor_urls: list[str] = field(default_factory=list)


def _clean_text(node: Tag) -> str:
    return " ".join(node.get_text(" ", strip=True).split())


def _unique(items: list[str]) -> list[str]:
    return list(dict.fromkeys(items))


def _is_two_digit_search_url(url: str) -> bool:
    parsed = urlparse(url)
    if not SEARCH_PATH_RE.fullmatch(parsed.path):
        return False
    keywords = parse_qs(parsed.query).get("keywords", [])
    return len(keywords) == 1 and re.fullmatch(r"\d{2}", keywords[0]) is not None


def parse_chapter_links(html: str, base_url: str) -> list[str]:
    soup = BeautifulSoup(html, "lxml")
    urls: list[str] = []
    for anchor in soup.select("#tree1 a.edit[href]"):
        url = urljoin(base_url, str(anchor["href"]))
        if _is_two_digit_search_url(url):
            urls.append(url)
    return _unique(urls)


def parse_search_page(html: str, base_url: str) -> SearchPage:
    soup = BeautifulSoup(html, "lxml")
    detail_urls: list[str] = []
    for anchor in soup.select("tr.result-grid a[href]"):
        url = urljoin(base_url, str(anchor["href"]))
        if DETAIL_PATH_RE.fullmatch(urlparse(url).path):
            detail_urls.append(url)

    pagination_urls: list[str] = []
    for anchor in soup.select("#pagination a[href]"):
        url = urljoin(base_url, str(anchor["href"]))
        if _is_two_digit_search_url(url):
            pagination_urls.append(url)

    return SearchPage(
        detail_urls=_unique(detail_urls),
        pagination_urls=_unique(pagination_urls),
    )


def _section_rows(container: Tag) -> list[SectionRow]:
    rows: list[SectionRow] = []
    for table_row in container.select("tr"):
        cells = table_row.find_all("td", recursive=False)
        if not cells:
            cells = table_row.find_all("td")
        if not cells:
            continue
        label = _clean_text(cells[0])
        value = " ".join(_clean_text(cell) for cell in cells[1:]).strip()
        if label or value:
            rows.append(SectionRow(label=label, value=value))
    return rows


def _code_from_sections(sections: dict[str, list[SectionRow]]) -> str | None:
    for row in sections.get("基本信息", []):
        if row.label == "商品编码":
            digits = re.sub(r"\D", "", row.value)
            if len(digits) == 10:
                return digits
    return None


def parse_detail_page(html: str, source_url: str) -> RawDetailRecord:
    soup = BeautifulSoup(html, "lxml")
    root = soup.select_one("#code-info")
    if root is None:
        raise ParseError("detail page is missing #code-info")

    sections: dict[str, list[SectionRow]] = {}
    for heading in root.select("h3.ch3"):
        title = _clean_text(heading)
        section = heading.find_next_sibling("div", class_="cbox")
        if not title or section is None:
            continue
        sections.setdefault(title, []).extend(_section_rows(section))

    code = _code_from_sections(sections)
    if code is None:
        match = DETAIL_PATH_RE.fullmatch(urlparse(source_url).path)
        code = match.group(1) if match else None
    if code is None:
        raise ParseError("detail page does not contain a valid ten-digit code")

    source = urlparse(source_url)
    neighbor_urls: list[str] = []
    for anchor in soup.select('a[href]'):
        candidate = urljoin(source_url, str(anchor["href"]))
        parsed = urlparse(candidate)
        if parsed.scheme != source.scheme or parsed.netloc.lower() != source.netloc.lower():
            continue
        if candidate == source_url or DETAIL_PATH_RE.fullmatch(parsed.path) is None:
            continue
        neighbor_urls.append(candidate)

    return RawDetailRecord(
        code=code,
        source_url=source_url,
        sections=sections,
        neighbor_urls=_unique(neighbor_urls),
    )
