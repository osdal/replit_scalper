#!/usr/bin/env python3
"""
Update all live configs to optimized $10 settings (same as testnet).
Run locally, then commit/push. Deploy to server but DON'T restart live bots yet.
"""
import yaml
import glob
import os

CONFIG_DIR = "bot/configs/live"

# Optimized settings for $10 deposit (matching testnet)
OPTIMIZED = {
    "leverage": 20,
    "position_size_pct": 2.0,
    "fixed_risk_usd": 0.10,
    "risk_pct": 1.0,
    "reverse_breakeven_pct": 1.5,
    "reverse_profit_pct": 0.5,
    "reverse_fee_buffer_pct": 0.1,
    "reverse_chain_max": 3,
    "max_position_notional_usd": 50.0,
    # Position limits (new fields):
    "expired_after_minutes": 120,
    "max_positions": 1,
    "max_positions_with_expired": 3,
    # Keep existing: trade_mode: manual, mode: live
}

def update_config(filepath):
    with open(filepath, 'r', encoding='utf-8') as f:
        data = yaml.safe_load(f) or {}
    
    changed = {}
    for key, value in OPTIMIZED.items():
        old = data.get(key)
        if old != value:
            data[key] = value
            changed[key] = (old, value)
    
    if changed:
        with open(filepath, 'w', encoding='utf-8') as f:
            yaml.dump(data, f, default_flow_style=False, allow_unicode=True, sort_keys=False)
        print(f"  UPDATED: {os.path.basename(filepath)}")
        for k, (old, new) in changed.items():
            print(f"    {k}: {old} -> {new}")
    else:
        print(f"  SKIP (no changes): {os.path.basename(filepath)}")
    
    return len(changed) > 0

def main():
    configs = sorted(glob.glob(os.path.join(CONFIG_DIR, "config_*.yaml")))
    print(f"Found {len(configs)} live configs\n")
    
    total_changed = 0
    for cfg in configs:
        if update_config(cfg):
            total_changed += 1
    
    print(f"\n=== Done: {total_changed}/{len(configs)} files updated ===")

if __name__ == "__main__":
    main()