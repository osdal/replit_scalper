# ============================================================
#  Запуск обоих стеков в Docker после перезагрузки компьютера.
#
#  Что делает:
#    1. Поднимает Docker Desktop, если он не запущен.
#    2. docker-compose up -d  -> testnet (5000/5173) + live (5001/5176).
#    3. Ждёт готовности обоих API.
#    4. Запускает live-ботов (сначала Arm, потом Start).
#
#  Testnet-боты поднимаются сами (AUTO_RESTART_BOTS по умолчанию true),
#  live — нет: в .env.live AUTO_RESTART_BOTS=false, это сделано намеренно,
#  чтобы реальные деньги не поехали торговать без явного действия оператора.
#
#  Запуск:
#    .\start_docker.ps1
#    .\start_docker.ps1 -LiveSymbols 1000PEPEUSDT,ETHUSDT
#    .\start_docker.ps1 -NoLiveBots          # только стеки, ботов не трогать
#    .\start_docker.ps1 -NoBrowser
# ============================================================
param(
    [string[]]$LiveSymbols = @("1000PEPEUSDT"),
    [switch]$NoLiveBots,
    [switch]$NoBrowser
)

Set-Location -LiteralPath $PSScriptRoot

function Wait-Api($url, $seconds) {
    for ($i = 0; $i -lt $seconds; $i++) {
        try {
            Invoke-RestMethod $url -ErrorAction Stop | Out-Null
            return $true
        } catch {
            Start-Sleep -Seconds 2
        }
    }
    return $false
}

Write-Host "============================================"
Write-Host "  Starting Docker stacks (testnet + live)"
Write-Host "============================================"

# 1. Docker Desktop
docker info *> $null
if ($LASTEXITCODE -ne 0) {
    $desktop = "$Env:ProgramFiles\Docker\Docker\Docker Desktop.exe"
    if (-not (Test-Path $desktop)) {
        Write-Error "[FAIL] Docker Desktop not found at $desktop"
        exit 1
    }
    Write-Host "[1/4] Starting Docker Desktop..."
    Start-Process $desktop
    for ($i = 0; $i -lt 60; $i++) {
        Start-Sleep -Seconds 3
        docker info *> $null
        if ($LASTEXITCODE -eq 0) { break }
    }
    if ($LASTEXITCODE -ne 0) {
        Write-Error "[FAIL] Docker did not become ready"
        exit 1
    }
}
Write-Host "[1/4] Docker is ready"

# 2. Контейнеры
Write-Host "[2/4] docker-compose up -d ..."
docker-compose up -d
if ($LASTEXITCODE -ne 0) {
    Write-Error "[FAIL] docker-compose up failed"
    exit 1
}

# 3. Готовность API
Write-Host "[3/4] Waiting for API servers..."
if (-not (Wait-Api "http://localhost:5000/api/bots" 90)) {
    Write-Warning "      testnet API (:5000) not ready - check: docker-compose logs api-testnet"
} else {
    Write-Host "      testnet API OK  - http://localhost:5000"
}
if (-not (Wait-Api "http://localhost:5001/api/bots" 90)) {
    Write-Warning "      live API (:5001) not ready - check: docker-compose logs api-live"
} else {
    Write-Host "      live API OK     - http://localhost:5001"
}

# 4. Live-боты
Write-Host "[4/4] Starting live bots..."
if ($NoLiveBots) {
    Write-Host "      skipped (-NoLiveBots)"
} elseif ($LiveSymbols.Count -eq 0) {
    Write-Host "      no symbols given"
} else {
    foreach ($sym in $LiveSymbols) {
        $u = $sym.ToUpper()
        try {
            # Arm обязателен: в live REQUIRE_ARM=true, без arm бот не открывает позиции.
            $arm = Invoke-RestMethod -Method Post -Uri "http://localhost:5001/api/bots/$u/arm" -TimeoutSec 15
            $start = Invoke-RestMethod -Method Post -Uri "http://localhost:5001/api/bots/$u/start" -TimeoutSec 60
            Write-Host ("      {0}: armed={1} start='{2}'" -f $u, $arm.armed, $start.message)
        } catch {
            Write-Warning "      ${u}: $($_.Exception.Message)"
        }
    }
}

Write-Host ""
Write-Host "============================================"
Write-Host "  TESTNET dashboard: http://localhost:5173"
Write-Host "  LIVE dashboard:    http://localhost:5176"
Write-Host "============================================"
Write-Host "Bot count:"
Write-Host "  testnet: (docker exec replit_scalper-api-testnet ps -eo args --no-headers | findstr /c:main.py | Measure-Object).Count"
Write-Host "  live:    (docker exec replit_scalper-api-live ps -eo args --no-headers | findstr /c:main.py | Measure-Object).Count"

if (-not $NoBrowser) {
    Start-Process "http://localhost:5176"
}
