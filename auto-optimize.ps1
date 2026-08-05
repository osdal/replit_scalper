# auto-optimize.ps1
# Runs optimizer for each pair over the last N days,
# applies the best parameters to config if improved, and reloads bots.
#
# Usage:
#   .\auto-optimize.ps1
#   .\auto-optimize.ps1 -Trials 200 -Jobs 4 -DaysBack 7
#   .\auto-optimize.ps1 -Symbols @("ETHUSDT","BTCUSDT")
#   .\auto-optimize.ps1 -SyncOnly (sync current CSV results without running optimizer)
#   .\auto-optimize.ps1 -CreateSchedule (set up daily Task Scheduler job)

param(
    [int]$Trials = 200,
    [int]$Jobs = 4,
    [int]$DaysBack = 7,
    [string[]]$Symbols = @(),
    [switch]$SyncOnly,
    [switch]$CreateSchedule,
    [switch]$Force
)

$scriptDir = $PSScriptRoot
$botDir = Join-Path $scriptDir "bot"
$logsDir = Join-Path $botDir "logs"
$API = "http://localhost:5000/api"

$endDate = (Get-Date).ToString("yyyy-MM-dd")
$startDate = (Get-Date).AddDays(-$DaysBack).ToString("yyyy-MM-dd")

Write-Host "============================================"
Write-Host "  Auto-Optimizer"
Write-Host "  Period: $startDate -> $endDate"
Write-Host "  Trials: $Trials | Jobs: $Jobs"
Write-Host "============================================"
Write-Host ""

# ── Helpers ────────────────────────────────────────────────────────

function Read-CSV-Params($symbol) {
    $pattern = "optimization_*.csv"
    $files = Get-ChildItem -Path $logsDir -Filter $pattern -ErrorAction SilentlyContinue |
        Sort-Object LastWriteTime -Descending
    if (-not $files) { return $null }
    
    foreach ($f in $files) {
        try {
            $lines = Get-Content $f.FullName -Encoding UTF8 | Where-Object { $_ -match "," }
            if ($lines.Count -lt 2) { continue }
            $headers = $lines[0] -split ","
            $best = $lines[1] -split ","
            if ($headers.Count -lt 3 -or $best.Count -lt 3) { continue }
            $result = @{}
            for ($i = 0; $i -lt $headers.Count -and $i -lt $best.Count; $i++) {
                $result[$headers[$i].Trim()] = $best[$i].Trim()
            }
            return $result
        } catch {}
    }
    return $null
}

function Get-CurrentScore($symbol) {
    try {
        $r = Invoke-RestMethod -Uri "$API/bots/$symbol" -ErrorAction Stop
        if ($r.best_score) { return [double]$r.best_score }
        if ($r.config -and $r.config.score) { return [double]$r.config.score }
    } catch {}
    return $null
}

