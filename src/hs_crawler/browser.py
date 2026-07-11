from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from enum import Enum
import logging
from pathlib import Path
import random
import re
import time
from typing import Any
from urllib.parse import urlparse

from playwright.async_api import (
    Browser,
    BrowserContext,
    Error as PlaywrightError,
    Page,
    Playwright,
    TimeoutError as PlaywrightTimeoutError,
    async_playwright,
)

from .rate_limit import AsyncTokenBucket, PoliteGate


LOGGER = logging.getLogger(__name__)


class ResponseAction(str, Enum):
    SUCCESS = "success"
    RETRY = "retry"
    PERMANENT = "permanent"
    BLOCK = "block"


@dataclass(frozen=True, slots=True)
class BrowserProfile:
    user_agent: str
    sec_ch_ua: str
    viewport: dict[str, int]


@dataclass(slots=True)
class FetchResult:
    requested_url: str
    final_url: str
    status: int
    html: str
    elapsed_seconds: float


class FetchError(RuntimeError):
    def __init__(
        self,
        message: str,
        *,
        url: str,
        status: int = 0,
        retryable: bool = False,
    ) -> None:
        super().__init__(message)
        self.url = url
        self.status = status
        self.retryable = retryable


class CrawlBlockedError(FetchError):
    pass


class BrowserDisconnectedError(FetchError):
    pass


@dataclass(slots=True)
class _BrowserSlot:
    context: BrowserContext
    page: Page
    profile: BrowserProfile
    generation: int
    request_count: int = 0


def classify_http_status(status: int) -> ResponseAction:
    if 200 <= status < 400:
        return ResponseAction.SUCCESS
    if status in {401, 403}:
        return ResponseAction.BLOCK
    if status in {404, 410}:
        return ResponseAction.PERMANENT
    if status in {0, 408, 425, 429} or 500 <= status < 600:
        return ResponseAction.RETRY
    if 400 <= status < 500:
        return ResponseAction.PERMANENT
    return ResponseAction.RETRY


def retry_after_seconds(
    value: str | None,
    *,
    now: datetime | None = None,
) -> float | None:
    if not value:
        return None
    text = value.strip()
    if re.fullmatch(r"\d+(?:\.\d+)?", text):
        return max(0.0, float(text))
    try:
        target = parsedate_to_datetime(text)
    except (TypeError, ValueError, OverflowError):
        return None
    if target.tzinfo is None:
        target = target.replace(tzinfo=timezone.utc)
    current = now or datetime.now(timezone.utc)
    return max(0.0, (target - current).total_seconds())


def compute_backoff(
    attempt: int,
    *,
    base: float = 2.0,
    cap: float = 60.0,
    jitter: Any = random.random,
) -> float:
    if attempt < 1:
        raise ValueError("attempt must be at least one")
    return min(cap, base * (2 ** (attempt - 1)) + float(jitter()))


def looks_like_challenge(html: str, title: str = "") -> bool:
    sample = f"{title}\n{html[:250_000]}".lower()
    markers = (
        "verify you are human",
        "checking your browser",
        "cf-chl-",
        "captcha",
        "访问过于频繁",
        "人机验证",
        "安全验证",
        "请完成验证",
    )
    return any(marker in sample for marker in markers)


def describe_dom_state(final_url: str, title: str, html: str) -> str:
    normalized_title = " ".join(title.split())[:160]
    result_rows = len(re.findall(r"\bresult-grid\b", html, flags=re.IGNORECASE))
    home_tree = re.search(
        r"\bid\s*=\s*['\"]tree1['\"]",
        html,
        flags=re.IGNORECASE,
    ) is not None
    return (
        f"final_url={final_url}; title={normalized_title!r}; "
        f"result_rows={result_rows}; home_tree={str(home_tree).lower()}"
    )


def _is_browser_disconnect_error(exc: BaseException) -> bool:
    message = str(exc).lower()
    markers = (
        "target page, context or browser has been closed",
        "browser has been closed",
        "browser closed",
        "connection closed",
    )
    return any(marker in message for marker in markers)


