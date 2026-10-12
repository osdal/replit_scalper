# Обёртка безопасного деплоя api-server: mkdir -P + scp + вызов deploy_api.sh.
#
#   powershell -File tools\deploy.ps1 artifacts/api-server/src/routes/trades.ts
#   powershell -File tools\deploy.ps1 artifacts/api-server/src/routes/trades.ts `
#                                     artifacts/api-server/src/routes/trading.ts
#
# Почему .ps1, а не .sh: bash в этом окружении — это WSL, который не умеет
# исполнять Windows-бинарники (ssh.exe падает с ENOEXEC), а собственный ssh WSL
# висит на IdentityFile ~/.ssh/oracle_bot_key — в WSL этого файла нет.
# PowerShell-шелл тула использует штатный Windows OpenSSH, который читает
# C:\Users\osdal\.ssh\config и работает. Проверено.
#
# Делает по порядку:
#   1. проверяет, что файлы есть локально
#   2. снимает список ботов, которые должны работать (is_running=1)
#   3. mkdir -p приёмника на Oracle
#   4. scp файлов в _incoming/
#   5. запускает tools/deploy_api.sh (бэкап -> baseline -> копирование ->
#      рестарт -> смоук -> откат при FAIL)
#   6. поднимает обратно ботов, которые должны были работать (рестарт
#      api-live их убивает, в live AUTO_RESTART_BOTS=false)

[CmdletBinding()]
param(
    # ValueFromRemainingArguments не работает с `powershell -File`,
    # поэтому просто позиционный string[] — он сам соберёт все оставшиеся аргументы.
    [Parameter(Mandatory = $true, Position = 0)]
    [string[]]$Files,

    [string]$Host_ = "oracle-bot",
    [string]$Root = "/opt/trading-bot",
    [string]$LiveApi = "http://localhost:5001",
    [switch]$NoBotRecovery,
    [switch]$DryRun
)

$ErrorActionPreference = "Stop"
$Incoming = "$Root/_incoming"

function Invoke-Ssh([string]$Command) {
    Write-Host "  [ssh] $Command" -ForegroundColor DarkGray
    & ssh $Host_ $Command
    if ($LASTEXITCODE -ne 0) { throw "ssh завершился с кодом $LASTEXITCODE : $Command" }
}

Write-Host "=== 1/4 проверка локальных файлов ===" -ForegroundColor Cyan
foreach ($f in $Files) {
    if (-not (Test-Path -LiteralPath $f)) { throw "не найден локальный файл: $f" }
    Write-Host "  ok $f"
}

Write-Host "=== 2/4 боты, которые должны работать ===" -ForegroundColor Cyan
# Кого поднимать после деплоя. Два источника, потому что в live
# AUTO_RESTART_BOTS=false, а api-live при старте сбрасывает is_running=0 у ботов,
# чей процесс уже мёртв (docker compose restart убивает всех). Поэтому надеяться
# только на is_running нельзя — он к моменту опроса может быть уже обнулён.
#   1) is_running=1 на момент опроса (актуально, если боты живы)
#   2) запасной список на хосте, куда пишем после каждого успешного восстановления
$BotsFile = "$Root/.live_bots_running"
$wantRunning = @()
try {
    $raw = & ssh $Host_ "curl -s '$LiveApi/api/bots'" 2>$null
    $bots = $raw | ConvertFrom-Json
    $wantRunning = @($bots | Where-Object { $_.is_running } | ForEach-Object { $_.symbol })
} catch {
    Write-Host "  (не удалось опросить /bots: $_)" -ForegroundColor Yellow
}
if ($wantRunning.Count -eq 0) {
    $saved = & ssh $Host_ "cat $BotsFile 2>/dev/null" 2>$null
    if ($saved) {
        $wantRunning = @($saved -split '\s+' | Where-Object { $_ })
        Write-Host "  (is_running пуст — беру сохранённый список: $($wantRunning -join ', '))" -ForegroundColor Yellow
    }
}
Write-Host "  должно работать: $($wantRunning -join ', ')"

Write-Host "=== 3/4 mkdir -p + scp в $Incoming/ ===" -ForegroundColor Cyan
foreach ($f in $Files) {
    $remoteDir = "$Incoming/" + (Split-Path -Parent $f).Replace('\', '/')
    Invoke-Ssh "mkdir -p '$remoteDir'"
    & scp -q $f "${Host_}:$Incoming/$($f.Replace('\','/'))"
    if ($LASTEXITCODE -ne 0) { throw "scp не удался для $f" }
    Write-Host "  отправлен $f"
}

Write-Host "=== 4/4 деплой на Oracle ===" -ForegroundColor Cyan
$paths = ($Files | ForEach-Object { $_.Replace('\', '/') }) -join ' '
$deployCmd = "bash $Root/tools/deploy_api.sh $paths"
if ($DryRun) { $deployCmd = "DRY_RUN=1 $deployCmd" }

& ssh $Host_ $deployCmd
$rc = $LASTEXITCODE
Write-Host ""
if ($rc -ne 0) {
    Write-Host "=== ДЕПЛОЙ ОТКАТЕН ИЛИ УПАЛ (код $rc) ===" -ForegroundColor Red
    Write-Host "Подробности выше. Бэкап: ${Root}/_backup_api_*" -ForegroundColor Red
    exit $rc
}

Write-Host "=== ДЕПЛОЙ ПРИНЯТ ===" -ForegroundColor Green

if (-not $NoBotRecovery -and -not $DryRun -and $wantRunning.Count -gt 0) {
    Write-Host ""
    Write-Host "=== восстановление ботов (рестарт api-live их убил) ===" -ForegroundColor Cyan
    Start-Sleep -Seconds 8
    foreach ($sym in $wantRunning) {
        $res = & ssh $Host_ "curl -s -X POST '$LiveApi/api/bots/$sym/start'" 2>$null
        if ($res -match '"success":true') {
            Write-Host "  поднят $sym" -ForegroundColor Green
        } else {
            Write-Host "  НЕ поднят $sym -> $res" -ForegroundColor Yellow
        }
    }
    Start-Sleep -Seconds 30
    $raw = & ssh $Host_ "docker exec replit_scalper-api-live ps -eo args | grep main.py | grep -v grep | wc -l" 2>$null
    Write-Host "  процессов ботов в api-live: $($raw.Trim())"
    # Сохраняем список, чтобы следующий деплой знал, кого поднимать, даже если
    # боты к тому моменту уже остановились.
    & ssh $Host_ "printf '%s\n' '$($wantRunning -join ' ')' > $BotsFile" 2>$null | Out-Null
    Write-Host "  список сохранён в $BotsFile" -ForegroundColor DarkGray
}

Write-Host ""
Write-Host "=== ГОТОВО ===" -ForegroundColor Green
