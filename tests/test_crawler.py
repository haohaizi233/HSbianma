import asyncio
from collections import Counter
from pathlib import Path

import pytest

from hs_crawler.browser import FetchResult
from hs_crawler.config import CrawlConfig
from hs_crawler.crawler import HSCrawler, RobotsDeniedError
from hs_crawler.exporter import ExportSummary
from hs_crawler.store import CheckpointStore


BASE = "https://www.hsbianma.com"


async def _fake_export(store: CheckpointStore, output_dir: Path) -> ExportSummary:
    return ExportSummary(1, len(await store.records()), len(await store.errors()))


class FakeFetcher:
    def __init__(self, pages: dict[str, str]) -> None:
        self.pages = pages
        self.calls: list[str] = []
        self.requests: list[tuple[str, str | None]] = []

    async def fetch(self, url: str, *, wait_selector: str | None = None) -> FetchResult:
        self.calls.append(url)
        self.requests.append((url, wait_selector))
        if url not in self.pages:
            raise AssertionError(f"unexpected URL: {url}")
        return FetchResult(
            requested_url=url,
            final_url=url,
            status=200,
            html=self.pages[url],
            elapsed_seconds=0.01,
        )


class DelayedFetcher(FakeFetcher):
    def __init__(self, pages: dict[str, str], delays: dict[str, float]) -> None:
        super().__init__(pages)
        self.delays = delays

    async def fetch(self, url: str, *, wait_selector: str | None = None) -> FetchResult:
        await asyncio.sleep(self.delays.get(url, 0.0))
        return await super().fetch(url, wait_selector=wait_selector)


class FailFirstSearchFetcher(FakeFetcher):
    def __init__(self, pages: dict[str, str], search_url: str) -> None:
        super().__init__(pages)
        self.search_url = search_url
        self.search_attempts = 0

    async def fetch(self, url: str, *, wait_selector: str | None = None) -> FetchResult:
        if url == self.search_url:
            self.search_attempts += 1
            if self.search_attempts == 1:
                self.calls.append(url)
                self.requests.append((url, wait_selector))
                return FetchResult(
                    requested_url=url,
                    final_url=BASE,
                    status=200,
                    html='<html><title>home</title><div id="tree1"></div></html>',
                    elapsed_seconds=0.01,
                )
        return await super().fetch(url, wait_selector=wait_selector)


def _search_page(detail_codes: list[str], pagination: list[str]) -> str:
    detail_rows = "".join(
        f'<tr class="result-grid"><td>{code}</td><td><a href="/Code/{code}.html">details</a></td></tr>'
        for code in detail_codes
    )
    page_links = "".join(f'<a href="{url}">page</a>' for url in pagination)
    return f'<table class="result">{detail_rows}</table><div id="pagination">{page_links}</div>'


def _detail_page(code: str, name: str, *, next_code: str | None = None) -> str:
    neighbor = (
        f'<div class="cbox"><span>下一条:<a href="/Code/{next_code}.html">next</a></span></div>'
        if next_code
        else ""
    )
    return f"""
    <div id="code-info">
      <h3 class="ch3">基本信息</h3>
      <div class="cbox"><table>
        <tr><td>商品编码</td><td>{code}</td></tr>
        <tr><td>商品名称</td><td>{name}</td></tr>
        <tr><td>编码状态</td><td>正常</td></tr>
      </table></div>
    </div>
    {neighbor}
    """


