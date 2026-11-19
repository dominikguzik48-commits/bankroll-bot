"""Challenge end reports and the weekly graduation report."""
from __future__ import annotations

import datetime as dt
import math
from pathlib import Path

from .models import MULTIPLIER


def _f(x, default=None):
    try:
        return float(x)
    except (TypeError, ValueError):
        return default


def max_drawdown(values: list[float]) -> tuple[float, float]:
    """(max drawdown as a fraction of the peak, in dollars)."""
    peak, dd, dd_usd = -math.inf, 0.0, 0.0
    for v in values:
        peak = max(peak, v)
        if peak > 0:
            if (peak - v) / peak > dd:
                dd, dd_usd = (peak - v) / peak, peak - v
    return dd, dd_usd


def trade_stats(trades: list[dict], haircut: float = 0.0) -> dict:
    """haircut = $ per option contract price, applied on entry AND exit of every leg."""
    pnls = [float(t["pnl"]) - haircut * MULTIPLIER * float(t.get("contracts_per_fill", t["qty"])) * 2
            for t in trades]
    n = len(pnls)
    if n == 0:
        return {"n": 0, "expectancy": 0.0, "se": 0.0, "win_rate": 0.0, "avg_win": 0.0,
                "avg_loss": 0.0, "total": 0.0}
    wins = [p for p in pnls if p > 0]
    losses = [p for p in pnls if p <= 0]
    mean = sum(pnls) / n
    sd = math.sqrt(sum((p - mean) ** 2 for p in pnls) / (n - 1)) if n > 1 else 0.0
    return {"n": n, "expectancy": mean, "se": sd / math.sqrt(n) if n else 0.0,
            "win_rate": len(wins) / n, "avg_win": sum(wins) / len(wins) if wins else 0.0,
            "avg_loss": sum(losses) / len(losses) if losses else 0.0, "total": sum(pnls)}


def _trade_table(trades: list[dict], limit: int | None = None) -> str:
    rows = ["| # | Underlying | Contract | Qty | Entry | Exit | Opened | Closed | Reason | P&L |",
            "|---|---|---|---|---|---|---|---|---|---|"]
    sel = trades if limit is None else trades[-limit:]
    for i, t in enumerate(sel, 1):
        rows.append(f"| {i} | {t['underlying']} | {' / '.join(t['legs'])} | {t['qty']} | "
                    f"{float(t['entry_price']):.2f} | {float(t['exit_price']):.2f} | {t['entry_date']} | "
                    f"{t['exit_date']} | {t['reason']} | {float(t['pnl']):+,.2f} |")
    return "\n".join(rows)


def write_challenge_report(engine, outcome: str, today: dt.date) -> Path:
    cfg = engine.cfg.challenge
    J = engine.journal
    name = "challenge_complete.md" if outcome == "target" else "challenge_failed.md"
    path = engine.reports_dir / name
    path.parent.mkdir(parents=True, exist_ok=True)
    s = trade_stats(J.trades)
    eq = [_f(r["bankroll"], 0.0) for r in J.equity] + [engine.ledger.cash]
    dd, dd_usd = max_drawdown(eq)
    start = float(cfg["starting_bankroll"])
    final = engine.ledger.cash
    title = "Challenge complete: target reached" if outcome == "target" else "Challenge failed: ruin floor hit"
    ms = "\n".join(f"- ${m['level']:,.0f} on {m['date']} (bankroll ${m['bankroll']:,.2f})"
                   for m in engine.challenge.milestones_hit) or "- none"
    started = engine.challenge.started
    days = (today - started).days if started else 0
    body = f"""# {title}

Trading is halted. All positions were closed. The bot will not trade again until you give
new instructions and its saved challenge state is reset by hand.

| | |
|---|---|
| Started | {started} |
| Finished | {today} ({days} calendar days, {len(J.equity)} trading days) |
| Starting bankroll | ${start:,.2f} |
| Final bankroll | ${final:,.2f} ({(final / start - 1):+.1%}) |
| Target / ruin floor | ${float(cfg['target']):,.0f} / ${float(cfg['ruin_floor']):,.0f} |
| Peak bankroll | ${engine.challenge.peak:,.2f} |
| Max drawdown | {dd:.1%} (${dd_usd:,.2f}) |
| Closed trades | {s['n']} |
| Win rate | {s['win_rate']:.1%} |
| Average win / loss | ${s['avg_win']:,.2f} / ${s['avg_loss']:,.2f} |
| Expectancy per trade | ${s['expectancy']:,.2f} |
| Fees paid | ${engine.ledger.fees_paid:,.2f} |
| Final risk per trade | {engine.risk.risk_pct():.1f}% |
| Reconciliation errors | {J.counters.get('reconciliation_errors', 0)} |
| Risk-rule violations | {J.counters.get('risk_violations', 0)} |
| Orders rejected by risk manager | {J.counters.get('orders_rejected', 0)} |

## Milestones
{ms}

## Trades
{_trade_table(J.trades)}
"""
    path.write_text(body, encoding="utf-8")
    return path


