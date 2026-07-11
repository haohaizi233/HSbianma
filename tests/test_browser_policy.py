from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

from hs_crawler.browser import (
    BrowserDisconnectedError,
    BrowserFetcher,
    FetchError,
    FetchResult,
    ResponseAction,
    build_browser_profiles,
    build_launch_options,
    classify_http_status,
    compute_backoff,
    describe_dom_state,
    looks_like_challenge,
    retry_after_seconds,
)


@pytest.mark.parametrize(
    ("status", "expected"),
    [
        (200, ResponseAction.SUCCESS),
        (304, ResponseAction.SUCCESS),
        (408, ResponseAction.RETRY),
        (425, ResponseAction.RETRY),
        (429, ResponseAction.RETRY),
        (500, ResponseAction.RETRY),
        (503, ResponseAction.RETRY),
        (404, ResponseAction.PERMANENT),
        (410, ResponseAction.PERMANENT),
        (400, ResponseAction.PERMANENT),
        (401, ResponseAction.BLOCK),
        (403, ResponseAction.BLOCK),
        (0, ResponseAction.RETRY),
    ],
)
def test_classify_http_status(status: int, expected: ResponseAction) -> None:
    assert classify_http_status(status) is expected


def test_retry_after_supports_seconds_and_http_date() -> None:
    now = datetime(2026, 7, 11, 10, 0, 0, tzinfo=timezone.utc)

    assert retry_after_seconds("12", now=now) == 12.0
    assert retry_after_seconds("Sat, 11 Jul 2026 10:00:30 GMT", now=now) == 30.0
    assert retry_after_seconds("bad", now=now) is None


def test_compute_backoff_is_exponential_and_capped() -> None:
    assert compute_backoff(1, base=2.0, cap=20.0, jitter=lambda: 0.0) == 2.0
    assert compute_backoff(4, base=2.0, cap=10.0, jitter=lambda: 0.0) == 10.0


def test_challenge_detection_covers_common_english_and_chinese_pages() -> None:
    assert looks_like_challenge("<h1>Verify you are human</h1>", "Security check")
    assert looks_like_challenge("<div>访问过于频繁，请稍后再试</div>", "提示")
    assert not looks_like_challenge("<div id='code-info'>商品编码</div>", "HS Code")


def test_browser_profiles_keep_user_agent_and_client_hints_coherent() -> None:
    profiles = build_browser_profiles(installed_major=149)

    assert len(profiles) == 3
    for profile, major in zip(profiles, (149, 148, 147), strict=True):
        assert f"Chrome/{major}.0.0.0" in profile.user_agent
        assert f'"Google Chrome";v="{major}"' in profile.sec_ch_ua
        assert profile.viewport["width"] >= 1280


def test_launch_options_keep_playwright_default_automation_argument() -> None:
    options = build_launch_options(headless=True)

    assert "ignore_default_args" not in options
    assert "--disable-blink-features=AutomationControlled" in options["args"]
    assert "--no-proxy-server" in options["args"]


def test_dom_diagnostic_distinguishes_home_fallback_from_search_results() -> None:
    home = describe_dom_state(
        "https://www.hsbianma.com/",
        "HS Code",
        '<div id="tree1"></div>',
    )
    results = describe_dom_state(
        "https://www.hsbianma.com/search?keywords=53",
        "search results",
        '<tr class="result-grid"><td>5301100000</td></tr>',
    )

    assert "final_url=https://www.hsbianma.com/" in home
    assert "result_rows=0" in home
    assert "home_tree=true" in home
    assert "result_rows=1" in results
    assert "home_tree=false" in results


@pytest.mark.asyncio
async def test_fetch_restarts_after_disconnect_without_exhausting_slot_pool(
    tmp_path, monkeypatch
) -> None:
    fetcher = BrowserFetcher(
        chrome_path=tmp_path / "chrome.exe",
        concurrency=1,
        min_delay=1,
        max_delay=2,
    )
    fetcher._browser = SimpleNamespace()
    fetcher._generation = 1
    await fetcher._slots.put(SimpleNamespace(generation=1))
    attempts = 0

    async def fake_fetch(slot, url, *, wait_selector):
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise BrowserDisconnectedError("browser closed", url=url)
        return FetchResult(url, url, 200, "<html></html>", 0.01)

    async def fake_release(slot):
        if slot.generation == fetcher._generation:
            await fetcher._slots.put(slot)

    async def fake_restart(stale_generation):
        assert stale_generation == 1
        fetcher._generation = 2
        await fetcher._slots.put(SimpleNamespace(generation=2))

    monkeypatch.setattr(fetcher, "_fetch_with_retries", fake_fetch)
    monkeypatch.setattr(fetcher, "_release_slot", fake_release)
    monkeypatch.setattr(fetcher, "_restart_browser", fake_restart)

    result = await fetcher.fetch("https://example.test")

    assert result.status == 200
    assert attempts == 2
    assert fetcher._slots.qsize() == 1


@pytest.mark.asyncio
async def test_fetch_returns_slot_after_terminal_fetch_error(tmp_path, monkeypatch) -> None:
    fetcher = BrowserFetcher(
        chrome_path=tmp_path / "chrome.exe",
        concurrency=1,
        min_delay=1,
        max_delay=2,
    )
    fetcher._browser = SimpleNamespace()
    fetcher._generation = 1
    slot = SimpleNamespace(generation=1)
    await fetcher._slots.put(slot)

    async def fake_fetch(_slot, url, *, wait_selector):
        raise FetchError("terminal timeout", url=url, retryable=True)

    async def fake_release(released_slot):
        await fetcher._slots.put(released_slot)

    monkeypatch.setattr(fetcher, "_fetch_with_retries", fake_fetch)
    monkeypatch.setattr(fetcher, "_release_slot", fake_release)

    with pytest.raises(FetchError, match="terminal timeout"):
        await fetcher.fetch("https://example.test")

    assert fetcher._slots.qsize() == 1
    assert await fetcher._slots.get() is slot
