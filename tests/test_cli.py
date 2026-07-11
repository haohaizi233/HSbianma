import logging
from pathlib import Path
import sys

import pytest

from hs_crawler.cli import _configure_logging, build_parser, config_from_args
from hs_crawler.config import CrawlConfig


def test_crawl_cli_builds_config_with_chapter_filters_and_browser_options() -> None:
    parser = build_parser()
    args = parser.parse_args(
        [
            "crawl",
            "--output-dir",
            "result",
            "--chapter",
            "01",
            "--chapter",
            "02",
            "--concurrency",
            "2",
            "--headful",
            "--detail-limit",
            "5",
            "--skip-robots",
        ]
    )

    config = config_from_args(args)

    assert config.output_dir == Path("result")
    assert config.chapters == ("01", "02")
    assert config.concurrency == 2
    assert config.headless is False
    assert config.detail_limit == 5
    assert config.respect_robots is False


def test_crawl_cli_rejects_non_two_digit_chapter() -> None:
    parser = build_parser()

    with pytest.raises(SystemExit):
        parser.parse_args(["crawl", "--chapter", "1"])


def test_windowless_logging_uses_only_the_file_handler(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(sys, "stderr", None)

    _configure_logging(tmp_path / "crawler.log")

    handlers = logging.getLogger().handlers
    assert len(handlers) == 1
    assert isinstance(handlers[0], logging.FileHandler)


def test_crawl_config_allows_zero_delay_for_explicit_unlimited_runs() -> None:
    config = CrawlConfig(
        min_delay=0,
        max_delay=0,
        token_rate=1000,
        token_capacity=1000,
    )

    assert config.min_delay == 0
    assert config.max_delay == 0
