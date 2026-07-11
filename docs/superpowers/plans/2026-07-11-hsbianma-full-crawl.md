# HSBianma Full Crawl Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build and start a restartable, Chrome-rendered full-site crawler that exports all third-level HS-code data to one analysis-ready Excel workbook with a total sheet and one sheet per two-digit chapter.

**Architecture:** A Python package separates deterministic DOM parsing and normalization from browser I/O, persistence, orchestration, and export. SQLite is the canonical checkpoint; CSV is an atomic derived artifact refreshed during and after crawling.

**Tech Stack:** Python 3.13, asyncio, Playwright 1.57, Google Chrome 149, BeautifulSoup 4, SQLite, pytest, pytest-asyncio.

## Global Constraints

- Render all in-scope pages with the installed Google Chrome engine.
- Random delay must remain between 1 and 2 seconds before navigation.
- A token bucket and `asyncio.Semaphore` must both enforce request pressure.
- Do not bypass authentication, CAPTCHA, persistent access denials, or `robots.txt` restrictions.
- Export only third-level business data; discovery pages remain internal state, and every chapter has its own workbook sheet after `总表`.
- Preserve all third-level DOM values losslessly in raw JSON even when also normalized.

---

### Task 1: Package skeleton and deterministic DOM parsing

**Files:**
- Create: `pyproject.toml`
- Create: `src/hs_crawler/__init__.py`
- Create: `src/hs_crawler/parser.py`
- Create: `tests/test_parser.py`

**Interfaces:**
- Produces: `parse_chapter_links(html, base_url) -> list[str]`
- Produces: `parse_search_page(html, base_url) -> SearchPage`
- Produces: `parse_detail_page(html, source_url) -> RawDetailRecord`

- [ ] **Step 1: Write failing parser tests** for rendered chapter links, paginated search links, ten-digit detail URL extraction, empty sections, and multi-row sections using representative HTML fixtures.
- [ ] **Step 2: Run `python -m pytest tests/test_parser.py -q`** and confirm imports fail because `hs_crawler.parser` does not exist.
- [ ] **Step 3: Implement minimal parser dataclasses and BeautifulSoup selectors** using `#tree1 a.edit`, `tr.result-grid`, `#pagination`, and `#code-info h3.ch3` sibling tables.
- [ ] **Step 4: Run `python -m pytest tests/test_parser.py -q`** and require all parser tests to pass.

### Task 2: Normalized record and CSV schemas

**Files:**
- Create: `src/hs_crawler/normalize.py`
- Create: `tests/test_normalize.py`

**Interfaces:**
- Consumes: `RawDetailRecord`
- Produces: `normalize_detail(raw) -> dict[str, object]`
- Produces: `chapter_code(record) -> str`

- [ ] **Step 1: Write failing normalization tests** for dates, rate percentages, split units, expiration status, hierarchy levels, condition codes, CIQ entries, and raw JSON preservation.
- [ ] **Step 2: Run `python -m pytest tests/test_normalize.py -q`** and confirm missing-module failure.
- [ ] **Step 3: Implement a stable wide schema** with Chinese-label aliases, filter-friendly summaries, and lossless JSON serialization using `ensure_ascii=False`.
- [ ] **Step 4: Run `python -m pytest tests/test_normalize.py -q`** and require all normalization tests to pass.

### Task 3: Token bucket, retry policy, and browser fetcher

**Files:**
- Create: `src/hs_crawler/rate_limit.py`
- Create: `src/hs_crawler/browser.py`
- Create: `tests/test_rate_limit.py`
- Create: `tests/test_browser_policy.py`

**Interfaces:**
- Produces: `AsyncTokenBucket.acquire() -> None`
- Produces: `classify_http_status(status) -> ResponseAction`
- Produces: `BrowserFetcher.fetch(url, wait_selector=None) -> FetchResult`

