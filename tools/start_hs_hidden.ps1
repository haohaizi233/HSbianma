[CmdletBinding()]
param(
    [ValidateRange(1, 64)]
    [int]$Concurrency = 20,

    [string]$ProjectRoot = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path,

    [string]$PythonwPath = "",

    [switch]$DryRun
)

$ErrorActionPreference = "Stop"

function Quote-CommandLineArgument {
    param([Parameter(Mandatory = $true)][string]$Value)

    return '"' + $Value.Replace('"', '\"') + '"'
}

$root = (Resolve-Path -LiteralPath $ProjectRoot).Path

if ([string]::IsNullOrWhiteSpace($PythonwPath)) {
    $pythonwCommand = Get-Command pythonw.exe -ErrorAction SilentlyContinue
    if ($null -eq $pythonwCommand) {
        $pythonCommand = Get-Command python.exe -ErrorAction Stop
        $PythonwPath = Join-Path (Split-Path -Parent $pythonCommand.Source) "pythonw.exe"
    }
    else {
        $PythonwPath = $pythonwCommand.Source
    }
}

$pythonw = (Resolve-Path -LiteralPath $PythonwPath).Path
$dataDir = Join-Path $root "data"
$logPath = Join-Path $root "logs\final.log"
$artifactDir = Join-Path $root "artifacts\final-blocked"

New-Item -ItemType Directory -Force -Path (Split-Path -Parent $logPath) | Out-Null
New-Item -ItemType Directory -Force -Path $artifactDir | Out-Null

$arguments = @(
    "-m",
    "hs_crawler",
    "crawl",
    "--output-dir",
    $dataDir,
    "--concurrency",
    [string]$Concurrency,
    "--min-delay",
    "0",
    "--max-delay",
    "0",
    "--token-rate",
    "1000",
    "--token-capacity",
    "1000",
    "--timeout-seconds",
    "35",
    "--max-retries",
    "3",
    "--context-recycle-every",
    "120",
    "--export-every",
    "100000000",
    "--retry-errors",
    "--log-path",
    $logPath,
    "--artifact-dir",
    $artifactDir
)

$quotedPython = Quote-CommandLineArgument $pythonw
$quotedArguments = $arguments | ForEach-Object {
    $argument = [string]$_
    if ($argument.StartsWith("-")) {
        $argument
    }
    else {
        Quote-CommandLineArgument $argument
    }
}
$commandLine = "$quotedPython $($quotedArguments -join ' ')"
$crawlerCommandPattern = '(?i)(?:^|\s)-m\s+"?hs_crawler"?\s+"?crawl"?(?:\s|$)'

if ($DryRun) {
    [ordered]@{
        launch_method = "Win32_Process.Create"
        concurrency = $Concurrency
        current_directory = $root
        command_line = $commandLine
        crawler_command_pattern = $crawlerCommandPattern
    } | ConvertTo-Json -Compress
    return
}

$existing = Get-CimInstance Win32_Process | Where-Object {
    $_.Name -match '^(python|pythonw)\.exe$' -and
    $_.CommandLine -match $crawlerCommandPattern -and
    $_.CommandLine -match [regex]::Escape($dataDir)
}
if ($existing) {
    $ids = ($existing | Select-Object -ExpandProperty ProcessId) -join ", "
    throw "A crawler is already running for this data directory (PID: $ids)."
}

$created = Invoke-CimMethod -ClassName Win32_Process -MethodName Create -Arguments @{
    CommandLine = $commandLine
    CurrentDirectory = $root
}

if ($created.ReturnValue -ne 0) {
    throw "Win32_Process.Create failed with return value $($created.ReturnValue)."
}

$process = $null
for ($attempt = 0; $attempt -lt 20; $attempt++) {
    Start-Sleep -Milliseconds 250
    $process = Get-CimInstance Win32_Process -Filter "ProcessId = $($created.ProcessId)" -ErrorAction SilentlyContinue
    if ($null -ne $process) {
        break
    }
}
if ($null -eq $process) {
    throw "Crawler process $($created.ProcessId) exited before startup verification."
}

[ordered]@{
    launch_method = "Win32_Process.Create"
    process_id = [int]$process.ProcessId
    parent_process_id = [int]$process.ParentProcessId
    process_name = $process.Name
    concurrency = $Concurrency
    command_line = $process.CommandLine
} | ConvertTo-Json -Compress
