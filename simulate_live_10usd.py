#!/usr/bin/env python3
"""
Simulation script for live bot with $10 deposit.
Tests various scenarios: entry sizing, reverse chains, cum loss cap.
Usages actual logic from bot modules where possible.
"""
import math
import sys
from dataclasses import dataclass, field
from typing import Optional, List, Tuple
from enum import Enum

# ──────────────────────────────────────────────────────────────
# Simulated config (mirrors bot/config.py Config)
# ──────────────────────────────────────────────────────────────
@dataclass
class SimConfig:
    symbol: str = "ATOMUSDT"
    leverage: int = 75
    position_size_pct: float = 0.6      # % of free balance for margin
    position_size_usd: float = 0.0      # explicit margin USD (overrides pct)
    fixed_risk_usd: float = 0.50        # risk in USD at SL
    fixed_notional_usd: float = 0.0     # margin USD (legacy)
    fixed_qty: float = 0.0              # fixed quantity
    margin_pct: float = 0.0             # % of total balance for margin
    risk_pct: float = 3.0               # % of balance risked at SL distance
    sl_pct: float = 0.8                 # SL % from entry
    tp1_pct: float = 1.0                # TP1 %
    tp2_pct: float = 1.0                # TP2 %
    tp1_close_pct: float = 100.0        # % to close at TP1
    reverse_breakeven_pct: float = 0.0  # target % above breakeven for reverse
    reverse_profit_pct: float = 0.0     # extra profit % on top of breakeven
    reverse_fee_buffer_pct: float = 0.0 # fee buffer %
    reverse_chain_max: int = 5          # max reverse steps
    commission_pct: float = 0.05        # taker fee %
    max_position_notional_usd: float = 0.0  # 0 = no cap
    mode: str = "live"

    # Binance filters (typical for ATOMUSDT)
    min_notional: float = 5.0
    step_size: float = 0.1
    tick_size: float = 0.0001


# ──────────────────────────────────────────────────────────────
# Fee helpers (from order_manager._reverse_fee_params)
# ──────────────────────────────────────────────────────────────
def reverse_fee_params(cfg: SimConfig) -> Tuple[float, float, float]:
    """Returns (f_in, f_out, p) as decimals (not %)."""
    f_in = cfg.commission_pct / 100.0
    f_out = cfg.commission_pct / 100.0  # conservative: taker both sides
    p = cfg.reverse_profit_pct / 100.0
    return f_in, f_out, p


# ──────────────────────────────────────────────────────────────
# Reverse sizing (from order_manager._reverse_sizing)
# ──────────────────────────────────────────────────────────────
def reverse_sizing(
    P: float,           # trigger price (SL price)
    N: float,           # signed net position (negative=SHORT, positive=LONG)
    net_realized: float, # realized PnL of cycle so far (negative=loss)
    U_eff: float,       # effective unrealized PnL at trigger
    new_dir: str,       # "LONG" or "SHORT"
    cfg: SimConfig,
    w_pct: Optional[float] = None,  # override reverse_breakeven_pct
) -> Optional[Tuple[float, float, float, float, float, float, float]]:
    """
    Returns (T, S_total, q, f_in, f_out, p, w) or None if sizing impossible.
    T = target price for new leg
    S_total = total signed position after reverse
    q = qty of new leg (signed)
    """
    base_w = float(w_pct) if w_pct is not None else float(cfg.reverse_breakeven_pct or 0.0)
    w = (base_w + float(cfg.reverse_fee_buffer_pct or 0.0)) / 100.0
    
    T = P * (1 + w) if new_dir == "LONG" else P * (1 - w)
    f_in, f_out, p = reverse_fee_params(cfg)
    
    denom = abs(T - P) - f_in * P - (f_out + p) * T
    if P <= 0 or denom <= 0:
        return None
    
    s_abs = (-(net_realized + U_eff) + f_in * abs(N) * P) / denom
    if s_abs <= 0:
        return None
    
    S_total = s_abs if new_dir == "LONG" else -s_abs
    q = S_total - N
    return T, S_total, q, f_in, f_out, p, w


