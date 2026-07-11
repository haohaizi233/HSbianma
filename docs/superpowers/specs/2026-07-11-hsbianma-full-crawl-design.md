# HSBianma Full Crawl Design

## Goal

Build a restartable crawler that discovers every public HS code detail page through the site's own three-level navigation, renders every fetched page in the installed Google Chrome engine, and exports all third-level data to one analysis-ready Excel workbook whose first sheet is the total table and whose remaining sheets are two-digit chapters.

## Scope

- Level 1 is used only to discover chapter search links.
- Level 2 is used only to discover paginated `/Code/{10-digit-code}.html` links.
- Level 3 is the only business data exported.
- Public pages are in scope. Authentication, CAPTCHA solving, access-control bypass, and proxy rotation are out of scope.
- `robots.txt` is checked at startup. The crawler stops rather than bypassing a challenge page or a persistent 401/403 response.

## Chosen Approach

Use Python 3.13, `asyncio`, Playwright, and the installed Chrome executable. Every page is loaded through a real Chrome `Page` and parsed from `page.content()`. This is slower than direct HTTP, but it keeps discovery and detail extraction faithful to the rendered DOM.

Rejected alternatives:

1. Direct HTTP requests are faster but do not meet the real-browser requirement.
2. A browser session followed by API-request fetching uses browser cookies but does not render each third-level page.

## Architecture

The crawler is split into independently testable units:

- `parser.py`: extracts chapter links, search pagination, detail links, and structured detail sections.
- `normalize.py`: converts raw section rows into stable columns, normalized rates/dates, hierarchy fields, and lossless JSON cells.
- `rate_limit.py`: implements an asynchronous token bucket plus configurable 1-2 second random request delay.
- `browser.py`: owns Chrome contexts/pages, realistic context profiles, coherent user-agent rotation, request interception, response classification, retry/backoff, and challenge detection.
- `store.py`: persists page status, attempts, errors, and complete detail records in SQLite for deduplication and restartability.
- `exporter.py`: writes one `.xlsx` workbook through `@oai/artifact-tool`, ordered as `总表`, then one sheet per two-digit chapter.
- `crawler.py`: orchestrates discovery and detail workers with `asyncio.Semaphore`.
- `cli.py`: exposes crawl, resume, export-only, headful, pacing, concurrency, and output options.

## Browser And Pacing Behavior

- Launch `C:\Program Files\Google\Chrome\Application\chrome.exe` through Playwright Chromium.
- Use isolated crawler profiles, never the user's personal Chrome profile.
- Keep one user agent per browser context and rotate only when a worker context is recycled. Profiles use plausible Windows Chrome versions near the installed major version.
- Set coherent locale, timezone, language headers, viewport, and client-hint headers per context.
- Block image, media, and font resources to reduce server load while preserving HTML and scripts.
- Before each navigation, sleep a random value in `[1.0, 2.0]` seconds and acquire a global token from the token bucket.
- Enforce the configured page count with `asyncio.Semaphore`; default concurrency is 3.
- Retry transient navigation failures, 408, 425, 429, and 5xx responses with exponential backoff and jitter. Honor `Retry-After` when present.
- Treat 404 and 410 as permanent. A repeated 401/403 or detected challenge stops the run and saves diagnostic HTML/screenshot evidence.

## Discovery Flow

1. Render the home page and collect chapter links from `#tree1 a.edit`.
2. Seed each canonical `/search?keywords=XX` page in SQLite.
3. Parse `table.result tr.result-grid` and extract codes only from `/Code/(\d{10}).html` links.
4. Parse every numeric pagination link under `#pagination`; URL deduplication prevents the last-page self-link from looping.
5. Store all discovered detail URLs before the detail phase starts.
6. Fetch pending detail URLs concurrently, parse `#code-info`, validate the URL code against the DOM code, and checkpoint each record immediately.

## Data Model

The canonical SQLite record stores every section as ordered JSON rows so no DOM value is discarded. Each workbook sheet contains one row per ten-digit HS code with 25 Chinese business-facing columns:

- Code hierarchy: 2, 4, 6, 8, and 10 digit codes.
- Product fields: name, description, status, expiration flag, update date, primary and secondary units.
- Tax fields: raw strings and numeric percentage values where conversion is possible.
- Declaration, supervision, inspection, agreement, RCEP, hierarchy, and CIQ values as readable summary fields and lossless JSON.
- Raw section JSON, source URL, and crawl timestamp for auditability.

One-to-many sections are represented as compact readable summaries, while the final `原始数据(JSON)` column preserves every label and value exactly. This keeps the workbook compact without discarding DOM data.

## Persistence And Output

- `data/checkpoint.sqlite3`: canonical state and records.
- `data/hs_codes.xlsx`: first sheet `总表`, then one self-contained sheet per discovered two-digit chapter.
- `data/errors.csv`: unresolved pages with status, attempts, and error details.
- `logs/crawler.log`: operational progress.
- `artifacts/blocked/`: challenge diagnostics, created only when needed.

CSV export is atomic: write a temporary file and replace the target after success. Export runs periodically and once at clean shutdown, so a long crawl produces usable intermediate results.

## Verification

- Unit tests cover DOM variants, empty `无` sections, expired codes, dynamic nested rows, URL deduplication, rate parsing, date normalization, token-bucket timing, retry classification, SQLite resume behavior, and CSV encoding/schema.
- A live smoke test uses the known chapter `01` and a small detail limit through installed Chrome.
- A resume smoke test interrupts and restarts against the same checkpoint, verifying no duplicate code rows.

## Success Criteria

- All discovered third-level links are either stored as complete records or listed in `errors.csv` with a concrete terminal reason.
- Re-running the same command resumes without refetching completed pages.
- Codes are unique within and across chapter sheets, exactly ten digits, and match the sheet-name prefix.
- Every parsed DOM section is represented in `raw_sections_json` and in the relevant normalized CSV where applicable.
- The crawler remains within configured pacing and concurrency bounds.
