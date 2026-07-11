from __future__ import annotations

import json
from pathlib import Path
import re
import subprocess
import sys


PROJECT_ROOT = Path(__file__).resolve().parents[1]
LAUNCHER = PROJECT_ROOT / "tools" / "start_hs_hidden.ps1"


def test_hidden_launcher_dry_run_uses_wmi_and_requested_concurrency() -> None:
    pythonw = Path(sys.executable).with_name("pythonw.exe")
    completed = subprocess.run(
        [
            "powershell.exe",
            "-NoLogo",
            "-NoProfile",
            "-NonInteractive",
            "-ExecutionPolicy",
            "Bypass",
            "-File",
            str(LAUNCHER),
            "-ProjectRoot",
            str(PROJECT_ROOT),
            "-PythonwPath",
            str(pythonw),
            "-Concurrency",
            "20",
            "-DryRun",
        ],
        check=False,
        capture_output=True,
        text=True,
        encoding="utf-8",
    )

    assert completed.returncode == 0, completed.stderr
    payload = json.loads(completed.stdout)
    command_line = payload["command_line"]

    assert payload["launch_method"] == "Win32_Process.Create"
    assert payload["concurrency"] == 20
    assert "pythonw.exe" in command_line
    assert "--concurrency \"20\"" in command_line
    assert "--min-delay \"0\"" in command_line
    assert "--max-delay \"0\"" in command_line
    assert "--token-rate \"1000\"" in command_line
    assert "--headful" not in command_line

    crawler_pattern = payload["crawler_command_pattern"]
    assert re.search(crawler_pattern, "pythonw.exe -m hs_crawler crawl --concurrency 20")
    assert re.search(crawler_pattern, 'pythonw.exe -m "hs_crawler" "crawl" --concurrency 20')
    assert not re.search(crawler_pattern, "pythonw.exe -m hs_crawler.monitor --port 8765")