# ──────────────────────────────────────────────────────────────
# Entry sizing (from order_manager._calculate_order_quantity logic)
# ──────────────────────────────────────────────────────────────
def calc_entry_qty(
    cfg: SimConfig,
    entry_price: float,
    free_balance: float,      # availableBalance (live) or paper_balance (testnet)
    total_balance: float,     # total wallet balance
    is_recovery: bool = False,
    recovery_target: float = 0.0,
    sl_price: Optional[float] = None,
) -> Tuple[Optional[float], str, dict]:
    """
    Returns (qty, path, debug_info)
    path: which sizing method was used
    """
    debug = {}
    
    # Effective SL distance
    if sl_price is not None and sl_price > 0:
        sl_distance = abs(entry_price - sl_price)
        sl_distance_pct = sl_distance / entry_price * 100
    else:
        sl_distance = entry_price * cfg.sl_pct / 100
        sl_distance_pct = cfg.sl_pct
    
    debug["sl_distance"] = sl_distance
    debug["sl_distance_pct"] = sl_distance_pct
    
    # 1. Recovery sizing (highest priority)
    if is_recovery and recovery_target > 0:
        raw_qty = recovery_target / (entry_price * cfg.tp1_pct / 100)
        margin = raw_qty * entry_price / cfg.leverage
        debug["recovery_margin"] = margin
        if margin > total_balance:
            return None, "recovery_insufficient_margin", debug
        return raw_qty, "recovery", debug
    
    # 2. Explicit position_size_usd (margin in USD)
    if cfg.position_size_usd > 0:
        margin = cfg.position_size_usd
        raw_qty = (margin * cfg.leverage) / entry_price
        debug["margin"] = margin
        debug["notional"] = margin * cfg.leverage
        return raw_qty, "position_size_usd", debug
    
    # 3. position_size_pct (% of free balance)
    pct = float(cfg.position_size_pct or 0.0)
    if pct > 0 and free_balance > 0:
        margin = free_balance * pct / 100
        # minNotional floor
        min_margin = (cfg.min_notional / cfg.leverage) if cfg.min_notional > 0 else 0.0
        if min_margin > 0 and margin < min_margin:
            if min_margin <= free_balance:
                margin = math.ceil(min_margin * 100) / 100
                debug["margin_bumped_to_min"] = True
            else:
                debug["fallback_to_legacy"] = True
                margin = 0.0
        if margin > 0:
            margin = round(margin, 2)
            raw_qty = (margin * cfg.leverage) / entry_price
            debug["margin"] = margin
            debug["free_balance"] = free_balance
            debug["notional"] = margin * cfg.leverage
            return raw_qty, "position_size_pct", debug
    
    # 4. LIVE_DEFAULT_MARGIN_USD (env) - skip, not in config
    
    # 5. margin_pct (% of total balance)
    if cfg.margin_pct > 0:
        margin = round(total_balance * cfg.margin_pct / 100, 1)
        raw_qty = (margin * cfg.leverage) / entry_price
        debug["margin"] = margin
        return raw_qty, "margin_pct", debug
    
    # 6. fixed_notional_usd (margin)
    if cfg.fixed_notional_usd > 0:
        raw_qty = (cfg.fixed_notional_usd * cfg.leverage) / entry_price
        debug["margin"] = cfg.fixed_notional_usd
        return raw_qty, "fixed_notional_usd", debug
    
    # 7. fixed_qty
    if cfg.fixed_qty > 0:
        return cfg.fixed_qty, "fixed_qty", debug
    
    # 8. fixed_risk_usd
    if cfg.fixed_risk_usd > 0:
        if sl_distance > 0:
            raw_qty = cfg.fixed_risk_usd / sl_distance
        else:
            raw_qty = cfg.fixed_risk_usd / (entry_price * cfg.sl_pct / 100)
        debug["risk_usd"] = cfg.fixed_risk_usd
        return raw_qty, "fixed_risk_usd", debug
    
    # 9. risk_pct of balance over SL distance
    if sl_distance > 0 and total_balance > 0:
        raw_qty = (total_balance * cfg.risk_pct / 100) / sl_distance
        debug["risk_pct"] = cfg.risk_pct
        return raw_qty, "risk_pct", debug
    
    # 10. Fallback: calc_quantity with sl_pct
    raw_qty = (total_balance * cfg.risk_pct / 100) / (entry_price * cfg.sl_pct / 100)
    return raw_qty, "fallback_calc_quantity", debug


