$ErrorActionPreference = "Stop"

$projectRoot = Split-Path -Parent $MyInvocation.MyCommand.Path
$python = "D:\APP\python 3.13.2\python.exe"
Set-Location -LiteralPath $projectRoot

& $python -m hs_crawler.monitor `
    --data-dir (Join-Path $projectRoot "data") `
    --log-path (Join-Path $projectRoot "logs\final.log") `
    --host 127.0.0.1 `
    --port 8765

exit $LASTEXITCODE
