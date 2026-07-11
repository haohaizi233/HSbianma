$ErrorActionPreference = "Stop"

$projectRoot = Split-Path -Parent $MyInvocation.MyCommand.Path
$python = "D:\APP\python 3.13.2\python.exe"
$dataDir = Join-Path $projectRoot "data"
$workbook = Join-Path $dataDir "hs_codes.xlsx"
$errorsCsv = Join-Path $dataDir "errors.csv"
$runnerLog = Join-Path $projectRoot "logs\runner.log"
$stdoutLog = Join-Path $projectRoot "logs\scheduled.stdout.log"
$stderrLog = Join-Path $projectRoot "logs\scheduled.stderr.log"

Set-Location -LiteralPath $projectRoot
New-Item -ItemType Directory -Force -Path (Split-Path $runnerLog) | Out-Null

function Write-RunnerLog([string]$Message) {
    $timestamp = Get-Date -Format "yyyy-MM-dd HH:mm:ss"
    Add-Content -LiteralPath $runnerLog -Encoding UTF8 -Value "$timestamp $Message"
}

function Get-CrawlStatus {
    $json = & $python -m hs_crawler status --output-dir $dataDir 2>> $stderrLog
    if ($LASTEXITCODE -ne 0) {
        throw "Could not read crawler status (exit $LASTEXITCODE)"
    }
    return ($json | Out-String | ConvertFrom-Json)
}

function Remove-IncompleteExports {
    Remove-Item -LiteralPath $workbook -Force -ErrorAction SilentlyContinue
    Remove-Item -LiteralPath $errorsCsv -Force -ErrorAction SilentlyContinue
}

$maximumPasses = 8
for ($pass = 1; $pass -le $maximumPasses; $pass++) {
    Write-RunnerLog "Starting pass $pass of $maximumPasses"
    $crawlArguments = @(
        "-m", "hs_crawler", "crawl",
        "--output-dir", $dataDir,
        "--concurrency", "4",
        "--min-delay", "1",
        "--max-delay", "2",
        "--token-rate", "1.8",
        "--token-capacity", "3",
        "--timeout-seconds", "60",
        "--max-retries", "4",
        "--context-recycle-every", "120",
        "--export-every", "100000000",
        "--retry-errors",
        "--log-path", (Join-Path $projectRoot "logs\final.log"),
        "--artifact-dir", (Join-Path $projectRoot "artifacts\final-blocked")
    )
    $crawlProcess = Start-Process `
        -FilePath $python `
        -ArgumentList $crawlArguments `
        -WorkingDirectory $projectRoot `
        -WindowStyle Hidden `
        -Wait `
        -PassThru `
        -RedirectStandardOutput $stdoutLog `
        -RedirectStandardError $stderrLog
    $crawlExitCode = $crawlProcess.ExitCode

    $status = Get-CrawlStatus
    $pending = [int]($status.detail_pending) + [int]($status.search_pending)
    $active = [int]($status.detail_in_progress) + [int]($status.search_in_progress)
    $errors = [int]($status.detail_error) + [int]($status.search_error)
    $records = [int]$status.records
    Write-RunnerLog "Pass $pass ended: exit=$crawlExitCode records=$records pending=$pending active=$active errors=$errors"

    if ($crawlExitCode -eq 0 -and $pending -eq 0 -and $active -eq 0 -and $errors -eq 0) {
        if (-not (Test-Path -LiteralPath $workbook)) {
            throw "Crawler completed without producing $workbook"
        }
        Write-RunnerLog "Crawl completed successfully"
        exit 0
    }

    Remove-IncompleteExports
    if ($crawlExitCode -ne 0 -or $pending -gt 0 -or $active -gt 0) {
        Write-RunnerLog "Crawler did not reach a clean pass; stopping wrapper for diagnosis"
        exit $(if ($crawlExitCode -ne 0) { $crawlExitCode } else { 2 })
    }

    if ($pass -lt $maximumPasses) {
        $delaySeconds = [Math]::Min(120, 15 * $pass)
        Write-RunnerLog "Retrying $errors terminal pages after $delaySeconds seconds"
        Start-Sleep -Seconds $delaySeconds
    }
}

Remove-IncompleteExports
Write-RunnerLog "Retry budget exhausted"
exit 2
