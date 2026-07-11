from __future__ import annotations

import asyncio
from dataclasses import dataclass
import logging
from pathlib import Path
import re
from typing import Awaitable, Callable, Protocol
from urllib.parse import parse_qs, urlencode, urljoin, urlparse, urlunparse
from urllib.robotparser import RobotFileParser

from bs4 import BeautifulSoup

from .browser import CrawlBlockedError, FetchError, FetchResult
from .config import CrawlConfig
from .exporter import ExportSummary, export_workbook
from .normalize import normalize_detail
from .parser import ParseError, parse_chapter_links, parse_detail_page, parse_search_page
from .store import CheckpointStore, PageTask


LOGGER = logging.getLogger(__name__)
DETAIL_PATH_RE = re.compile(r"^/Code/(\d{10})\.html$", re.IGNORECASE)
SEARCH_PATH_RE = re.compile(r"^/search(?:/(\d+))?/?$", re.IGNORECASE)


class Fetcher(Protocol):
    async def fetch(self, url: str, *, wait_selector: str | None = None) -> FetchResult: ...


Exporter = Callable[[CheckpointStore, Path], Awaitable[ExportSummary]]


class RobotsDeniedError(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class CrawlSummary:
    records: int
    errors: int
    search_done: int
    detail_done: int
    chapter_sheets: int


def _same_origin(url: str, base_url: str) -> bool:
    candidate = urlparse(urljoin(base_url + "/", url))
    base = urlparse(base_url)
    return candidate.scheme == base.scheme and candidate.netloc.lower() == base.netloc.lower()


def canonical_search_url(url: str, base_url: str) -> tuple[str, str]:
    absolute = urljoin(base_url + "/", url)
    if not _same_origin(absolute, base_url):
        raise ValueError(f"off-origin search URL: {absolute}")
    parsed = urlparse(absolute)
    match = SEARCH_PATH_RE.fullmatch(parsed.path)
    keywords = parse_qs(parsed.query).get("keywords", [])
    if match is None or len(keywords) != 1 or re.fullmatch(r"\d{2}", keywords[0]) is None:
        raise ValueError(f"invalid search URL: {absolute}")
    chapter = keywords[0]
    page = int(match.group(1) or "1")
    path = "/search" if page <= 1 else f"/Search/{page}"
    canonical = urlunparse((parsed.scheme, parsed.netloc, path, "", urlencode({"keywords": chapter}), ""))
    return canonical, chapter


def canonical_detail_url(url: str, base_url: str) -> tuple[str, str]:
    absolute = urljoin(base_url + "/", url)
    if not _same_origin(absolute, base_url):
        raise ValueError(f"off-origin detail URL: {absolute}")
    parsed = urlparse(absolute)
    match = DETAIL_PATH_RE.fullmatch(parsed.path)
    if match is None:
        raise ValueError(f"invalid detail URL: {absolute}")
    code = match.group(1)
    canonical = urlunparse((parsed.scheme, parsed.netloc, f"/Code/{code}.html", "", "", ""))
    return canonical, code


class HSCrawler:
    def __init__(
        self,
        config: CrawlConfig,
        store: CheckpointStore,
        fetcher: Fetcher,
        *,
        exporter: Exporter = export_workbook,
    ) -> None:
        self.config = config
        self.store = store
        self.fetcher = fetcher
        self._exporter = exporter
        self._semaphore = asyncio.Semaphore(config.concurrency)
        self._export_lock = asyncio.Lock()
        self._completed_details = 0
        self._last_export_count = 0

    async def run(self) -> CrawlSummary:
        if self.config.respect_robots:
            await self._check_robots()

        export_summary = ExportSummary(0, 0, 0)
        try:
            await self._seed_search_pages()
            await self._crawl_search_pages()
            await self._crawl_detail_pages()
        finally:
            export_summary = await asyncio.shield(
                self._exporter(self.store, self.config.output_dir)
            )

        stats = await self.store.stats()
        errors = sum(value for key, value in stats.items() if key.endswith("_error"))
        return CrawlSummary(
            records=stats.get("records", 0),
            errors=errors,
            search_done=stats.get("search_done", 0),
            detail_done=stats.get("detail_done", 0),
            chapter_sheets=export_summary.chapter_sheets,
        )

    async def _fetch(self, url: str, *, wait_selector: str | None = None) -> FetchResult:
        async with self._semaphore:
            return await self.fetcher.fetch(url, wait_selector=wait_selector)

    async def _check_robots(self) -> None:
        robots_url = f"{self.config.base_url}/robots.txt"
        try:
            result = await self._fetch(robots_url)
        except FetchError as exc:
            if exc.status == 404:
                return
            raise
        text = BeautifulSoup(result.html, "lxml").get_text("\n", strip=True)
        parser = RobotFileParser()
        parser.set_url(robots_url)
        parser.parse(text.splitlines())
        checks = (
            f"{self.config.base_url}/",
            f"{self.config.base_url}/search?keywords=01",
            f"{self.config.base_url}/Code/0101210010.html",
        )
        agent = "HSBianmaResearchCrawler"
        for url in checks:
            if not parser.can_fetch(agent, url):
                raise RobotsDeniedError(f"robots.txt disallows {urlparse(url).path}")

    async def _seed_search_pages(self) -> None:
        if self.config.chapters is not None:
            chapter_urls = [
                (f"{self.config.base_url}/search?keywords={chapter}", "search", chapter)
                for chapter in self.config.chapters
            ]
        else:
            home_url = f"{self.config.base_url}/"
            result = await self._fetch(home_url, wait_selector="#tree1 a.edit")
            links = parse_chapter_links(result.html, self.config.base_url)
            if not links:
                raise ParseError("home page did not expose any chapter links")
            chapter_urls = []
            for link in links:
                canonical, chapter = canonical_search_url(link, self.config.base_url)
                chapter_urls.append((canonical, "search", chapter))
        inserted = await self.store.enqueue_pages(chapter_urls)
        LOGGER.info("Seeded %s new chapter search pages", inserted)

    async def _crawl_search_pages(self) -> None:
        batch_size = max(self.config.concurrency * 4, 4)
        retry_passes = 0
        while True:
            tasks = await self.store.pending_pages("search", limit=batch_size)
            if tasks:
                await self._run_batch(tasks, self._process_search_page)
                continue
            if retry_passes >= 1:
                return
            requeued = await self.store.requeue_errors("search")
            if not requeued:
                return
            retry_passes += 1
            LOGGER.warning(
                "Requeued %s search errors for in-run retry pass %s",
                requeued,
                retry_passes,
            )

    async def _crawl_detail_pages(self) -> None:
        batch_size = max(self.config.concurrency * 4, 4)
        remaining = self.config.detail_limit
        while remaining is None or remaining > 0:
            limit = batch_size if remaining is None else min(batch_size, remaining)
            tasks = await self.store.pending_pages("detail", limit=limit)
            if not tasks:
                return
            await self._run_batch(tasks, self._process_detail_page)
            if remaining is not None:
                remaining -= len(tasks)

    async def _run_batch(
        self,
        tasks: list[PageTask],
        handler: Callable[[PageTask], Awaitable[None]],
    ) -> None:
        results = await asyncio.gather(*(handler(task) for task in tasks), return_exceptions=True)
        for result in results:
            if isinstance(result, BaseException):
                raise result

    async def _process_search_page(self, task: PageTask) -> None:
        await self.store.mark_in_progress(task.url)
        try:
            is_chapter_entry = urlparse(task.url).path.lower().rstrip("/") == "/search"
            result = await self._fetch(
                task.url,
                wait_selector="tr.result-grid" if is_chapter_entry else None,
            )
            parsed = parse_search_page(result.html, self.config.base_url)
            if is_chapter_entry and not parsed.detail_urls:
                raise ParseError(
                    f"chapter entry search page exposed no detail links: {task.url}"
                )
            detail_pages: list[tuple[str, str, str]] = []
            for url in parsed.detail_urls:
                canonical, code = canonical_detail_url(url, self.config.base_url)
                detail_pages.append((canonical, "detail", code[:2]))
            await self.store.enqueue_pages(detail_pages)

            pagination: list[tuple[str, str, str]] = []
            for url in parsed.pagination_urls:
                canonical, chapter = canonical_search_url(url, self.config.base_url)
                pagination.append((canonical, "search", chapter))
            await self.store.enqueue_pages(pagination)
            await self.store.mark_done(task.url, http_status=result.status)
            LOGGER.info(
                "Search done %s: %s detail links, %s page links",
                task.url,
                len(parsed.detail_urls),
                len(parsed.pagination_urls),
            )
        except CrawlBlockedError as exc:
            await self.store.mark_error(task.url, str(exc), http_status=exc.status)
            raise
        except (FetchError, ParseError, ValueError) as exc:
            await self.store.mark_error(
                task.url,
                str(exc),
                http_status=exc.status if isinstance(exc, FetchError) else 0,
            )
            LOGGER.error("Search failed %s: %s", task.url, exc)

    async def _process_detail_page(self, task: PageTask) -> None:
        await self.store.mark_in_progress(task.url)
        try:
            result = await self._fetch(task.url, wait_selector="#code-info")
            raw = parse_detail_page(result.html, task.url)
            _, expected_code = canonical_detail_url(task.url, self.config.base_url)
            if raw.code != expected_code:
                raise ParseError(
                    f"DOM code {raw.code} does not match URL code {expected_code}"
                )
            record = normalize_detail(raw)
            await self.store.save_record(record)
            neighbor_pages: list[tuple[str, str, str]] = []
            for url in raw.neighbor_urls:
                canonical, neighbor_code = canonical_detail_url(url, self.config.base_url)
                if neighbor_code[:2] == expected_code[:2]:
                    neighbor_pages.append((canonical, "detail", neighbor_code[:2]))
            await self.store.enqueue_pages(neighbor_pages)
            await self.store.mark_done(task.url, http_status=result.status)
            self._completed_details += 1
            LOGGER.info("Detail done %s", expected_code)
            await self._maybe_periodic_export()
        except CrawlBlockedError as exc:
            await self.store.mark_error(task.url, str(exc), http_status=exc.status)
            raise
        except (FetchError, ParseError, ValueError) as exc:
            await self.store.mark_error(
                task.url,
                str(exc),
                http_status=exc.status if isinstance(exc, FetchError) else 0,
            )
            LOGGER.error("Detail failed %s: %s", task.url, exc)

    async def _maybe_periodic_export(self) -> None:
        if self._completed_details - self._last_export_count < self.config.export_every:
            return
        async with self._export_lock:
            if self._completed_details - self._last_export_count < self.config.export_every:
                return
            summary = await self._exporter(self.store, self.config.output_dir)
            self._last_export_count = self._completed_details
            LOGGER.info(
                "Periodic export: %s records across %s chapter sheets",
                summary.record_count,
                summary.chapter_sheets,
            )
