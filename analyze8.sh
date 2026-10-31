#!/bin/bash
echo "=== recovery_config.yaml (testnet) ==="
find /opt/trading-bot -name "recovery_config*.yaml" | head
for f in $(find /opt/trading-bot -name "recovery_config*.yaml" | head -4); do echo "--- $f"; cat "$f"; done
echo
echo "=== daily_loss / trading control из API ==="
curl -s http://localhost:5000/api/trading/control | head -c 600
echo
echo "=== slots.check / loadOccupiedSymbols ==="
grep -n "def check" -A 70 /opt/trading-bot/artifacts/api-server/src/lib/slots.ts 2>/dev/null | head -90
echo
echo "=== loadOccupiedSymbols ==="
sed -n '160,210p' /opt/trading-bot/artifacts/api-server/src/routes/trading.ts