@pytest.mark.asyncio
async def test_crawler_discovers_home_pagination_then_crawls_unique_details(tmp_path) -> None:
    home = f"{BASE}/"
    search_1 = f"{BASE}/search?keywords=01"
    search_2 = f"{BASE}/Search/2?keywords=01"
    detail_1 = f"{BASE}/Code/0101210010.html"
    detail_2 = f"{BASE}/Code/0101210090.html"
    pages = {
        home: '<div id="tree1"><a class="edit" href="/search?keywords=01">01</a></div>',
        search_1: _search_page(["0101210010"], ["/search?keywords=01", "/Search/2?keywords=01"]),
        search_2: _search_page(["0101210090"], ["/Search/2?keywords=01"]),
        detail_1: _detail_page("0101210010", "horse one"),
        detail_2: _detail_page("0101210090", "horse two"),
    }
    fetcher = FakeFetcher(pages)
    export_calls = 0

    async def fake_export(store: CheckpointStore, output_dir: Path) -> ExportSummary:
        nonlocal export_calls
        export_calls += 1
        return ExportSummary(1, len(await store.records()), len(await store.errors()))

    config = CrawlConfig(
        output_dir=tmp_path / "data",
        respect_robots=False,
        concurrency=2,
        export_every=1,
    )
    store = CheckpointStore(config.database_path)
    await store.open()

    summary = await HSCrawler(config, store, fetcher, exporter=fake_export).run()

    assert summary.records == 2
    assert [row["hs_code"] for row in await store.records()] == ["0101210010", "0101210090"]
    assert Counter(fetcher.calls) == Counter(pages.keys())
    assert max(fetcher.calls.index(search_1), fetcher.calls.index(search_2)) < min(
        fetcher.calls.index(detail_1), fetcher.calls.index(detail_2)
    )
    assert export_calls >= 3
    await store.close()


@pytest.mark.asyncio
async def test_crawler_resume_does_not_refetch_completed_pages(tmp_path) -> None:
    config = CrawlConfig(
        output_dir=tmp_path / "data",
        chapters=("01",),
        respect_robots=False,
    )
    store = CheckpointStore(config.database_path)
    await store.open()
    search = f"{BASE}/search?keywords=01"
    detail = f"{BASE}/Code/0101210010.html"
    await store.enqueue_page(search, "search", "01")
    await store.mark_in_progress(search)
    await store.mark_done(search)
    await store.enqueue_page(detail, "detail", "01")
    await store.mark_in_progress(detail)
    await store.save_record(
        {
            "hs_code": "0101210010",
            "chapter_code": "01",
            "product_name": "horse",
            "source_url": detail,
            "scraped_at_utc": "2026-07-11T10:00:00Z",
        }
    )
    await store.mark_done(detail)
    fetcher = FakeFetcher({})

    summary = await HSCrawler(config, store, fetcher, exporter=_fake_export).run()

    assert summary.records == 1
    assert fetcher.calls == []
    await store.close()


@pytest.mark.asyncio
async def test_crawler_records_error_when_dom_code_disagrees_with_url(tmp_path) -> None:
    config = CrawlConfig(
        output_dir=tmp_path / "data",
        chapters=("01",),
        respect_robots=False,
    )
    search = f"{BASE}/search?keywords=01"
    detail = f"{BASE}/Code/0101210010.html"
    fetcher = FakeFetcher(
        {
            search: _search_page(["0101210010"], []),
            detail: _detail_page("0101210090", "wrong code"),
        }
    )
    store = CheckpointStore(config.database_path)
    await store.open()

    summary = await HSCrawler(config, store, fetcher, exporter=_fake_export).run()

    errors = await store.errors()
    assert summary.records == 0
    assert len(errors) == 1
    assert "does not match URL code" in errors[0]["error"]
    await store.close()


@pytest.mark.asyncio
async def test_robots_denial_stops_before_home_discovery(tmp_path) -> None:
    robots = f"{BASE}/robots.txt"
    fetcher = FakeFetcher(
        {
            robots: "<html><body><pre>User-agent: *\nDisallow: /search</pre></body></html>"
        }
    )
    config = CrawlConfig(output_dir=tmp_path / "data", respect_robots=True)
    store = CheckpointStore(config.database_path)
    await store.open()

    with pytest.raises(RobotsDeniedError, match="/search"):
        await HSCrawler(config, store, fetcher, exporter=_fake_export).run()

    assert fetcher.calls == [robots]
    await store.close()


@pytest.mark.asyncio
async def test_deep_search_home_fallback_is_a_completed_boundary_not_a_retry_error(tmp_path) -> None:
    config = CrawlConfig(
        output_dir=tmp_path / "data",
        chapters=("03",),
        respect_robots=False,
    )
    search = f"{BASE}/search?keywords=03"
    deep = f"{BASE}/Search/21?keywords=03"
    detail = f"{BASE}/Code/0301110010.html"
    fetcher = FakeFetcher(
        {
            search: _search_page(["0301110010"], ["/Search/21?keywords=03"]),
            deep: '<html><title>HS Code_HS编码查询</title><div id="tree1"></div></html>',
            detail: _detail_page("0301110010", "ornamental fish"),
        }
    )
    store = CheckpointStore(config.database_path)
    await store.open()

    summary = await HSCrawler(config, store, fetcher, exporter=_fake_export).run()

    assert summary.search_done == 2
    assert summary.errors == 0
    assert (deep, None) in fetcher.requests
    await store.close()


