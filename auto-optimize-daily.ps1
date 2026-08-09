# auto-optimize-daily.ps1
# Ежедневная walk-forward оптимизация без остановки ботов во время прогона.
# 1) Запускает walk_forward_opt.py с низкой нагрузкой (не душит боты)
# 2) После завершения обновляет конфиги и перезапускает ботов через API
#
# Запуск вручную:
#   powershell -ExecutionPolicy Bypass -File auto-optimize-daily.ps1
#
# Параметры:
#   -Trials          trials на фолд (по умолчанию 200)
#   -Folds           число фолдов (10, 20)
#   -DaysBack        сколько дней истории назад (по умолчанию 35)
#   -RegisterTask    зарегистрировать ежедневную задачу в Task Scheduler
#   -NoRestart       не перезапускать ботов после оптимизации

param(
    [int]$Trials = 200,
    [int]$Folds = 2,
    [int]$DaysBack = 35,
    [switch]$RegisterTask,
    [switch]$RegisterOnly,
    [switch]$NoRestart
)

# Регистрация задачи без запуска оптимизации (удобно вызывать один раз)
if ($RegisterOnly) {
    $scriptPathAbs = Join-Path $PSScriptRoot "auto-optimize-daily.ps1"
    $taskName = "AutoOptimizeWalkForward"
    $existing = Get-ScheduledTask -TaskName $taskName -ErrorAction SilentlyContinue
    if ($existing) { Unregister-ScheduledTask -TaskName $taskName -Confirm:$false }
    $action = New-ScheduledTaskAction -Execute "powershell.exe" `
        -Argument "-NoProfile -ExecutionPolicy Bypass -File `"$scriptPathAbs`""
    $trigger = New-ScheduledTaskTrigger -Daily -At 4am
    $settings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries `
        -DontStopIfGoingOnBatteries -StartWhenAvailable -ExecutionTimeLimit (New-TimeSpan -Hours 6)
    $principal = New-ScheduledTaskPrincipal -UserId "$env:USERDOMAIN\$env:USERNAME" -LogonType Interactive -RunLevel Limited
    try {
        Register-ScheduledTask -TaskName $taskName -Action $action -Trigger $trigger `
            -Settings $settings -Principal $principal -Force | Out-Null
        Write-Host "Scheduled task '$taskName' registered: daily 04:00"
        Write-Host "Script: $scriptPathAbs"
    } catch {
        Write-Host "Register FAILED: $($_.Exception.Message)" -ForegroundColor Yellow
        Write-Host "Зарегистрируй задачу от имени администратора (правая кнопка -> Запуск от имени администратора)."
        exit 1
    }
    exit 0
}

$ErrorActionPreference = "Stop"
$scriptDir  = $PSScriptRoot
$botDir     = Join-Path $scriptDir "bot"
$logsDir    = Join-Path $botDir "logs"
$logFile    = Join-Path $logsDir ("auto-opt_" + (Get-Date -Format "yyyyMMdd_HHmmss") + ".log")
$api        = "http://localhost:5000/api"

# Перенаправляем весь вывод в лог и консоль
Start-Transcript -Path $logFile -Force | Out-Null

# 24 торгуемых пары (для подсчёта завершённых и выявления неудач)
$TRADE_PAIRS = @(
    "ATOMUSDT","DOGEUSDT","ETHUSDT","SOLUSDT","INJUSDT","OPUSDT",
    "POLUSDT","ONTUSDT","HBARUSDT","NEARUSDT","SUIUSDT","FILUSDT",
    "KASUSDT","XRPUSDT","LINKUSDT","DOTUSDT","TRXUSDT","BTCUSDT",
    "BNBUSDT","AVAXUSDT","ADAUSDT","1000PEPEUSDT","ARBUSDT","APTUSDT"
)

# Файл статуса - сюда пишем результат, чтобы легко проверять сбой
$statusFile = Join-Path $logsDir "optimization_status.txt"