def adjust_qty(raw_qty: float, step_size: float) -> float:
    """Round down to step_size (Binance LOT_SIZE filter)."""
    if step_size <= 0:
        return raw_qty
    return math.floor(raw_qty / step_size) * step_size


def apply_notional_cap(qty: float, entry_price: float, cap_usd: float) -> float:
    """Clamp qty by max notional."""
    if cap_usd <= 0:
        return qty
    max_qty = cap_usd / entry_price
    return min(qty, max_qty)


# ──────────────────────────────────────────────────────────────
# CumLossCap (from cum_loss_cap.py)
# ──────────────────────────────────────────────────────────────
@dataclass
class CycleState:
    net_realized: float = 0.0
    step: int = 0
    entry_price: float = 0.0
    position_qty: float = 0.0
    direction: str = "LONG"
    unrealized: float = 0.0


class CumLossCap:
    def __init__(self, cap_pct: float = 5.0, realized_stale_max_sec: int = 900):
        self.cap_pct = cap_pct
        self.realized_stale_max_sec = realized_stale_max_sec
        self._last_net_by_step: dict[int, Tuple[float, float]] = {}  # step -> (net, timestamp)
    
    def evaluate(self, equity: float, cycle: CycleState, now_ts: float) -> Tuple[bool, str]:
        """
        Returns (should_stop, reason)
        """
        cap_usd = equity * self.cap_pct / 100
        net = cycle.net_realized + cycle.unrealized
        
        if net <= -cap_usd:
            return True, f"cum_loss_cap hit: net={net:.4f} <= -cap={-cap_usd:.4f} (cap_pct={self.cap_pct}%, equity={equity:.2f})"
        
        # Stale realized fallback
        if cycle.step > 0 and abs(cycle.net_realized) > 1e-9:
            self._last_net_by_step[cycle.step] = (cycle.net_realized, now_ts)
        
        return False, "ok"


# ──────────────────────────────────────────────────────────────
# Simulation scenarios
# ──────────────────────────────────────────────────────────────

def print_header(title: str):
    print(f"\n{'='*60}")
    print(f" {title}")
    print(f"{'='*60}")


def print_cfg(cfg: SimConfig, label: str):
    print(f"\n--- {label} ---")
    print(f"  leverage: {cfg.leverage}x")
    print(f"  position_size_pct: {cfg.position_size_pct}%")
    print(f"  fixed_risk_usd: ${cfg.fixed_risk_usd}")
    print(f"  risk_pct: {cfg.risk_pct}%")
    print(f"  sl_pct: {cfg.sl_pct}%")
    print(f"  reverse_breakeven_pct: {cfg.reverse_breakeven_pct}%")
    print(f"  reverse_profit_pct: {cfg.reverse_profit_pct}%")
    print(f"  reverse_fee_buffer_pct: {cfg.reverse_fee_buffer_pct}%")
    print(f"  max_position_notional_usd: ${cfg.max_position_notional_usd}")
    print(f"  min_notional: ${cfg.min_notional}")
    print(f"  step_size: {cfg.step_size}")


