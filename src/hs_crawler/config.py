from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import re


DEFAULT_BASE_URL = "https://www.hsbianma.com"
DEFAULT_CHROME_PATH = Path(r"C:\Program Files\Google\Chrome\Application\chrome.exe")


@dataclass(frozen=True, slots=True)
class CrawlConfig:
    output_dir: Path = Path("data")
    base_url: str = DEFAULT_BASE_URL
    chrome_path: Path = DEFAULT_CHROME_PATH
    concurrency: int = 3
    min_delay: float = 1.0
    max_delay: float = 2.0
    token_rate: float = 1.2
    token_capacity: float = 2.0
    timeout_ms: int = 45_000
    max_retries: int = 4
    context_recycle_every: int = 120
    export_every: int = 250
    headless: bool = True
    respect_robots: bool = True
    chapters: tuple[str, ...] | None = None
    detail_limit: int | None = None
    log_path: Path = Path("logs/crawler.log")
    artifact_dir: Path = Path("artifacts/blocked")

    def __post_init__(self) -> None:
        object.__setattr__(self, "output_dir", Path(self.output_dir))
        object.__setattr__(self, "chrome_path", Path(self.chrome_path))
        object.__setattr__(self, "log_path", Path(self.log_path))
        object.__setattr__(self, "artifact_dir", Path(self.artifact_dir))
        object.__setattr__(self, "base_url", self.base_url.rstrip("/"))
        if self.concurrency < 1:
            raise ValueError("concurrency must be at least one")
        if self.min_delay < 0 or self.max_delay > 2.0 or self.max_delay < self.min_delay:
            raise ValueError("request delay must stay within 0 to 2.0 seconds")
        if self.token_rate <= 0 or self.token_capacity <= 0:
            raise ValueError("token bucket values must be positive")
        if self.export_every < 1:
            raise ValueError("export_every must be at least one")
        if self.detail_limit is not None and self.detail_limit < 1:
            raise ValueError("detail_limit must be at least one")
        if self.chapters is not None:
            chapters = tuple(dict.fromkeys(self.chapters))
            if any(re.fullmatch(r"\d{2}", chapter) is None for chapter in chapters):
                raise ValueError("chapters must contain two-digit codes")
            object.__setattr__(self, "chapters", chapters)

    @property
    def database_path(self) -> Path:
        return self.output_dir / "checkpoint.sqlite3"
