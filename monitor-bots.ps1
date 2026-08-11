# monitor-bots.ps1
# Checks bot health every 6 hours and sends Telegram report.
# Scheduled Task: 00:00, 06:00, 12:00, 18:00
# Run: powershell -ExecutionPolicy Bypass -File monitor-bots.ps1

$ErrorActionPreference = "Stop"

$botDir    = "C:\DATA\bots\replit_scalper\bot"
$logsDir   = Join-Path $botDir "logs"
$apiUrl    = "http://localhost:5000"
$pairs     = @(
    "ATOMUSDT","DOGEUSDT","ETHUSDT","SOLUSDT","INJUSDT","OPUSDT",
    "POLUSDT","ONTUSDT","HBARUSDT","NEARUSDT","SUIUSDT","FILUSDT",
    "KASUSDT","XRPUSDT","LINKUSDT","DOTUSDT","TRXUSDT","BTCUSDT",
    "BNBUSDT","AVAXUSDT","ADAUSDT","1000PEPEUSDT","ARBUSDT","APTUSDT"
)

$telegramToken = "8866471817:AAEPKrfudwcXwQ5ClH8N9-CntG-Rqnu_uAU"
$telegramChatId = "-1004343152214"

function Send-Telegram {
    param([string]$Text)
    $url = "https://api.telegram.org/bot$telegramToken/sendMessage"
    try {
        Invoke-RestMethod -Uri $url -Method Post -Body @{chat_id=$telegramChatId; text=$Text; parse_mode="HTML"} | Out-Null
    } catch {
        Write-Warning "Telegram send failed: $_"
    }
}

function Get-AllBots {
    try {
        $r = Invoke-RestMethod -Uri "$apiUrl/api/bots" -Method Get -ErrorAction Stop
        return $r
    } catch {
        return @()
    }
}

$now = Get-Date -Format "yyyy-MM-dd HH:mm:ss"
Write-Host "[$now] Starting bot health check..."

$apiAlive = $false
try {
    $health = Invoke-RestMethod -Uri "$apiUrl/api/healthz" -Method Get -ErrorAction Stop
    $apiAlive = $true
} catch {
    $apiAlive = $false
}

if (-not $apiAlive) {
    $msg = "[FAIL] API server is down!`n$apiUrl/api/healthz not responding."
    Send-Telegram $msg
    Write-Host $msg
    exit 1
}

# ---------- Helpers: live prices & trades ----------
function Get-LivePrice {
    param([string]$symbol)
    try {
        $url = "https://fapi.binance.com/fapi/v1/ticker/price?symbol=$symbol"
        $r = Invoke-RestMethod -Uri $url -Method Get -TimeoutSec 10 -ErrorAction Stop
        return [double]$r.price
    } catch {
        return -1
    }
}

function Get-BotPrice {
    param([object]$bot)
    try {
        return [double]$bot.current_price
    } catch {
        return -1
    }
}

# Count how many signals/trades were opened within the last N hours.
# A trade = record in /api/trades with an entry_time inside the window.
# NOTE: This is informational. Zero signals can be normal (flat market),
# but combined with frozen prices it becomes a strong stall indicator.
function Get-RecentSignals {
    param([int]$hours)
    $result = @{}
    try {
        $resp = Invoke-RestMethod -Uri "$apiUrl/api/trades" -Method Get -TimeoutSec 10 -ErrorAction Stop
        $trades = $resp.trades | Where-Object { $_ -and $_.entry_time }
        $cut = (Get-Date).ToUniversalTime().AddHours(-$hours)
        foreach ($t in $trades) {
            $ts = $null
            try { $ts = [datetime]::Parse($t.entry_time) } catch { continue }
            if ($ts -ge $cut) {
                $sym = ($t.symbol).ToUpper()
                if ($result.ContainsKey($sym)) { $result[$sym]++ } else { $result[$sym] = 1 }
            }
        }
    } catch {}
    return $result
}

$allBots = Get-AllBots
$runningCount = ($allBots | Where-Object { $_.is_running }).Count
$totalCount = $allBots.Count

