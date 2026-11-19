"""CASH_L2 strategy v1: 20/50 SMA trend + RSI momentum, buy calls or puts.

The same indicator code runs on live bars (one column per symbol) and on the
simulator's batches (thousands of columns at once).
"""
from __future__ import annotations

import datetime as dt
import math

import numpy as np
import pandas as pd

from .models import LONG_CALL, LONG_PUT, MULTIPLIER, Leg, OptionQuote, OrderIntent, Position


def sma(x: np.ndarray, n: int) -> np.ndarray:
    """Simple moving average down axis 0; NaN until n values exist."""
    x = np.asarray(x, dtype=float)
    cs = np.cumsum(x, axis=0)
    out = np.full_like(x, np.nan)
    out[n - 1] = cs[n - 1] / n
    out[n:] = (cs[n:] - cs[:-n]) / n
    return out


def rsi(x: np.ndarray, n: int) -> np.ndarray:
    """Wilder RSI down axis 0."""
    x = np.asarray(x, dtype=float)
    d = np.diff(x, axis=0, prepend=x[:1])
    gain = pd.DataFrame(np.clip(d, 0, None).reshape(len(x), -1))
    loss = pd.DataFrame(np.clip(-d, 0, None).reshape(len(x), -1))
    ag = gain.ewm(alpha=1.0 / n, adjust=False).mean().to_numpy()
    al = loss.ewm(alpha=1.0 / n, adjust=False).mean().to_numpy()
    with np.errstate(divide="ignore", invalid="ignore"):
        rs = ag / al
        out = 100.0 - 100.0 / (1.0 + rs)
    out = np.where(al == 0, 100.0, out)
    out[:n] = np.nan
    return out.reshape(x.shape)


def compute_signals(closes: np.ndarray, cfg: dict):
    """closes: [T, ...]. Returns (call_entry, put_entry, strength), same shape, for each bar's close."""
    s = cfg["signal"]
    f = sma(closes, int(s["fast_sma"]))
    sl = sma(closes, int(s["slow_sma"]))
    r = rsi(closes, int(s["rsi_period"]))
    fp, slp, rp = np.roll(f, 1, 0), np.roll(sl, 1, 0), np.roll(r, 1, 0)
    with np.errstate(invalid="ignore"):
        up, down = f > sl, f < sl
        call_band = (r >= s["call_rsi_min"]) & (r <= s["call_rsi_max"])
        put_band = (r >= s["put_rsi_min"]) & (r <= s["put_rsi_max"])
        if s.get("entry_mode", "trend") == "cross":
            call = up & (fp <= slp) & call_band
            put = down & (fp >= slp) & put_band
        else:
            call = up & (rp < s["call_rsi_min"]) & call_band
            put = down & (rp > s["put_rsi_max"]) & put_band
        strength = np.abs(f / sl - 1.0)
    valid = ~(np.isnan(slp) | np.isnan(rp))
    valid[0] = False
    call &= valid
    put &= valid
    return call, put, np.nan_to_num(strength)


def select_contract(chain: list[OptionQuote], side: str, today: dt.date, cfg: dict,
                    risk, budget: float, spendable: float, now: dt.datetime, vol_forecast=None):
    """Pick the contract nearest target delta/DTE that fits inside the risk budget.
    Returns (OrderIntent, None) or (None, reason). No fit -> no trade."""
    c = cfg["contract"]
    right = "C" if side == "call" else "P"
    fee = risk.fees_per_contract
    best, best_key = None, None
    n_window = n_illiquid = n_too_big = n_rich = 0
    vf = cfg.get("vol_filter") or {}
    use_vf = bool(vf.get("enabled")) and vol_forecast is not None
    for q in chain:
        k = q.contract
        if k.right != right:
            continue
        dte = (k.expiry - today).days
        if not (c["min_dte"] <= dte <= c["max_dte"]):
            continue
        if not (c["min_delta"] <= abs(q.delta) <= c["max_delta"]):
            continue
        n_window += 1
        if risk.liquidity_problems(q) or risk.stale_problems(q, now):
            n_illiquid += 1
            continue
        if use_vf:
            fv = vol_forecast(dte)
            if fv is None or q.iv <= 0 or q.iv > float(vf["max_iv_to_forecast"]) * fv:
                n_rich += 1
                continue
        per_contract_risk = q.ask * MULTIPLIER + 2 * fee
        per_contract_cost = q.ask * MULTIPLIER + fee
        qty = int(min(budget / per_contract_risk, spendable / per_contract_cost) + 1e-9)
        if qty < 1:
            n_too_big += 1
            continue
        key = (abs(abs(q.delta) - c["target_delta"]), abs(dte - c["target_dte"]))
        if best_key is None or key < best_key:
            best, best_key = (q, qty), key
    if best is None:
        if n_window == 0:
            return None, "no contracts in DTE/delta window"
        return None, (f"{n_window} contracts in window: {n_illiquid} fail liquidity/staleness, "
                      f"{n_rich} too expensive vs vol forecast, "
                      f"{n_too_big} cost more than the ${budget:,.2f} risk budget")
    q, qty = best
    structure = LONG_CALL if right == "C" else LONG_PUT
    intent = OrderIntent(structure, q.contract.underlying, [Leg(q.contract, +1, 1)], qty,
                         limit_price=q.ask, quotes={q.contract.symbol: q},
                         reason=f"{side} signal, delta {q.delta:+.2f}, "
                                f"{(q.contract.expiry - today).days} DTE")
    return intent, None


def exit_reason(pos: Position, mark_per_unit: float, today: dt.date, cfg: dict) -> str | None:
    e = cfg["exits"]
    if pos.entry_price > 0:
        pnl_pct = (mark_per_unit - pos.entry_price) / pos.entry_price * 100
        if pnl_pct >= e["take_profit_pct"]:
            return "take_profit"
        if pnl_pct <= -e["stop_loss_pct"]:
            return "stop_loss"
    if (pos.expiry - today).days <= e["close_at_dte"]:
        return "dte_exit"
    return None