def simulate_entry(cfg: SimConfig, entry_price: float, deposit: float = 10.0):
    """Simulate first entry with given config."""
    print_header(f"ENTRY SIMULATION @ ${entry_price:.4f} | Deposit: ${deposit:.2f}")
    print_cfg(cfg, "Config")
    
    free_balance = deposit  # no open positions
    total_balance = deposit
    
    raw_qty, path, debug = calc_entry_qty(cfg, entry_price, free_balance, total_balance)
    
    if raw_qty is None:
        print(f"[FAIL] FAILED: {path}")
        return None
    
    qty = adjust_qty(raw_qty, cfg.step_size)
    qty = apply_notional_cap(qty, entry_price, cfg.max_position_notional_usd)
    
    margin = qty * entry_price / cfg.leverage
    notional = qty * entry_price
    risk_usd = qty * debug.get("sl_distance", entry_price * cfg.sl_pct / 100)
    
    print(f"\n[CHART] Sizing path: {path}")
    print(f"   raw_qty: {raw_qty:.6f} -> adjusted: {qty:.6f}")
    print(f"   margin: ${margin:.4f} ({margin/deposit*100:.2f}% of deposit)")
    print(f"   notional: ${notional:.2f} ({notional/deposit*100:.1f}% of deposit)")
    print(f"   risk at SL: ${risk_usd:.4f} ({risk_usd/deposit*100:.2f}% of deposit)")
    print(f"   debug: {debug}")
    
    if qty <= 0:
        print("[FAIL] Qty rounded to 0!")
        return None
    
    if notional < cfg.min_notional:
        print(f"[WARN]  WARNING: notional ${notional:.2f} < minNotional ${cfg.min_notional}")
    
    return {
        "qty": qty,
        "margin": margin,
        "notional": notional,
        "risk_usd": risk_usd,
        "path": path,
        "entry_price": entry_price,
    }


