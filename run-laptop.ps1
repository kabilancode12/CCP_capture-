# run-laptop.ps1: update and start CCP on Windows (PowerShell)
# Usage in PowerShell: .\run-laptop.ps1
param (
    [string]$Action = "update"
)

$ErrorActionPreference = "Stop"
Set-Location $PSScriptRoot

if (-not (Test-Path ".env")) {
    Write-Error "No .env file found in $PSScriptRoot. Please configure .env first."
}

# Determine Python executable
$Python = ".\.venv\Scripts\python.exe"
if (Test-Path $Python) {
    $Python = (Resolve-Path $Python).Path
} else {
    $Cmd = Get-Command python -ErrorAction SilentlyContinue
    if ($Cmd) {
        $Python = $Cmd.Source
    } else {
        Write-Error "Python was not found. Please install Python 3.11+ and create .venv."
    }
}

Write-Host "==> 1/6 Updating CCP Python packages..." -ForegroundColor Cyan
& $Python -m pip install -q -e ".[server]"

Write-Host "==> 2/6 Ensuring .env has required plug-in settings..." -ForegroundColor Cyan
& $Python -c @"
import secrets
from pathlib import Path

p = Path('.env')
if p.exists():
    text = p.read_text(encoding='utf-8')
    changed = False
    if 'CCP_DECISIONS_ENABLED=' not in text:
        text += '\nCCP_DECISIONS_ENABLED=true\n'
        changed = True
    elif 'CCP_DECISIONS_ENABLED=false' in text:
        text = text.replace('CCP_DECISIONS_ENABLED=false', 'CCP_DECISIONS_ENABLED=true')
        changed = True
    if 'CCP_CATALOG_ENABLED=' not in text:
        text += '\nCCP_CATALOG_ENABLED=true\n'
        changed = True
    if 'CCP_EXPLORER_ENABLED=' not in text:
        text += '\nCCP_EXPLORER_ENABLED=true\n'
        changed = True
    if 'CCP_EXPLORER_SECRET=' not in text:
        text += f'\nCCP_EXPLORER_SECRET={secrets.token_urlsafe(32)}\n'
        changed = True
    if changed:
        p.write_text(text, encoding='utf-8')
"@

Write-Host "==> 3/6 Starting Docker containers (PostgreSQL, MinIO, Metabase)..." -ForegroundColor Cyan
docker compose --env-file .env -f infra/docker-compose.yml --profile dashboards up -d postgres minio metabase

# Stop any running ccp.server before migration so it does not hold database locks
Get-CimInstance Win32_Process -Filter "Name = 'python.exe'" -ErrorAction SilentlyContinue |
    Where-Object { $_.CommandLine -like "*ccp.server*" } |
    ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }

Write-Host "==> 4/6 Applying database migrations..." -ForegroundColor Cyan
& $Python -m ccp.db migrate

Write-Host "==> 5/6 Updating Claude Code capture hooks in ~/.claude/settings.json..." -ForegroundColor Cyan
$TokenLine = Get-Content .env | Select-String "^CCP_INGEST_TOKEN=(.+)"
if ($TokenLine) {
    $Token = $TokenLine.Matches.Groups[1].Value.Trim()
    & $Python -m ccp.capture install --api-url "http://127.0.0.1:8000" --token $Token
}
& $Python -m ccp.capture watch-service install

Write-Host "==> 6/6 Checking CCP server..." -ForegroundColor Cyan
$ServerHealthy = $false
try {
    $Resp = Invoke-RestMethod -Uri "http://127.0.0.1:8000/health" -TimeoutSec 2 -ErrorAction SilentlyContinue
    if ($Resp.status -eq "ok") {
        $ServerHealthy = $true
    }
} catch {
    $ServerHealthy = $false
}

if (-not $ServerHealthy) {
    Write-Host "Starting CCP Server in a dedicated background window..." -ForegroundColor Yellow
    Start-Process powershell -ArgumentList "-NoExit", "-Command", "cd '$PSScriptRoot'; & '$Python' -m ccp.server --migrate"
    Start-Sleep -Seconds 3
} else {
    Write-Host "CCP server is already running and healthy." -ForegroundColor Green
}

Write-Host "`nAll services up to date! You can start a new Claude Code session now." -ForegroundColor Green
