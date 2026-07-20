try {
  $r = Invoke-RestMethod -Uri 'http://localhost:5000/api/bots/BTCUSDT' -ErrorAction Stop
  Write-Host ("DB: leverage=" + $r.leverage + " risk_pct=" + $r.risk_pct + " sl_pct=" + $r.sl_pct + " tp1_pct=" + $r.tp1_pct + " tp2_pct=" + $r.tp2_pct)
} catch {
  Write-Host ("ERR: " + $_.Exception.Message)
}