function Write-AutoStatus {
    param(
        [string]$Outcome,     # OK / PARTIAL / FAILED
        [string]$Message
    )
    $line = (Get-Date -Format "yyyy-MM-dd HH:mm:ss") + " | " + $Outcome + " | " + $Message
    try { [System.IO.File]::WriteAllText($statusFile, $line + "`n", (New-Object System.Text.UTF8Encoding $false)) } catch {}
    return $line
}

function Get-PairsDoneToday {
    $today = Get-Date -Format "yyyyMMdd"
    $done = @{}
    Get-ChildItem "$logsDir\optimization_*${today}*.csv" -ErrorAction SilentlyContinue |
        ForEach-Object {
            $m = [regex]::Match($_.Name, "optimization_([A-Z]+)_\d+")
            if ($m.Success) { $done[$m.Groups[1].Value] = $true }
        }
    $doneCount = 0
    $failedPairs = @()
    foreach ($p in $TRADE_PAIRS) {
        if ($done[$p]) { $doneCount++ } else { $failedPairs += $p }
    }
    return @{ done = $doneCount; failed = $failedPairs }
}

# ── Telegram-уведомления ─────────────────────────────────────────────
$tgToken = ""
$tgChat  = ""

function Load-TelegramConfig {
    # Читаем из корневого .env
    $envPath = Join-Path $scriptDir ".env"
    if (Test-Path $envPath) {
        Get-Content $envPath -ErrorAction SilentlyContinue | ForEach-Object {
            $line = $_.Trim()
            if ($line -and -not $line.StartsWith("#") -and $line.Contains("=")) {
                $kv = $line -split "=", 2
                $key = $kv[0].Trim(); $val = $kv[1].Trim()
                if ($key -eq "TELEGRAM_BOT_TOKEN") { $script:tgToken = $val }
                elseif ($key -eq "TELEGRAM_CHAT_ID") { $script:tgChat = $val }
            }
        }
    }
}

function Send-Telegram {
    param([string]$Text)
    if (-not $script:tgToken -or -not $script:tgChat) {
        Write-Host "      [TG] skips: TELEGRAM_BOT_TOKEN/CHAT_ID not set" -ForegroundColor DarkGray
        return
    }
    $uri = "https://api.telegram.org/bot$($script:tgToken)/sendMessage"
    $body = @{ chat_id = $script:tgChat; text = $Text; parse_mode = "HTML" }
    try {
        $json = $body | ConvertTo-Json -Compress
        $utf8 = [System.Text.Encoding]::UTF8.GetBytes($json)
        $resp = Invoke-RestMethod -Uri $uri -Method Post -Body $utf8 -ContentType "application/json; charset=utf-8" -ErrorAction Stop
        Write-Host "      [TG] sent OK" -ForegroundColor DarkGreen
    } catch {
        Write-Host "      [TG] send failed: $($_.Exception.Message)" -ForegroundColor Yellow
    }
}

Load-TelegramConfig

Set-Content -Path $statusFile -Value "START | $(Get-Date -Format 'yyyy-MM-dd HH:mm:ss') | optimization begin" -Encoding UTF8

Write-Host "============================================"
Write-Host "  Auto-Optimize (daily walk-forward)"
Write-Host "  $(Get-Date -Format 'yyyy-MM-dd HH:mm:ss')"
Write-Host "  Trials=$Trials Folds=$Folds DaysBack=$DaysBack"
Write-Host "  Status file: $statusFile"
Write-Host "============================================"
Write-Host ""

# ── Рабочий период ───────────────────────────────────────────────
$endDate   = (Get-Date).AddDays(-1).ToString("yyyy-MM-dd")
$startDate = (Get-Date).AddDays(-$DaysBack).ToString("yyyy-MM-dd")
Write-Host "Period: $startDate -> $endDate"
Write-Host ""

# ── 1. Проверяем API (нужен для перезапуска ботов) ──────────────
$apiUp = $false
try {
    Invoke-RestMethod "$api/bots" -ErrorAction Stop | Out-Null
    $apiUp = $true
    Write-Host "[1/4] API is UP" -ForegroundColor Green
} catch {
    Write-Host "[1/4] API is DOWN: $($_.Exception.Message)" -ForegroundColor Yellow
}
Write-Host ""