def simulate_reverse_chain(
    cfg: SimConfig,
    entry_price: float,
    initial_qty: float,
    initial_direction: str,
    deposit: float = 10.0,
    sl_pct: float = 0.8,
    max_steps: int = 5,
):
    """Simulate reverse chain: SL hit -> reverse -> SL hit -> reverse..."""
    print_header(f"REVERSE CHAIN SIMULATION | Deposit: ${deposit:.2f}")
    print_cfg(cfg, "Config")
    
    if cfg.reverse_breakeven_pct <= 0:
        print("[FAIL] reverse_breakeven_pct = 0 -> reverse sizing will FAIL (denom <= 0)")
        return
    
    cycle = CycleState(
        net_realized=0.0,
        step=0,
        entry_price=entry_price,
        position_qty=initial_qty,
        direction=initial_direction,
        unrealized=0.0,
    )
    
    # Simulate price moving against us to SL
    if initial_direction == "LONG":
        sl_price = entry_price * (1 - sl_pct / 100)
        reverse_dir = "SHORT"
    else:
        sl_price = entry_price * (1 + sl_pct / 100)
        reverse_dir = "LONG"
    
    print(f"\n[TARGET] Initial: {initial_direction} {initial_qty:.6f} @ ${entry_price:.4f}")
    print(f"   SL price: ${sl_price:.4f} ({sl_pct}%)")
    print(f"   Reverse direction: {reverse_dir}")
    
    equity = deposit
    cumcap = CumLossCap(cap_pct=5.0)  # REVERSE_CUM_LOSS_PCT=5
    
    for step in range(1, max_steps + 1):
        print(f"\n--- Step {step} ---")
        
        # At SL trigger: unrealized = position_qty * (sl_price - entry_price) for LONG
        if cycle.direction == "LONG":
            U_eff = cycle.position_qty * (sl_price - cycle.entry_price)
        else:
            U_eff = cycle.position_qty * (cycle.entry_price - sl_price)
        
        print(f"  Cycle: net_realized={cycle.net_realized:.4f}, U_eff={U_eff:.4f}, N={cycle.position_qty:.6f}")
        
        # Check cum loss cap BEFORE opening reverse
        should_stop, reason = cumcap.evaluate(equity, cycle, step * 1000)
        if should_stop:
            print(f"  [STOP] {reason}")
            break
        
        # Try reverse sizing
        result = reverse_sizing(
            P=sl_price,
            N=cycle.position_qty,
            net_realized=cycle.net_realized,
            U_eff=U_eff,
            new_dir=reverse_dir,
            cfg=cfg,
        )
        
        if result is None:
            print(f"  [FAIL] Reverse sizing FAILED (denom <= 0 or s_abs <= 0)")
            print(f"     -> Position would be FORCE CLOSED")
            break
        
        T, S_total, q, f_in, f_out, p, w = result
        q_adj = adjust_qty(abs(q), cfg.step_size)
        q_adj = q_adj if q > 0 else -q_adj
        q_adj = apply_notional_cap(abs(q_adj), sl_price, cfg.max_position_notional_usd)
        q_adj = q_adj if q > 0 else -q_adj
        
        new_margin = abs(q_adj) * sl_price / cfg.leverage
        new_notional = abs(q_adj) * sl_price
        
        print(f"  [OK] Reverse sizing OK:")
        print(f"     T (target): ${T:.4f}")
        print(f"     S_total: {S_total:.6f} (was {cycle.position_qty:.6f})")
        print(f"     q (new leg): {q:.6f} -> adjusted: {q_adj:.6f}")
        print(f"     w (breakeven+fee): {w*100:.3f}%")
        print(f"     p (profit target): {p*100:.3f}%")
        print(f"     new margin: ${new_margin:.4f}, notional: ${new_notional:.2f}")
        
        # Update cycle state
        # Realized PnL from closing the old leg at SL:
        # For LONG: realized = qty * (sl_price - entry_price) - fees
        # Fees on closing old leg:
        fee_close = abs(cycle.position_qty) * sl_price * f_in
        fee_open = abs(q_adj) * sl_price * f_in
        
        realized_this_step = U_eff - fee_close - fee_open
        cycle.net_realized += realized_this_step
        cycle.step = step
        cycle.entry_price = sl_price
        cycle.position_qty = S_total
        cycle.direction = reverse_dir
        cycle.unrealized = 0.0  # reset at new entry
        
        print(f"     realized this step: ${realized_this_step:.4f} (incl fees ${fee_close+fee_open:.4f})")
        print(f"     cumulative net_realized: ${cycle.net_realized:.4f}")
        
        # Check cap AFTER step
        should_stop, reason = cumcap.evaluate(equity, cycle, step * 1000)
        if should_stop:
            print(f"  [STOP] {reason}")
            break
        
        # Next SL price (assume same % move against new direction)
        if reverse_dir == "LONG":
            sl_price = sl_price * (1 - sl_pct / 100)
            reverse_dir = "SHORT"
        else:
            sl_price = sl_price * (1 + sl_pct / 100)
            reverse_dir = "LONG"
        
        # Check if we hit max chain
        if step >= cfg.reverse_chain_max:
            print(f"  [STOP] Max chain steps ({cfg.reverse_chain_max}) reached")
            break
    
    print(f"\n[FINAL] FINAL: net_realized=${cycle.net_realized:.4f}, step={cycle.step}, equity=${equity:.2f}")


