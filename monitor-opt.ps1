# monitor-opt.ps1
# Live monitor for walk-forward optimization.
# Run:  powershell -ExecutionPolicy Bypass -File monitor-opt.ps1
# Shows progress, active processes and errors. Ctrl+C to exit.

$botDir   = "C:\DATA\bots\replit_scalper\bot"
$logsDir  = Join-Path $botDir "logs"
$startTime = [datetime]'2026-08-07 17:54:00'

$pairs = @(
    "ATOMUSDT","DOGEUSDT","ETHUSDT","SOLUSDT","INJUSDT","OPUSDT",
    "POLUSDT","ONTUSDT","HBARUSDT","NEARUSDT","SUIUSDT","FILUSDT",
    "KASUSDT","XRPUSDT","LINKUSDT","DOTUSDT","TRXUSDT","BTCUSDT",
    "BNBUSDT","AVAXUSDT","ADAUSDT","1000PEPEUSDT","ARBUSDT","APTUSDT"
)

$statusFile = Join-Path $logsDir "optimization_status.txt"

function Get-LastStatus {
    if (Test-Path $statusFile) {
        return (Get-Content $statusFile -TotalCount 1 -ErrorAction SilentlyContinue)
    }
    return "(нет файла $statusFile - оптимизация ещё не запускалась или запускалась до добавления статуса)"
}

$today = Get-Date -Format "yyyyMMdd"
$cycle = 0

while ($true) {
    Clear-Host

    $done = @{}
    Get-ChildItem "$logsDir\optimization_*${today}*.csv" -ErrorAction SilentlyContinue |
        ForEach-Object {
            $m = [regex]::Match($_.Name, "optimization_(\w+)")
            if ($m.Success) { $done[$m.Groups[1].Value] = $true }
        }
    $donePairs = $pairs | Where-Object { $done[$_] }
    $leftPairs = $pairs | Where-Object { -not $done[$_] }

    $activePairs = @{}
    Get-CimInstance Win32_Process -Filter "Name='python.exe'" -ErrorAction SilentlyContinue |
        Where-Object { $_.CommandLine -match "optimizer.py" } |
        ForEach-Object {
            $m = [regex]::Match($_.CommandLine, "--symbol (\w+)")
            if ($m.Success) { $activePairs[$m.Groups[1].Value] = $_.ProcessId }
        }

    $wfAlive = $false
    Get-CimInstance Win32_Process -Filter "Name='python.exe'" -ErrorAction SilentlyContinue |
        Where-Object { $_.CommandLine -match "walk_forward_opt" } |
        ForEach-Object { $wfAlive = $true }

    $doneCount = $donePairs.Count
    $pct = [math]::Round($doneCount / $pairs.Count * 100)
    $bar = ("#" * $doneCount).PadRight($pairs.Count, "-")

    $elapsed = (Get-Date) - $startTime
    $elapsedStr = "{0:H}h {0:m}m" -f $elapsed
    $estLeftSec = [math]::Round((100 - $pct) / 100 * 4 * 3600)
    $estLeft = New-TimeSpan -Seconds $estLeftSec
    $estLeftStr = "{0:H}h {0:m}m" -f $estLeft

    Write-Host "============================================================" -ForegroundColor Cyan
    Write-Host ("  MONITOR WALK-FORWARD  (update #" + $cycle + ", " + (Get-Date -Format 'HH:mm:ss') + ")") -ForegroundColor Cyan
    Write-Host "  Ctrl+C to exit" -ForegroundColor DarkGray
    Write-Host "============================================================" -ForegroundColor Cyan
    Write-Host ("  [last optimization] " + (Get-LastStatus)) -ForegroundColor DarkYellow
    Write-Host ""
    Write-Host ("  Progress: [" + $bar + "]  " + $doneCount + "/" + $pairs.Count + "  (" + $pct + "%)") -ForegroundColor White
    Write-Host ("  Elapsed:  " + $elapsedStr)
    Write-Host ("  ETA left: ~" + $estLeftStr)
    Write-Host ""

    if ($wfAlive) {
        Write-Host "  STATUS: OPTIMIZATION RUNNING" -ForegroundColor Green
    } elseif ($doneCount -ge $pairs.Count) {
        Write-Host "  STATUS: ALL DONE (all pairs optimized)" -ForegroundColor Green
    } else {
        Write-Host ("  STATUS: FAILED/STOPPED - done " + $doneCount + "/" + $pairs.Count) -ForegroundColor Red
    }

    if ($activePairs.Count -gt 0) {
        Write-Host ""
        Write-Host "  Active optimizer.py:" -ForegroundColor DarkCyan
        foreach ($k in ($activePairs.Keys | Sort-Object)) {
            Write-Host ("    " + $k + "  (PID " + $activePairs[$k] + ")")
        }
    } elseif (-not $wfAlive) {
        Write-Host ""
        Write-Host "  (no active processes - completed or crashed)" -ForegroundColor DarkGray
    }

    if ($leftPairs.Count -gt 0) {
        Write-Host ""
        Write-Host ("  Pairs remaining: " + $leftPairs.Count) -ForegroundColor Yellow
        Write-Host ("  Current batch (first 4): " + (@($leftPairs | Select-Object -First 4) -join ", "))
    }

    if ($donePairs.Count -gt 0) {
        Write-Host ""
        Write-Host ("  Completed: " + $donePairs -join ", ") -ForegroundColor Green
    }

    $errCands = Get-ChildItem "$logsDir\optimization_*${today}*.csv" -ErrorAction SilentlyContinue |
        Where-Object { $_.Length -lt 60 }
    if ($errCands) {
        Write-Host ""
        Write-Host "  Empty CSVs (fold with no profitable runs):" -ForegroundColor Magenta
        foreach ($e in $errCands) {
            $errContent = (Get-Content $e.FullName -TotalCount 1 -ErrorAction SilentlyContinue)
            Write-Host ("    " + $e.Name + ": " + $errContent) -ForegroundColor Magenta
        }
    }

    Write-Host ""
    Write-Host "  (auto-refresh every 10 sec...)" -ForegroundColor DarkGray

    $cycle++
    Start-Sleep -Seconds 10

    if (-not $wfAlive -and $doneCount -ge $pairs.Count) { break }
}
