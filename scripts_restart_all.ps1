# Udobny restart vseh botov odnoj komandoj (bez stop-all — on valit API-server).
#   .\scripts_restart_all.ps1              — polnyj restart (ostanovka + zapusk vseh)
#   .\scripts_restart_all.ps1 -NoStop      — tolko zapustit vseh
param(
    [switch]$NoStop
)
$ErrorActionPreference = "Continue"
$root = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location $root
$api = "http://localhost:5000/api"

if (-not $NoStop) {
    Write-Host "Stopping all bots (per-symbol, safe)..."
    try {
        $bots = Invoke-RestMethod -Uri "$api/bots" -TimeoutSec 10
        foreach ($b in $bots) {
            try {
                Invoke-RestMethod -Method Post -Uri "$api/bots/$($b.symbol)/stop" -TimeoutSec 8 | Out-Null
            } catch {
                # ignored — bot may not be tracked
            }
        }
    } catch {
        Write-Warning ("list bots error: " + $_.Exception.Message)
    }
    # Stop any stray python bot processes directly (dashboard's stop may leave them).
    Get-Process python -ErrorAction SilentlyContinue | Stop-Process -Force -ErrorAction SilentlyContinue
    Start-Sleep -Seconds 3
}

Write-Host "Starting all bots (parallel)..."
python scripts_start_bots_parallel.py
