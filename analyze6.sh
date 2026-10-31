#!/bin/bash
echo "=== testnet atom.yaml ==="
grep -E "paper_balance|position_size_pct|position_size_usd|max_position_notional|fixed_risk|leverage|reverse_|expired_after|max_positions" /opt/trading-bot/bot/configs/testnet/config_atom.yaml
echo
echo "=== live atom.yaml ==="
grep -E "paper_balance|position_size_pct|position_size_usd|max_position_notional|fixed_risk|leverage|reverse_|expired_after|max_positions" /opt/trading-bot/bot/configs/live/config_atom.yaml
echo
echo "=== live btc.yaml ==="
grep -E "paper_balance|position_size_pct|max_position_notional|fixed_risk|leverage" /opt/trading-bot/bot/configs/live/config_btc.yaml
echo
echo "=== sizing code order_manager.py 1880-1975 ==="
sed -n '1880,1975p' /opt/trading-bot/bot/order_manager.py
