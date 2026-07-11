from __future__ import annotations

import argparse
import asyncio
from dataclasses import asdict
import json
import logging
from pathlib import Path
import re
import sys
from typing import Sequence

from .browser import BrowserFetcher, CrawlBlockedError
from .config import CrawlConfig, DEFAULT_BASE_URL, DEFAULT_CHROME_PATH
from .crawler import HSCrawler, RobotsDeniedError
from .exporter import export_workbook
from .store import CheckpointStore


def _chapter(value: str) -> str:
    if re.fullmatch(r"\d{2}", value) is None:
        raise argparse.ArgumentTypeError("chapter must be exactly two digits")
    return value


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="hs-crawler",
        description="Chrome-rendered, restartable hsbianma.com crawler",
    )
    commands = parser.add_subparsers(dest="command", required=True)

    crawl = commands.add_parser("crawl", help="discover and crawl pending pages")
    crawl.add_argument("--output-dir", type=Path, default=Path("data"))
    crawl.add_argument("--base-url", default=DEFAULT_BASE_URL)
    crawl.add_argument("--chrome-path", type=Path, default=DEFAULT_CHROME_PATH)
    crawl.add_argument("--concurrency", type=int, default=3)
    crawl.add_argument("--min-delay", type=float, default=1.0)
    crawl.add_argument("--max-delay", type=float, default=2.0)
    crawl.add_argument("--token-rate", type=float, default=1.2)
    crawl.add_argument("--token-capacity", type=float, default=2.0)
    crawl.add_argument("--timeout-seconds", type=float, default=45.0)
    crawl.add_argument("--max-retries", type=int, default=4)
    crawl.add_argument("--context-recycle-every", type=int, default=120)
    crawl.add_argument("--export-every", type=int, default=250)
    crawl.add_argument("--chapter", action="append", type=_chapter, dest="chapters")
    crawl.add_argument("--detail-limit", type=int)
    crawl.add_argument("--headful", action="store_true")
    crawl.add_argument("--skip-robots", action="store_true")
    crawl.add_argument("--retry-errors", action="store_true")
    crawl.add_argument("--log-path", type=Path, default=Path("logs/crawler.log"))
    crawl.add_argument("--artifact-dir", type=Path, default=Path("artifacts/blocked"))

    export = commands.add_parser("export", help="rebuild the Excel workbook from SQLite")
    export.add_argument("--output-dir", type=Path, default=Path("data"))

    status = commands.add_parser("status", help="show checkpoint statistics")
    status.add_argument("--output-dir", type=Path, default=Path("data"))

    return parser


def config_from_args(args: argparse.Namespace) -> CrawlConfig:
    if args.command != "crawl":
        raise ValueError("config_from_args requires the crawl command")
    return CrawlConfig(
        output_dir=args.output_dir,
        base_url=args.base_url,
        chrome_path=args.chrome_path,
        concurrency=args.concurrency,
        min_delay=args.min_delay,
        max_delay=args.max_delay,
        token_rate=args.token_rate,
        token_capacity=args.token_capacity,
        timeout_ms=int(args.timeout_seconds * 1000),
        max_retries=args.max_retries,
        context_recycle_every=args.context_recycle_every,
        export_every=args.export_every,
        headless=not args.headful,
        respect_robots=not args.skip_robots,
        chapters=tuple(args.chapters) if args.chapters else None,
        detail_limit=args.detail_limit,
        log_path=args.log_path,
        artifact_dir=args.artifact_dir,
    )


def _configure_logging(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    formatter = logging.Formatter(
        "%(asctime)s %(levelname)s %(name)s %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )
    file_handler = logging.FileHandler(path, encoding="utf-8")
    file_handler.setFormatter(formatter)
    handlers: list[logging.Handler] = [file_handler]
    if sys.stderr is not None:
        console = logging.StreamHandler()
        console.setFormatter(formatter)
        handlers.insert(0, console)
    logging.basicConfig(
        level=logging.INFO,
        handlers=handlers,
        force=True,
    )


async def _run_crawl(args: argparse.Namespace) -> dict[str, int]:
    config = config_from_args(args)
    _configure_logging(config.log_path)
    store = CheckpointStore(config.database_path)
    await store.open()
    fetcher = BrowserFetcher(
        chrome_path=config.chrome_path,
        concurrency=config.concurrency,
        min_delay=config.min_delay,
        max_delay=config.max_delay,
        token_rate=config.token_rate,
        token_capacity=config.token_capacity,
        headless=config.headless,
        timeout_ms=config.timeout_ms,
        max_retries=config.max_retries,
        context_recycle_every=config.context_recycle_every,
        artifact_dir=config.artifact_dir,
    )
    try:
        if args.retry_errors:
            requeued = await store.requeue_errors()
            logging.getLogger(__name__).info("Requeued %s terminal errors", requeued)
        await fetcher.start()
        summary = await HSCrawler(config, store, fetcher).run()
        return asdict(summary)
    finally:
        await fetcher.close()
        await store.close()


async def _run_export(output_dir: Path) -> dict[str, int]:
    store = CheckpointStore(
        output_dir / "checkpoint.sqlite3",
        reset_in_progress_on_open=False,
    )
    await store.open()
    try:
        return asdict(await export_workbook(store, output_dir))
    finally:
        await store.close()


async def _run_status(output_dir: Path) -> dict[str, int]:
    store = CheckpointStore(
        output_dir / "checkpoint.sqlite3",
        reset_in_progress_on_open=False,
    )
    await store.open()
    try:
        return await store.stats()
    finally:
        await store.close()


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        if args.command == "crawl":
            result = asyncio.run(_run_crawl(args))
        elif args.command == "export":
            result = asyncio.run(_run_export(args.output_dir))
        else:
            result = asyncio.run(_run_status(args.output_dir))
        if sys.stdout is not None:
            print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0
    except (CrawlBlockedError, RobotsDeniedError) as exc:
        logging.getLogger(__name__).critical("Crawl stopped: %s", exc)
        if sys.stderr is not None:
            print(f"crawl stopped: {exc}", file=sys.stderr)
        return 3
    except KeyboardInterrupt:
        if sys.stderr is not None:
            print("interrupted", file=sys.stderr)
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