# ── 2. Walk-forward оптимизация (низкая нагрузка, боты не трогаем) ─
Write-Host "[2/4] Running walk-forward optimization..." -ForegroundColor Cyan
Write-Host "      (batch-size=2, jobs=1 - чтобы не потерять CPU для живых ботов)"
$wfArgs = @(
    "walk_forward_opt.py",
    "--trials", $Trials,
    "--folds", $Folds,
    "--jobs", "1",
    "--batch-size", "2",
    "--start", $startDate,
    "--end", $endDate,
    "--skip-mc"
)

$before = @(Get-ChildItem "$logsDir\optimization_*" -ErrorAction SilentlyContinue).Count

try {
    $proc = Start-Process -FilePath "python" -ArgumentList $wfArgs `
        -WorkingDirectory $botDir -NoNewWindow -PassThru -Wait
    $exitCode = $proc.ExitCode
} catch {
    $exitCode = -999
    Write-AutoStatus "FAILED" "walk_forward не запустился: $($_.Exception.Message)"
}

$after = @(Get-ChildItem "$logsDir\optimization_*" -ErrorAction SilentlyContinue).Count
$stats = Get-PairsDoneToday

# Признак аварийного падения: вышли с ненулевым кодом ИЛИ рекордных CSV нет
if ($exitCode -ne 0) {
    Write-Host "[2/4] Walk-forward FAILED: ExitCode=$exitCode" -ForegroundColor Red
    Write-AutoStatus "FAILED" "optimizer вышел с кодом $exitCode; пар завершено $($stats.done)/$(($TRADE_PAIRS.Count))"
    Send-Telegram -Text "⚠️ <b>AUTO-OPTIMIZATION FAILED</b> ($(Get-Date -Format 'dd.MM HH:mm'))
Optimizer exited with code $exitCode
Pairs done: $($stats.done)/$(($TRADE_PAIRS.Count))
Log: $logFile"
    Stop-Transcript | Out-Null
    exit 1
}

if ($stats.done -eq 0 -and $after -le $before) {
    Write-Host "[2/4] Walk-forward упал: не создано ни одного CSV" -ForegroundColor Red
    Write-AutoStatus "FAILED" "не создано ни одного CSV (циклический запуск/падение)"
    Send-Telegram -Text "⚠️ <b>AUTO-OPTIMIZATION FAILED</b> ($(Get-Date -Format 'dd.MM HH:mm'))
No CSV files created (possible crash/infinite loop)
Log: $logFile"
    Stop-Transcript | Out-Null
    exit 1
}

if ($stats.failed.Count -gt 0) {
    Write-Host "[2/4] Часть пар не была оптимизирована: $($stats.failed -join ',')" -ForegroundColor Yellow
    Write-AutoStatus "PARTIAL" "завершено $($stats.done)/$(($TRADE_PAIRS.Count)) пар; ошибки: $($stats.failed -join ',')"
} else {
    Write-Host "[2/4] Walk-forward complete: $($stats.done)/$(($TRADE_PAIRS.Count)) пар" -ForegroundColor Green
    Write-AutoStatus "OPT_DONE" "завершено $($stats.done)/$(($TRADE_PAIRS.Count)) пар"
}
Write-Host ""

# ── 3. Перезапуск ботов через API (если запущены) ────────────────
if (-not $NoRestart -and $apiUp) {
    Write-Host "[3/4] Restarting bots via API..." -ForegroundColor Cyan

    # Получаем список пар из конфигов
    $symbols = Get-ChildItem -Path $botDir -Filter "config_*.yaml" |
        Where-Object { $_.Name -notmatch "recovery_config" } |
        ForEach-Object { $_.BaseName -replace "^config_", "" | ForEach-Object { $_.ToUpper() + "USDT" } } |
        Sort-Object

    Write-Host "      Found $($symbols.Count) bot configs"

    # Основи: сначала остановить все, потом перечитать конфиги, потом запустить
    try {
        Invoke-RestMethod -Method POST "$api/refresh" -ErrorAction Stop | Out-Null
        Write-Host "      Configs reloaded (all bots stopped)"
    } catch {
        Write-Host "      /refresh failed: $($_.Exception.Message)" -ForegroundColor Yellow
    }

    Start-Sleep -Seconds 3

    $started = 0; $failed = @()
    foreach ($sym in $symbols) {
        try {
            $r = Invoke-RestMethod -Method POST "$api/bots/$sym/start" -ErrorAction Stop
            if ($r.success) { $started++ } else { $failed += "$sym ($($r.message))" }
        } catch {
            $failed += "$sym ($($_.Exception.Message))"
        }
        Start-Sleep -Milliseconds 150
    }

    Write-Host "      Started: $started/$($symbols.Count)"
    if ($failed.Count -gt 0) {
        Write-Host "      FAILED:" -ForegroundColor Red
        $failed | ForEach-Object { Write-Host "        $_" -ForegroundColor Red }
        Write-AutoStatus "RESTART_PARTIAL" "боты перезапущены $started/$($symbols.Count); ошибки: $($failed -join '; ')"
    } else {
        Write-AutoStatus "RESTART_OK" "все $started ботов перезапущены"
    }
} else {
    Write-Host "[3/4] Bot restart SKIPPED (NoRestart or API down)"
    Write-AutoStatus "RESTART_SKIPPED" "боты не перезапускались (NoRestart или API недоступен)"
}
Write-Host ""

# ── 4. Итог + уведомление в Telegram ───────────────────────────────
Write-Host "[4/4] Done. Log: $logFile"
Write-Host "============================================"
Write-Host ""

# Итоговое сообщение в Telegram
if ($stats.failed.Count -gt 0) {
    Send-Telegram -Text "⚠️ <b>AUTO-OPTIMIZATION (partial)</b> ($(Get-Date -Format 'dd.MM HH:mm'))
Pairs done: $($stats.done)/$(($TRADE_PAIRS.Count))
Failed/skipped: $($stats.failed -join ', ')
Details: $logFile"
} else {
    Send-Telegram -Text "✅ <b>AUTO-OPTIMIZATION COMPLETE</b> ($(Get-Date -Format 'dd.MM HH:mm'))
Pairs done: $($stats.done)/$(($TRADE_PAIRS.Count))
Configs updated, bots restarted
Details: $logFile"
}

Stop-Transcript | Out-Null

# ── Опционально: зарегистрировать ежедневную задачу ─────────────
if ($RegisterTask) {
    $taskName = "AutoOptimizeWalkForward"
    $scriptPath = Join-Path $scriptDir "auto-optimize-daily.ps1"

    # Удаляем старую задачу если есть
    $existing = Get-ScheduledTask -TaskName $taskName -ErrorAction SilentlyContinue
    if ($existing) {
        Unregister-ScheduledTask -TaskName $taskName -Confirm:$false
        Write-Host "Removed old scheduled task $taskName"
    }

    $action = New-ScheduledTaskAction -Execute "powershell.exe" `
        -Argument "-NoProfile -ExecutionPolicy Bypass -File `"$scriptPath`""

    # Останавливается через запуск каждые 3 часа? Нет - ежедневно в 4:00
    $trigger = New-ScheduledTaskTrigger -Daily -At 4am

    $settings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries `
        -DontStopIfGoingOnBatteries -StartWhenAvailable -ExecutionTimeLimit (New-TimeSpan -Hours 6)

    $principal = New-ScheduledTaskPrincipal -UserId "$env:USERDOMAIN\$env:USERNAME" -LogonType Interactive -RunLevel Limited

    try {
        Register-ScheduledTask -TaskName $taskName -Action $action -Trigger $trigger `
            -Settings $settings -Principal $principal -Force | Out-Null
    } catch {
        Write-Host "Register FAILED: $($_.Exception.Message)" -ForegroundColor Yellow
        Write-Host "Зарегистрируй задачу от имени администратора."
    }

    Write-Host ""
    Write-Host "Scheduled task '$taskName' registered: runs daily at 04:00"
    Write-Host "Execute manually anytime: $scriptPath"
}