$issues = @()
$staleList = @()
$frozenPriceList = @()
$okList = @()

$maxAgeSeconds = 900
$nowUtc = (Get-Date).ToUniversalTime()

# Live prices fetched once per pair; used to detect bots that are "alive"
# (heartbeat) but NOT actually processing fresh candles (price frozen / stale).
$livePriceCache = @{}

$priceDeviationPct = 5.0  # allowed deviation between bot current_price and Binance live

foreach ($pair in $pairs) {
    $bot = $allBots | Where-Object { $_.symbol -eq $pair }
    if (-not $bot) {
        $issues += "$pair : not found in API"
        continue
    }
    if (-not $bot.is_running) {
        $issues += "$pair : stopped in API"
        continue
    }

    # Heartbeat freshness check
    $hbStr = $bot.last_heartbeat
    $hbOk = $true
    $age = -1
    if (-not $hbStr) {
        $staleList += "$pair (no heartbeat)"
        $hbOk = $false
    } else {
        try {
            $hbTime = [datetime]::Parse($hbStr)
            $age = [math]::Round(($nowUtc - $hbTime).TotalSeconds)
        } catch {
            $staleList += "$pair (invalid heartbeat: $hbStr)"
            $hbOk = $false
        }
        if ($hbOk -and $age -gt $maxAgeSeconds) {
            $staleList += "$pair (heartbeat stale ${age}s)"
            $hbOk = $false
        }
    }
    if (-not $hbOk) { continue }

    # Price freshness / live-processing check:
    # If bot's current_price is 0 (fresh startup) -> skip (it sends 0 on startup).
    # Else compare to Binance live futures price. If way off -> bot not feeding fresh prices.
    $botPrice = Get-BotPrice $bot
    if ($botPrice -gt 0) {
        if (-not $livePriceCache.ContainsKey($pair)) {
            $livePriceCache[$pair] = Get-LivePrice $pair
        }
        $live = $livePriceCache[$pair]
        if ($live -gt 0) {
            $dev = [math]::Abs(($botPrice - $live) / $live) * 100
            if ($dev -gt $priceDeviationPct) {
                $frozenPriceList += "$pair (price ${botPrice} vs Binance ${live}, dev=$([math]::Round($dev,1))%)"
                continue
            }
        }
    }

    $okList += $pair
}

# Gather signal/trade activity over the last 6h (informational)
$signalWindowHours = 6
$recentSignals = Get-RecentSignals -hours $signalWindowHours
$activeSymbols = @($recentSignals.Keys)
$totalRecentSignals = 0
foreach ($k in $recentSignals.Keys) { $totalRecentSignals += $recentSignals[$k] }

$body = "[BOT HEALTH] $now`n"
$body += "Total: $totalCount | Running: $runningCount`n`n"

$signalsNote = if ($totalRecentSignals -eq 0) {
    "Last $signalWindowHours h: no new signals/trades. If prices are also frozen this is a stall."
} else {
    "Last $signalWindowHours h: $totalRecentSignals new signal(s) across: $($activeSymbols -join ', ')"
}

if ($issues.Count -gt 0) {
    $body += "[FAIL] Issues ($($issues.Count)):`n"
    $body += ($issues | ForEach-Object { "- $_" }) -join "`n"
    $body += "`n"
}
if ($staleList.Count -gt 0) {
    $body += "[WARN] Stale heartbeat ($($staleList.Count)):`n"
    $body += ($staleList | ForEach-Object { "- $_" }) -join "`n"
    $body += "`n"
}
if ($frozenPriceList.Count -gt 0) {
    $body += "[WARN] Frozen price / not processing ($($frozenPriceList.Count)):`n"
    $body += ($frozenPriceList | ForEach-Object { "- $_" }) -join "`n"
    $body += "`n"
}
if ($okList.Count -gt 0) {
    $body += "[OK] Healthy ($($okList.Count)): "
    $body += ($okList -join ", ")
    $body += "`n`n"
}
$body += "[SIGNALS] $signalsNote"

Send-Telegram $body
Write-Host $body