def build_browser_profiles(installed_major: int) -> list[BrowserProfile]:
    if installed_major < 3:
        raise ValueError("installed_major must be at least 3")
    viewports = (
        {"width": 1920, "height": 1080},
        {"width": 1536, "height": 864},
        {"width": 1366, "height": 768},
    )
    profiles: list[BrowserProfile] = []
    for index, major in enumerate(range(installed_major, installed_major - 3, -1)):
        profiles.append(
            BrowserProfile(
                user_agent=(
                    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                    f"AppleWebKit/537.36 (KHTML, like Gecko) Chrome/{major}.0.0.0 "
                    "Safari/537.36"
                ),
                sec_ch_ua=(
                    f'"Not_A Brand";v="99", "Google Chrome";v="{major}", '
                    f'"Chromium";v="{major}"'
                ),
                viewport=viewports[index],
            )
        )
    return profiles


def build_launch_options(*, headless: bool) -> dict[str, Any]:
    return {
        "headless": headless,
        "args": [
            "--disable-blink-features=AutomationControlled",
            "--disable-dev-shm-usage",
            "--no-proxy-server",
            "--no-default-browser-check",
            "--no-first-run",
        ],
    }


class BrowserFetcher:
    def __init__(
        self,
        *,
        chrome_path: Path,
        concurrency: int = 3,
        min_delay: float = 1.0,
        max_delay: float = 2.0,
        token_rate: float = 1.2,
        token_capacity: float = 2.0,
        headless: bool = True,
        timeout_ms: int = 45_000,
        max_retries: int = 4,
        context_recycle_every: int = 120,
        artifact_dir: Path = Path("artifacts/blocked"),
    ) -> None:
        if concurrency < 1:
            raise ValueError("concurrency must be at least one")
        if max_retries < 0:
            raise ValueError("max_retries cannot be negative")
        self.chrome_path = Path(chrome_path)
        self.concurrency = concurrency
        self.headless = headless
        self.timeout_ms = timeout_ms
        self.max_retries = max_retries
        self.context_recycle_every = context_recycle_every
        self.artifact_dir = Path(artifact_dir)
        self._semaphore = asyncio.Semaphore(concurrency)
        bucket = AsyncTokenBucket(rate=token_rate, capacity=token_capacity)
        self._gate = PoliteGate(
            bucket,
            min_delay=min_delay,
            max_delay=max_delay,
        )
        self._playwright: Playwright | None = None
        self._browser: Browser | None = None
        self._slots: asyncio.Queue[_BrowserSlot] = asyncio.Queue(maxsize=concurrency)
        self._profiles: list[BrowserProfile] = []
        self._profile_index = 0
        self._generation = 0
        self._restart_lock = asyncio.Lock()

    async def __aenter__(self) -> BrowserFetcher:
        await self.start()
        return self

    async def __aexit__(self, *_: object) -> None:
        await self.close()

    async def start(self) -> None:
        if self._browser is not None:
            return
        if not self.chrome_path.is_file():
            raise FileNotFoundError(f"Chrome executable not found: {self.chrome_path}")

        self._playwright = await async_playwright().start()
        try:
            await self._launch_browser()
            for _ in range(self.concurrency):
                await self._slots.put(await self._new_slot())
        except Exception:
            await self.close()
            raise

    async def close(self) -> None:
        while not self._slots.empty():
            slot = self._slots.get_nowait()
            try:
                await slot.context.close()
            except PlaywrightError:
                pass
        if self._browser is not None:
            try:
                await self._browser.close()
            except PlaywrightError:
                pass
            self._browser = None
        if self._playwright is not None:
            await self._playwright.stop()
            self._playwright = None

    async def _launch_browser(self) -> None:
        if self._playwright is None:
            raise RuntimeError("Playwright is not started")
        self._browser = await self._playwright.chromium.launch(
            executable_path=str(self.chrome_path),
            **build_launch_options(headless=self.headless),
        )
        if not self._profiles:
            installed_major = await self._detect_installed_major()
            self._profiles = build_browser_profiles(installed_major)
        self._generation += 1

    async def _detect_installed_major(self) -> int:
        if self._browser is None:
            raise RuntimeError("browser is not started")
        context = await self._browser.new_context()
        try:
            page = await context.new_page()
            user_agent = await page.evaluate("navigator.userAgent")
        finally:
            await context.close()
        match = re.search(r"(?:Chrome|HeadlessChrome)/(\d+)", user_agent)
        if not match:
            raise RuntimeError(f"cannot determine Chrome major from {user_agent!r}")
        return int(match.group(1))

    async def _new_slot(self) -> _BrowserSlot:
        if self._browser is None or not self._profiles:
            raise RuntimeError("browser is not started")
        profile = self._profiles[self._profile_index % len(self._profiles)]
        self._profile_index += 1
        context = await self._browser.new_context(
            user_agent=profile.user_agent,
            locale="zh-CN",
            timezone_id="Asia/Shanghai",
            viewport=profile.viewport,
            screen=profile.viewport,
            color_scheme="light",
            extra_http_headers={
                "Accept-Language": "zh-CN,zh;q=0.9,en-US;q=0.7,en;q=0.6",
                "sec-ch-ua": profile.sec_ch_ua,
                "sec-ch-ua-mobile": "?0",
                "sec-ch-ua-platform": '"Windows"',
                "Upgrade-Insecure-Requests": "1",
            },
        )
        await context.add_init_script(
            """
            Object.defineProperty(navigator, 'webdriver', {get: () => undefined});
            Object.defineProperty(navigator, 'languages', {get: () => ['zh-CN', 'zh', 'en-US', 'en']});
            """
        )

        async def reduce_bandwidth(route: Any) -> None:
            if route.request.resource_type in {"image", "media", "font"}:
                await route.abort()
            else:
                await route.continue_()

        await context.route("**/*", reduce_bandwidth)
        page = await context.new_page()
        page.set_default_navigation_timeout(self.timeout_ms)
        page.set_default_timeout(min(self.timeout_ms, 15_000))
        return _BrowserSlot(
            context=context,
            page=page,
            profile=profile,
            generation=self._generation,
        )

    async def _replace_slot(self, slot: _BrowserSlot) -> _BrowserSlot:
        try:
            await slot.context.close()
        except PlaywrightError:
            pass
        return await self._new_slot()

    async def _discard_slot(self, slot: _BrowserSlot) -> None:
        try:
            await slot.context.close()
        except (AttributeError, PlaywrightError):
            pass

    async def _release_slot(self, slot: _BrowserSlot) -> None:
        if slot.generation != self._generation:
            await self._discard_slot(slot)
            return
        unhealthy = slot.page.is_closed()
        recycle = slot.request_count >= self.context_recycle_every
        if unhealthy or recycle:
            try:
                slot = await self._replace_slot(slot)
            except PlaywrightError as exc:
                raise BrowserDisconnectedError(
                    f"browser slot replacement failed: {exc}",
                    url="",
                    retryable=True,
                ) from exc
        await self._slots.put(slot)

    async def _restart_browser(self, stale_generation: int) -> None:
        async with self._restart_lock:
            if stale_generation != self._generation:
                return
            LOGGER.warning("Restarting disconnected Chrome generation %s", stale_generation)
            while not self._slots.empty():
                queued = self._slots.get_nowait()
                try:
                    await queued.context.close()
                except PlaywrightError:
                    pass
            old_browser = self._browser
            self._browser = None
            if old_browser is not None:
                try:
                    await old_browser.close()
                except PlaywrightError:
                    pass
            try:
                await self._launch_browser()
                for _ in range(self.concurrency):
                    await self._slots.put(await self._new_slot())
            except Exception as exc:
                raise FetchError(
                    f"could not restart disconnected Chrome: {exc}",
                    url="",
                    retryable=True,
                ) from exc

    async def fetch(self, url: str, *, wait_selector: str | None = None) -> FetchResult:
        if self._browser is None:
            raise RuntimeError("BrowserFetcher.start() must be called before fetch()")

        async with self._semaphore:
            for recovery_attempt in range(2):
                try:
                    slot = await asyncio.wait_for(
                        self._slots.get(),
                        timeout=max(30.0, self.timeout_ms / 1000),
                    )
                except TimeoutError as exc:
                    raise FetchError(
                        "browser slot pool was exhausted",
                        url=url,
                        retryable=True,
                    ) from exc

                result: FetchResult | None = None
                disconnected: BrowserDisconnectedError | None = None
                try:
                    result = await self._fetch_with_retries(
                        slot,
                        url,
                        wait_selector=wait_selector,
                    )
                except BrowserDisconnectedError as exc:
                    disconnected = exc
                except BaseException:
                    try:
                        await self._release_slot(slot)
                    except BrowserDisconnectedError:
                        await self._discard_slot(slot)
                        await self._restart_browser(slot.generation)
                    raise
                if disconnected is not None:
                    await self._discard_slot(slot)
                else:
                    try:
                        await self._release_slot(slot)
                    except BrowserDisconnectedError as exc:
                        disconnected = exc

                if disconnected is None:
                    if result is None:
                        raise RuntimeError("browser fetch completed without a result")
                    return result

                await self._restart_browser(slot.generation)
                if recovery_attempt == 0:
                    continue
                raise FetchError(
                    f"browser disconnected twice while fetching {url}: {disconnected}",
                    url=url,
                    retryable=True,
                ) from disconnected

        raise RuntimeError("unreachable browser recovery state")

    async def _fetch_with_retries(
        self,
        slot: _BrowserSlot,
        url: str,
        *,
        wait_selector: str | None,
    ) -> FetchResult:
        last_error = "request did not run"
        last_status = 0
        for attempt in range(1, self.max_retries + 2):
            await self._gate.wait()
            started = time.monotonic()
            try:
                response = await slot.page.goto(
                    url,
                    wait_until="domcontentloaded",
                    timeout=self.timeout_ms,
                )
                slot.request_count += 1
                status = response.status if response is not None else 0
                last_status = status
                html = await slot.page.content()
                title = await slot.page.title()
                if looks_like_challenge(html, title):
                    await self._save_block_artifacts(slot.page, url, html)
                    raise CrawlBlockedError(
                        "challenge page detected",
                        url=url,
                        status=status,
                    )

                action = classify_http_status(status)
                if action is ResponseAction.SUCCESS:
                    if wait_selector:
                        await slot.page.wait_for_selector(wait_selector, state="attached")
                        html = await slot.page.content()
                    return FetchResult(
                        requested_url=url,
                        final_url=slot.page.url,
                        status=status,
                        html=html,
                        elapsed_seconds=time.monotonic() - started,
                    )

                if action is ResponseAction.PERMANENT:
                    raise FetchError(
                        f"permanent HTTP {status}",
                        url=url,
                        status=status,
                    )

                if action is ResponseAction.BLOCK and attempt >= 2:
                    await self._save_block_artifacts(slot.page, url, html)
                    raise CrawlBlockedError(
                        f"repeated access denial HTTP {status}",
                        url=url,
                        status=status,
                    )

                last_error = f"HTTP {status}"
                if attempt > self.max_retries:
                    break
                headers = response.headers if response is not None else {}
                delay = retry_after_seconds(headers.get("retry-after"))
                await asyncio.sleep(delay if delay is not None else compute_backoff(attempt))
            except CrawlBlockedError:
                raise
            except FetchError:
                raise
            except (PlaywrightTimeoutError, PlaywrightError) as exc:
                if _is_browser_disconnect_error(exc):
                    raise BrowserDisconnectedError(
                        f"browser disconnected: {exc}",
                        url=url,
                        status=last_status,
                        retryable=True,
                    ) from exc
                diagnostic = ""
                if wait_selector:
                    try:
                        diagnostic = describe_dom_state(
                            slot.page.url,
                            await slot.page.title(),
                            await slot.page.content(),
                        )
                    except PlaywrightError:
                        diagnostic = "DOM diagnostic unavailable"
                last_error = f"{type(exc).__name__}: {exc}"
                if diagnostic:
                    last_error = f"{last_error}; {diagnostic}"
                if attempt > self.max_retries:
                    break
                LOGGER.warning("Browser retry %s/%s for %s: %s", attempt, self.max_retries, url, exc)
                await asyncio.sleep(compute_backoff(attempt))

        raise FetchError(
            f"transient fetch failure after retries: {last_error}",
            url=url,
            status=last_status,
            retryable=True,
        )

    async def _save_block_artifacts(self, page: Page, url: str, html: str) -> None:
        self.artifact_dir.mkdir(parents=True, exist_ok=True)
        host = re.sub(r"[^a-zA-Z0-9._-]", "_", urlparse(url).netloc or "page")
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        stem = self.artifact_dir / f"{stamp}-{host}"
        stem.with_suffix(".html").write_text(html, encoding="utf-8")
        try:
            await page.screenshot(path=str(stem.with_suffix(".png")), full_page=True)
        except PlaywrightError:
            LOGGER.exception("Could not save block screenshot for %s", url)
