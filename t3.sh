#!/bin/bash
cd /opt/trading-bot/bot/logs/testnet || exit 1
L=api_filusdt_stdout.log
echo "=== FIL 09:44-09:52 ==="
grep -aE '2026-11-10 09:(4[4-9]|5[0-2])' "$L" | grep -aiE 'SL|REVERSE|SYNC|close|CLOSE|hit|chain|expir|ban|position' | head -40
echo
L=api_dogeusdt_stdout.log
echo "=== DOGE 07:25-07:35 ==="
grep -aE '2026-11-10 07:(2[5-9]|3[0-5])' "$L" | grep -aiE 'SL|REVERSE|SYNC|close|CLOSE|hit|chain|expir|ban|position' | head -40
echo
echo "=== DOGE 04:24-04:34 ==="
grep -aE '2026-11-10 04:(2[4-9]|3[0-4])' "$L" | grep -aiE 'SL|REVERSE|SYNC|close|CLOSE|hit|chain|expir|ban|position' | head -40
echo
L=api_adausdt_stdout.log
echo "=== ADA 00:04-00:14 ==="
grep -aE '2026-11-10 00:(0[4-9]|1[0-4])' "$L" | grep -aiE 'SL|REVERSE|SYNC|close|CLOSE|hit|chain|expir|ban|position' | head -40
echo
L=api_lskusdt_stdout.log
echo "=== LSK 2026-11-09 22:14-22:24 ==="
grep -aE '2026-11-09 22:(1[4-9]|2[0-4])' "$L" | grep -aiE 'SL|REVERSE|SYNC|close|CLOSE|hit|chain|expir|ban|position' | head -40
echo
echo "=== LSK 2026-11-08 21:42-21:52 ==="
grep -aE '2026-11-08 21:(4[2-9]|5[0-2])' "$L" | grep -aiE 'SL|REVERSE|SYNC|close|CLOSE|hit|chain|expir|ban|position' | head -40
echo
echo "=== LSK 2026-11-07 21:39-21:49 ==="
grep -aE '2026-11-07 21:(3[9]|4[0-9])' "$L" | grep -aiE 'SL|REVERSE|SYNC|close|CLOSE|hit|chain|expir|ban|position' | head -40