def simulate_scenarios():
    """Run all scenarios with different configs."""
    deposit = 10.0
    entry_price = 4.50  # ATOM ~$4.50
    
    # ── Config A: Current LIVE (problematic) ─────────────────────
    cfg_live = SimConfig(
        leverage=75,
        position_size_pct=0.6,
        fixed_risk_usd=0.50,
        risk_pct=3.0,
        sl_pct=0.8,
        reverse_breakeven_pct=0.0,   # PROBLEM: 0
        reverse_profit_pct=0.0,
        reverse_fee_buffer_pct=0.0,
        reverse_chain_max=5,
        commission_pct=0.05,
        min_notional=5.0,
        step_size=0.1,
    )
    
    # ── Config B: Testnet-like (working reverse) ─────────────────
    cfg_testnet = SimConfig(
        leverage=20,
        position_size_pct=1.0,
        fixed_risk_usd=0.175,
        risk_pct=3.0,
        sl_pct=0.8,
        reverse_breakeven_pct=1.5,
        reverse_profit_pct=0.5,
        reverse_fee_buffer_pct=0.0,
        reverse_chain_max=5,
        commission_pct=0.05,
        min_notional=5.0,
        step_size=0.1,
    )
    
    # ── Config C: Optimized for $10 live ─────────────────────────
    cfg_optimized = SimConfig(
        leverage=20,
        position_size_pct=2.0,       # 2% of $10 = $0.20 margin -> $4 notional (below minNotional, will bump)
        fixed_risk_usd=0.10,         # $0.10 risk = 1% deposit
        risk_pct=1.0,                # 1% risk
        sl_pct=0.8,
        reverse_breakeven_pct=1.5,
        reverse_profit_pct=0.5,
        reverse_fee_buffer_pct=0.1,  # small fee buffer
        reverse_chain_max=3,         # limit chain depth
        commission_pct=0.05,
        max_position_notional_usd=50, # cap at 5x deposit
        min_notional=5.0,
        step_size=0.1,
    )
    
    # ── Config D: Fixed risk only (no position_size_pct) ─────────
    cfg_fixed_risk = SimConfig(
        leverage=20,
        position_size_pct=0.0,       # disabled
        fixed_risk_usd=0.10,
        risk_pct=1.0,
        sl_pct=0.8,
        reverse_breakeven_pct=1.5,
        reverse_profit_pct=0.5,
        reverse_fee_buffer_pct=0.1,
        reverse_chain_max=3,
        commission_pct=0.05,
        max_position_notional_usd=50,
        min_notional=5.0,
        step_size=0.1,
    )
    
    configs = [
        ("LIVE (current)", cfg_live),
        ("TESTNET-like", cfg_testnet),
        ("OPTIMIZED for $10", cfg_optimized),
        ("FIXED_RISK only", cfg_fixed_risk),
    ]
    
    for name, cfg in configs:
        # Test entry
        entry = simulate_entry(cfg, entry_price, deposit)
        if entry:
            # Test reverse chain
            simulate_reverse_chain(cfg, entry_price, entry["qty"], "LONG", deposit)
        print("\n" + "-"*60)
    
    # Additional test: what happens with different SL prices
    print_header("SENSITIVITY: Different entry prices (OPTIMIZED config)")
    for price in [2.0, 4.5, 10.0, 50.0, 100.0]:
        simulate_entry(cfg_optimized, price, deposit)


def simulate_cum_loss_cap():
    """Test CumLossCap with various equity/loss scenarios."""
    print_header("CUM LOSS CAP SIMULATION")
    
    deposit = 10.0
    cap_pct = 5.0  # REVERSE_CUM_LOSS_PCT
    cap_usd = deposit * cap_pct / 100
    print(f"Deposit: ${deposit:.2f}, Cap: {cap_pct}% = ${cap_usd:.2f}")
    
    cumcap = CumLossCap(cap_pct=cap_pct)
    cycle = CycleState()
    
    scenarios = [
        ("Small loss", -0.10, 0.0),
        ("Medium loss", -0.30, -0.10),
        ("Near cap", -0.45, -0.20),
        ("At cap", -0.50, -0.25),
        ("Over cap", -0.60, -0.30),
        ("With unrealized", -0.30, -0.25),
    ]
    
    for name, net_realized, unrealized in scenarios:
        cycle.net_realized = net_realized
        cycle.unrealized = unrealized
        should_stop, reason = cumcap.evaluate(deposit, cycle, 0)
        status = "[STOP] STOP" if should_stop else "[OK] OK"
        print(f"  {name:20s} | net_realized={net_realized:6.2f} unrealized={unrealized:6.2f} | total={net_realized+unrealized:6.2f} | {status} -> {reason}")