- [ ] **Step 1: Write failing asynchronous tests** using an injected clock/sleeper for capacity, refill, 1-2 second jitter bounds, retryable statuses, permanent statuses, and block statuses.
- [ ] **Step 2: Run `python -m pytest tests/test_rate_limit.py tests/test_browser_policy.py -q`** and confirm missing-module failures.
- [ ] **Step 3: Implement token accounting and response policy**, then build the Playwright fetcher with Chrome contexts, context-stable UA profiles, semaphore, interception, retries, backoff, `Retry-After`, and challenge detection.
- [ ] **Step 4: Run the two test files** and require all tests to pass without live network access.

### Task 4: SQLite checkpoint and atomic CSV export

**Files:**
- Create: `src/hs_crawler/store.py`
- Create: `src/hs_crawler/exporter.py`
- Create: `tests/test_store.py`
- Create: `tests/test_exporter.py`

**Interfaces:**
- Produces: `CheckpointStore.enqueue_page`, `pending_pages`, `mark_done`, `mark_error`, `save_record`, `iter_records`, and `stats`
- Produces: `export_workbook(store, output_dir) -> ExportSummary`

- [ ] **Step 1: Write failing tests** for URL deduplication, reset of interrupted work, unique records, attempt tracking, atomic replacement, 25 Chinese headers, `总表` first, one sheet per chapter, chapter-prefix validation, and errors CSV.
- [ ] **Step 2: Run `python -m pytest tests/test_store.py tests/test_exporter.py -q`** and confirm missing-module failures.
- [ ] **Step 3: Implement SQLite transactions guarded by an async lock** and deterministic CSV writers that replace temporary files atomically.
- [ ] **Step 4: Run the two test files** and require all tests to pass.

### Task 5: Crawl orchestration and CLI

**Files:**
- Create: `src/hs_crawler/config.py`
- Create: `src/hs_crawler/crawler.py`
- Create: `src/hs_crawler/cli.py`
- Create: `src/hs_crawler/__main__.py`
- Create: `tests/test_crawler.py`
- Create: `README.md`

**Interfaces:**
- Consumes: parser, normalizer, browser fetcher, checkpoint store, and exporter interfaces.
- Produces: `HSCrawler.run() -> CrawlSummary`
- Produces: `python -m hs_crawler crawl|export|status`

- [ ] **Step 1: Write failing orchestration tests** with a deterministic fake fetcher for pagination loops, discovery/detail phase separation, resume behavior, code mismatch rejection, and periodic export.
- [ ] **Step 2: Run `python -m pytest tests/test_crawler.py -q`** and confirm missing-module failure.
- [ ] **Step 3: Implement queue workers with explicit `asyncio.Semaphore` use**, structured logging, graceful Ctrl+C export, CLI validation, and concise operator documentation.
- [ ] **Step 4: Run `python -m pytest -q`** and require the full suite to pass.

### Task 6: Live Chrome verification and full crawl launch

**Files:**
- Runtime output: `data/checkpoint.sqlite3`
- Runtime output: `data/hs_codes.xlsx`
- Runtime output: `logs/crawler.log`

**Interfaces:**
- Consumes: `python -m hs_crawler crawl` CLI.
- Produces: a resumable running crawl and continuously refreshed CSV artifacts.

- [ ] **Step 1: Run a live smoke crawl** with chapter `01`, two details, concurrency 2, and the installed Chrome executable.
- [ ] **Step 2: Validate smoke output** by checking the `总表`/`01` sheet order, Chinese headers, code uniqueness, ten-digit text values, non-empty raw section JSON, and checkpoint counts.
- [ ] **Step 3: Run the full test suite again** after live-site findings and fix any regression through a failing test first.
- [ ] **Step 4: Start the full crawl** with default 1-2 second delay, token rate, concurrency 3, periodic export, and hidden background process logging.
- [ ] **Step 5: Confirm continued progress** from both SQLite statistics and log growth before handing off the monitor/status commands.
