$inv = [System.Globalization.CultureInfo]::InvariantCulture
$target = [datetime]::ParseExact('2026-11-10 12:37:00', 'yyyy-MM-dd HH:mm:ss', $inv)
$sec = [int][math]::Ceiling(($target - [datetime]::UtcNow).TotalSeconds)
Write-Output ("now_utc=" + [datetime]::UtcNow.ToString('s') + " sleep_s=" + $sec)
if ($sec -gt 0) { Start-Sleep -Seconds $sec }
ssh -o ConnectTimeout=30 -o ServerAliveInterval=10 oracle-bot "bash /tmp/check_resume.sh 2>&1"