def simulate_reverse_denom_analysis():
    """Analyze why reverse sizing fails with certain params."""
    print_header("REVERSE DENOMINATOR ANALYSIS")
    
    cfg = SimConfig(
        leverage=20,
        sl_pct=0.8,
        commission_pct=0.05,
    )
    
    P = 4.50  # trigger price
    N = -10.0  # SHORT position
    net_realized = -0.20
    U_eff = -0.10  # unrealized loss at trigger
    new_dir = "LONG"
    
    print(f"P={P}, N={N}, net_realized={net_realized}, U_eff={U_eff}, new_dir={new_dir}")
    print(f"fee={cfg.commission_pct}%")
    
    for w_pct in [0.0, 0.5, 1.0, 1.5, 2.0, 3.0]:
        for p_pct in [0.0, 0.3, 0.5, 1.0]:
            for fee_buf in [0.0, 0.1]:
                cfg.reverse_breakeven_pct = w_pct
                cfg.reverse_profit_pct = p_pct
                cfg.reverse_fee_buffer_pct = fee_buf
                
                result = reverse_sizing(P, N, net_realized, U_eff, new_dir, cfg)
                if result:
                    T, S_total, q, f_in, f_out, p, w = result
                    denom = abs(T - P) - f_in * P - (f_out + p) * T
                    print(f"  w={w_pct:.1f}% p={p_pct:.1f}% fee_buf={fee_buf:.1f}% -> denom={denom:.6f} T={T:.4f} S={S_total:.2f} q={q:.2f} [OK]")
                else:
                    # Calculate denom manually for display
                    base_w = w_pct
                    w = (base_w + fee_buf) / 100
                    T = P * (1 + w)
                    f_in = f_out = cfg.commission_pct / 100
                    p = p_pct / 100
                    denom = abs(T - P) - f_in * P - (f_out + p) * T
                    print(f"  w={w_pct:.1f}% p={p_pct:.1f}% fee_buf={fee_buf:.1f}% -> denom={denom:.6f} [FAIL]")


if __name__ == "__main__":
    print("[SIM] LIVE BOT SIMULATION WITH $10 DEPOSIT")
    print("="*60)
    
    simulate_scenarios()
    simulate_cum_loss_cap()
    simulate_reverse_denom_analysis()
    
    print_header("SUMMARY")
    print("""
KEY FINDINGS for $10 deposit:

1. CURRENT LIVE CONFIG (leverage=75, position_size_pct=0.6%, fixed_risk=0.50):
   - position_size_pct -> margin=$0.06 -> notional=$4.50 < minNotional($5)
     -> code bumps to margin=$0.07, notional=$5.25 (barely works)
   - fixed_risk_usd=$0.50 -> 5% deposit risk per trade (TOO HIGH)
   - reverse_breakeven_pct=0 -> REVERSE CHAINS DON'T WORK (denom <= 0)

2. TESTNET-LIKE (leverage=20, position_size_pct=1%, fixed_risk=0.175):
   - position_size_pct -> margin=$0.10 -> notional=$2.00 < minNotional
     -> bumps to margin=$0.25, notional=$5.00
   - fixed_risk=$0.175 -> 1.75% deposit risk (reasonable)
   - reverse_breakeven_pct=1.5% -> REVERSE WORKS

3. OPTIMIZED FOR $10 (recommended):
   - leverage=20 (safer, same as testnet)
   - position_size_pct=2% -> margin=$0.20 -> notional=$4.00 -> bumps to $5.00
   - fixed_risk_usd=0.10 -> 1% deposit risk
   - risk_pct=1% (fallback)
   - reverse_breakeven_pct=1.5%, reverse_profit_pct=0.5%, fee_buffer=0.1%
   - max_position_notional_usd=50 (5x deposit cap)
   - reverse_chain_max=3 (limit depth)

4. CUM LOSS CAP (5%):
   - At $10 deposit: cap = $0.50 total loss per cycle
   - With 3-step reverse chain, each step loses ~$0.15-0.20 -> hits cap at step 2-3

REQUIRED ENV FOR LIVE:
  REVERSE_STEP_DOUBLING=true
  REVERSE_CUM_LOSS_PCT=5
  BINANCE_GATE_ENABLED=1
""")