def graduation_report(journal, cfg, haircuts=(0.02, 0.05), today: dt.date | None = None,
                      header_note: str = "") -> tuple[str, dict]:
    G = {"min_trades": 60, "min_weeks": 8}
    trades = journal.trades
    eq = journal.equity
    dates = [dt.date.fromisoformat(r["date"]) for r in eq]
    weeks = ((dates[-1] - dates[0]).days / 7) if len(dates) > 1 else 0.0
    base = trade_stats(trades)
    hc = {h: trade_stats(trades, h) for h in haircuts}
    worst = hc[max(haircuts)]
    bank = [_f(r["bankroll"], 0.0) for r in eq]
    dd, dd_usd = max_drawdown(bank)
    dd_lim = float(cfg.risk.get("max_drawdown_pct", 35)) / 100
    recon = int(journal.counters.get("reconciliation_errors", 0))
    viol = int(journal.counters.get("risk_violations", 0))
    start = bank[0] if bank else float(cfg.challenge["starting_bankroll"])
    end_hc = (bank[-1] if bank else start) - (base["total"] - worst["total"])
    bot_ret = end_hc / start - 1 if start else 0.0
    spy = [_f(r.get("spy_close")) for r in eq if _f(r.get("spy_close"))]
    spy_ret = spy[-1] / spy[0] - 1 if len(spy) > 1 else None

    def pf(ok):
        return "PASS" if ok else "FAIL"

    gates = [
        ("At least 60 closed trades", f"{base['n']} closed trades", base["n"] >= G["min_trades"]),
        ("At least 8 weeks of paper trading", f"{weeks:.1f} weeks", weeks >= G["min_weeks"]),
        (f"Positive expectancy after commissions and a ${max(haircuts):.2f}/contract haircut",
         f"${worst['expectancy']:+,.2f} per trade (±${worst['se']:,.2f} std. error)",
         worst["n"] > 0 and worst["expectancy"] > 0),
        (f"Max drawdown within {dd_lim:.0%}", f"{dd:.1%} (${dd_usd:,.2f})", dd <= dd_lim and bool(bank)),
        ("Zero reconciliation errors", str(recon), recon == 0),
        ("Zero risk-rule violations", str(viol), viol == 0),
        ("Beat buy-and-hold SPY over the same period (after haircut)",
         f"bot {bot_ret:+.1%} vs SPY {spy_ret:+.1%}" if spy_ret is not None else "no SPY data",
         spy_ret is not None and bot_ret > spy_ret),
    ]
    all_pass = all(g[2] for g in gates)
    rows = "\n".join(f"| {name} | {val} | **{pf(ok)}** |" for name, val, ok in gates)
    hrows = "\n".join(
        f"| ${h:.2f} | ${hc[h]['expectancy']:+,.2f} | ${hc[h]['total']:+,.2f} |" for h in haircuts)
    period = f"{dates[0]} to {dates[-1]}" if dates else "no data yet"
    md = f"""# Graduation report

Generated {today or dt.date.today()}. Paper period: {period}.
{header_note}
This report only informs your decision. It changes nothing: the bot has no code path to a
live account and will never go live on its own.

## Gates

| Gate | Result | |
|---|---|---|
{rows}

**Overall: {"ALL GATES PASS" if all_pass else "NOT READY: at least one gate fails"}**

## Expectancy detail

Paper P&L already includes commissions. The haircut assumes real fills are worse than paper
by the stated amount per contract on every entry and every exit.

| Haircut | Expectancy / trade | Total P&L |
|---|---|---|
| $0.00 (paper as filled) | ${base['expectancy']:+,.2f} | ${base['total']:+,.2f} |
{hrows}

Win rate {base['win_rate']:.1%}, average win ${base['avg_win']:,.2f}, average loss ${base['avg_loss']:,.2f}.

## Recent trades
{_trade_table(trades, limit=20) if trades else "No closed trades yet."}
"""
    return md, {"gates": [(g[0], g[2]) for g in gates], "all_pass": all_pass}