@pytest.mark.asyncio
async def test_chapter_entry_without_result_rows_is_not_marked_done(tmp_path) -> None:
    config = CrawlConfig(
        output_dir=tmp_path / "data",
        chapters=("53",),
        respect_robots=False,
    )
    search = f"{BASE}/search?keywords=53"
    fetcher = FakeFetcher(
        {search: '<html><title>home</title><div id="tree1"></div></html>'}
    )
    store = CheckpointStore(config.database_path)
    await store.open()

    summary = await HSCrawler(config, store, fetcher, exporter=_fake_export).run()

    errors = await store.errors()
    assert summary.search_done == 0
    assert summary.records == 0
    assert len(errors) == 1
    assert "no detail links" in errors[0]["error"]
    assert fetcher.requests == [
        (search, "tr.result-grid"),
        (search, "tr.result-grid"),
    ]
    await store.close()


@pytest.mark.asyncio
async def test_search_errors_are_requeued_once_during_the_same_run(tmp_path) -> None:
    config = CrawlConfig(
        output_dir=tmp_path / "data",
        chapters=("53",),
        respect_robots=False,
    )
    search = f"{BASE}/search?keywords=53"
    detail = f"{BASE}/Code/5301100000.html"
    fetcher = FailFirstSearchFetcher(
        {
            search: _search_page(["5301100000"], []),
            detail: _detail_page("5301100000", "recovered"),
        },
        search,
    )
    store = CheckpointStore(config.database_path)
    await store.open()

    summary = await HSCrawler(config, store, fetcher, exporter=_fake_export).run()

    assert summary.errors == 0
    assert summary.records == 1
    assert fetcher.search_attempts == 2
    await store.close()


@pytest.mark.asyncio
async def test_detail_neighbor_link_extends_discovery_past_search_page_cap(tmp_path) -> None:
    config = CrawlConfig(
        output_dir=tmp_path / "data",
        chapters=("03",),
        respect_robots=False,
    )
    search = f"{BASE}/search?keywords=03"
    detail_1 = f"{BASE}/Code/0301110010.html"
    detail_2 = f"{BASE}/Code/0301110090.html"
    fetcher = FakeFetcher(
        {
            search: _search_page(["0301110010"], []),
            detail_1: _detail_page("0301110010", "first", next_code="0301110090"),
            detail_2: _detail_page("0301110090", "second"),
        }
    )
    store = CheckpointStore(config.database_path)
    await store.open()

    summary = await HSCrawler(config, store, fetcher, exporter=_fake_export).run()

    assert summary.records == 2
    assert detail_2 in fetcher.calls
    await store.close()


@pytest.mark.asyncio
async def test_detail_loop_waits_for_slow_neighbor_discovery_before_finishing(tmp_path) -> None:
    config = CrawlConfig(
        output_dir=tmp_path / "data",
        chapters=("03",),
        respect_robots=False,
        concurrency=2,
    )
    search = f"{BASE}/search?keywords=03"
    detail_1 = f"{BASE}/Code/0301110010.html"
    detail_2 = f"{BASE}/Code/0301110090.html"
    detail_3 = f"{BASE}/Code/0301190000.html"
    fetcher = DelayedFetcher(
        {
            search: _search_page(["0301110010", "0301110090"], []),
            detail_1: _detail_page("0301110010", "fast"),
            detail_2: _detail_page("0301110090", "slow", next_code="0301190000"),
            detail_3: _detail_page("0301190000", "discovered late"),
        },
        {detail_2: 0.05},
    )
    store = CheckpointStore(config.database_path)
    await store.open()

    summary = await HSCrawler(config, store, fetcher, exporter=_fake_export).run()

    assert summary.records == 3
    assert detail_3 in fetcher.calls
    await store.close()