function Apply-To-Config($symbol, $params) {
    $body = @{}
    $numberKeys = @("ema_fast","ema_slow","sl_pct","tp1_pct","tp2_pct",
                    "volume_multiplier","tp1_close_pct","risk_pct",
                    "htf_ema_fast","htf_ema_slow")
    foreach ($k in $numberKeys) {
        if ($params.ContainsKey($k) -and $params[$k] -ne "" -and $params[$k] -ne "-") {
            $body[$k] = [double]$params[$k]
        }
    }
    # HTF enabled if HTF params are present and > 0
    if ($params.ContainsKey("htf_ema_fast") -and [double]$params["htf_ema_fast"] -gt 0) {
        $body["htf_enabled"] = $true
    } else {
        $body["htf_enabled"] = $false
    }
    
    try {
        $json = $body | ConvertTo-Json
        $r = Invoke-RestMethod -Method PUT -Uri "$API/bots/$symbol/config" `
            -Body $json -ContentType "application/json" -ErrorAction Stop
        return $true
    } catch {
        Write-Host "  API error: $_" -ForegroundColor Red
        return $false
    }
}

function Run-Optimizer($symbol) {
    $configFile = "config_" + $symbol.Replace("USDT","").ToLower() + ".yaml"
    $args = @(
        "optimizer.py",
        "--config", $configFile,
        "--symbol", $symbol,
        "--start", $startDate,
        "--end", $endDate,
        "--trials", $Trials,
        "--jobs", $Jobs,
        "--study-name", "optimizer_$symbol"
    )
    
    $proc = Start-Process -FilePath "python" -ArgumentList $args `
        -WorkingDirectory $botDir -NoNewWindow -PassThru -Wait
    
    return ($proc.ExitCode -eq 0)
}

# ── Get symbol list ─────────────────────────────────────────────────

if ($Symbols.Count -eq 0) {
    $Symbols = Get-ChildItem -Path $botDir -Filter "config_*.yaml" |
        Where-Object { $_.Name -notmatch "recovery_config" } |
        ForEach-Object {
            $_.BaseName -replace "^config_", "" |
                ForEach-Object { $_.ToUpper() + "USDT" }
        } | Sort-Object
}

Write-Host "Symbols to optimize: $($Symbols.Count)"
Write-Host ""

# ── Main loop ───────────────────────────────────────────────────────

$results = @()
$failures = @()

foreach ($sym in $Symbols) {
    Write-Host ("[$($results.Count + 1)/$($Symbols.Count)] $sym ...") -ForegroundColor Cyan
    
    $success = $true
    if (-not $SyncOnly) {
        Write-Host "  Running optimizer..." -ForegroundColor Gray
        $success = Run-Optimizer $sym
    }
    
    if (-not $success) {
        Write-Host "  FAILED: optimizer error" -ForegroundColor Red
        $failures += "$sym (optimizer failed)"
        continue
    }
    
    $bestParams = Read-CSV-Params $sym
    if (-not $bestParams) {
        Write-Host "  FAILED: no CSV results found" -ForegroundColor Red
        $failures += "$sym (no results)"
        continue
    }
    
    $scoreStr = $bestParams["score"]
    $emaF = $bestParams["ema_fast"]
    $emaS = $bestParams["ema_slow"]
    Write-Host "  Best: score=$scoreStr EMA=$emaF/$emaS TP1=$($bestParams['tp1_pct']) SL=$($bestParams['sl_pct'])" -ForegroundColor Green
    
    # Compare with current score
    $currentScore = Get-CurrentScore $sym
    $newScore = if ($scoreStr -ne "" -and $scoreStr -ne "-") { [double]$scoreStr } else { 0 }
    
    if ($currentScore -and (-not $Force)) {
        $delta = $newScore - $currentScore
        $deltaPct = if ($currentScore -ne 0) { ($delta / $currentScore * 100) } else { 100 }
        if ($delta -le 0) {
            Write-Host "  SKIP: current score=$currentScore is better than new=$newScore (delta=$deltaPct%)" -ForegroundColor DarkYellow
            continue
        }
        Write-Host "  IMPROVE: $currentScore -> $newScore ($deltaPct%)" -ForegroundColor Green
    }
    
    $applied = Apply-To-Config $sym $bestParams
    if ($applied) {
        $results += "$sym (score=$scoreStr, EMA=$emaF/$emaS)"
    } else {
        $failures += "$sym (config update failed)"
    }
}

# ── Reload & restart ─────────────────────────────────────────────────

Write-Host ""
Write-Host "Reloading configs and restarting bots..." -ForegroundColor Yellow

try {
    Invoke-RestMethod -Method POST -Uri "$API/refresh" -ErrorAction Stop | Out-Null
    Start-Sleep -Seconds 2
    foreach ($sym in $Symbols) {
        try {
            Invoke-RestMethod -Method POST -Uri "$API/bots/$sym/start" -ErrorAction SilentlyContinue | Out-Null
        } catch {}
        Start-Sleep -Milliseconds 300
    }
    Write-Host "All bots restarted" -ForegroundColor Green
} catch {
    Write-Host "Reload FAILED: $_" -ForegroundColor Red
}

# ── Create scheduled task ─────────────────────────────────────────────

if ($CreateSchedule) {
    Write-Host ""
    Write-Host "Creating daily scheduled task..." -ForegroundColor Yellow
    
    $taskName = "AutoOptimizeTradingBots"
    $scriptPath = Join-Path $scriptDir "auto-optimize.ps1"
    $action = New-ScheduledTaskAction -Execute "powershell.exe" -Argument "-NoProfile -ExecutionPolicy Bypass -File `"$scriptPath`""
    $trigger = New-ScheduledTaskTrigger -Daily -At 3am
    $settings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -StartWhenAvailable
    $principal = New-ScheduledTaskPrincipal -UserId "$env:USERDOMAIN\$env:USERNAME" -RunLevel Highest
    
    try {
        Register-ScheduledTask -TaskName $taskName -Action $action -Trigger $trigger -Settings $settings -Principal $principal -Force | Out-Null
        Write-Host "Scheduled task created: $taskName (runs daily at 3am)" -ForegroundColor Green
    } catch {
        Write-Host "Failed to create scheduled task: $_" -ForegroundColor Red
    }
}

# ── Report ───────────────────────────────────────────────────────────

Write-Host ""
Write-Host "============================================"
Write-Host "  RESULTS"
Write-Host "============================================"
Write-Host "Applied : $($results.Count) bots"
foreach ($r in $results) { Write-Host "  $r" -ForegroundColor Green }
if ($failures.Count -gt 0) {
    Write-Host "Failed  : $($failures.Count)"
    foreach ($f in $failures) { Write-Host "  $f" -ForegroundColor Red }
}
Write-Host ""
Write-Host "Next run: $(if ($CreateSchedule) { 'Daily at 3am (scheduled)' } else { 'Manual' })"
Write-Host "============================================"
