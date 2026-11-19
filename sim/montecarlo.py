"""Monte Carlo of the Bankroll Challenge using the real engine, risk manager, ledger,
strategy and challenge controller on synthetic markets."""
from __future__ import annotations

import json
import math
import multiprocessing as mp
import os
import time
from pathlib import Path

import numpy as np

from bot.alerts import NullAlerts
from bot.broker import SimBroker
from bot.config import Config, load_config
from bot.engine import Engine
from bot.journal import Journal
from bot.reports import max_drawdown
from sim.market import MarketBatch


def run_batch(args) -> list[dict]:
    cfg, model, risk_pct, n_runs, seed = args
    cfg = cfg.copy()
    cfg.challenge["risk_per_trade_pct"] = risk_pct
    cfg.challenge["min_risk_per_trade_pct"] = min(float(cfg.challenge["min_risk_per_trade_pct"]), risk_pct)
    cfg.risk["kill_switch_file"] = None
    b = MarketBatch(cfg, model, n_runs, seed)
    out = []
    for r in range(n_runs):
        J = Journal(None)
        eng = Engine(cfg, SimBroker(cfg.sim["fills"], cfg.risk["fees_per_contract"]), J,
                     NullAlerts(), reports_dir=None, extra_blackouts=b.blackout_dates,
                     verbose_events=False)
        days = 0
        for t in range(b.warmup, b.warmup + b.horizon):
            snap = b.snapshot(r, t)
            eng.on_day(snap)
            days += 1
            if eng.halt_reason:
                break
        status = eng.challenge.status
        outcome = {"complete": "target", "failed": "ruin"}.get(status, "unresolved")
        if eng.halt_reason and outcome == "unresolved":
            outcome = "halted_other"
        bank = [row["bankroll"] for row in J.equity]
        dd, _ = max_drawdown(bank)
        pnls = [t_["pnl"] for t_ in J.trades]
        out.append({
            "outcome": outcome, "trades": len(J.trades), "days": days,
            "final": eng.ledger.cash + sum(eng._last_marks.get(p, (0, 0))[1] for p in eng.ledger.positions),
            "peak": eng.challenge.peak, "max_dd": dd,
            "reached_2000": max(bank) >= 2000 if bank else False,
            "wins": sum(1 for p in pnls if p > 0), "pnl_sum": float(sum(pnls)),
            "fees": eng.ledger.fees_paid,
            "rejected": J.counters["orders_rejected"],
            "violations": J.counters["risk_violations"],
            "recon": J.counters["reconciliation_errors"],
            "halt": eng.halt_reason,
        })
    return out


def run(cfg: Config, runs: int, risks: list[float], models: list[str], seed: int = 7,
        batch: int = 50, workers: int | None = None) -> dict:
    jobs, keys = [], []
    for mi, model in enumerate(models):
        for rp in risks:
            n_b = math.ceil(runs / batch)
            for i in range(n_b):
                n = min(batch, runs - i * batch)
                # same seeds across risk levels -> same markets, so risk is the only difference
                jobs.append((cfg, model, rp, n, seed * 100003 + mi * 7919 + i))
                keys.append((model, rp))
    t0 = time.time()
    with mp.get_context("spawn" if os.name == "nt" else "fork").Pool(workers or mp.cpu_count()) as pool:
        results = pool.map(run_batch, jobs, chunksize=1)
    agg: dict = {}
    for k, res in zip(keys, results):
        agg.setdefault(k, []).extend(res)
    return {"elapsed_s": time.time() - t0, "results": agg}


def _med(xs):
    return float(np.median(xs)) if xs else float("nan")


def summarize(rows: list[dict], horizon_days: int) -> dict:
    n = len(rows)
    by = lambda o: [r for r in rows if r["outcome"] == o]
    tg, ru, un = by("target"), by("ruin"), by("unresolved")
    fin = tg + ru
    trades = sum(r["trades"] for r in rows)
    years = sum(r["days"] for r in rows) / 252
    return {
        "runs": n,
        "p_target": len(tg) / n, "p_ruin": len(ru) / n, "p_unresolved": len(un) / n,
        "p_other_halt": len(by("halted_other")) / n,
        "p_reach_2000": sum(r["reached_2000"] for r in rows) / n,
        "median_trades_finish": _med([r["trades"] for r in fin]),
        "median_days_finish": _med([r["days"] for r in fin]),
        "median_trades_target": _med([r["trades"] for r in tg]),
        "median_days_target": _med([r["days"] for r in tg]),
        "median_trades_ruin": _med([r["trades"] for r in ru]),
        "median_days_ruin": _med([r["days"] for r in ru]),
        "median_final_unresolved": _med([r["final"] for r in un]),
        "median_final_all": _med([r["final"] for r in rows]),
        "trades_per_year": trades / years if years else 0,
        "win_rate": sum(r["wins"] for r in rows) / trades if trades else 0,
        "avg_pnl_per_trade": sum(r["pnl_sum"] for r in rows) / trades if trades else 0,
        "zero_trade_runs": sum(1 for r in rows if r["trades"] == 0) / n,
        "violations": sum(r["violations"] for r in rows),
        "recon_errors": sum(r["recon"] for r in rows),
        "median_max_dd": _med([r["max_dd"] for r in rows]),
        "horizon_days": horizon_days,
    }


def write_report(summary: dict, cfg: Config, runs: int, path: Path, elapsed: float) -> None:
    ch = cfg.challenge
    H = cfg.sim["horizon_trading_days"]
    lines = [
        "# Bankroll Challenge: Monte Carlo",
        "",
        f"{runs:,} runs per row. Start ${ch['starting_bankroll']:,}, target ${ch['target']:,}, "
        f"ruin floor ${ch['ruin_floor']:,}, profile CASH_L2 (long calls/puts only), max 1 position, "
        f"milestones {ch['milestones']} stepping risk down to {ch['min_risk_per_trade_pct']}%. "
        f"Horizon {H} trading days ({H / 252:.0f} years); runs still going at the end are 'unresolved'.",
        "",
        "**Synthetic market.** No real price history was used. Fills at mid + 75% of the half-spread "
        f"+ $0.01, fees ${cfg.risk['fees_per_contract']}/contract each side, IV = realized vol x "
        f"{cfg.sim['variance_risk_premium']} (option buyers pay a volatility premium), "
        "daily bars (exits checked at the close, so gaps go through stops).",
        "",
        "- `regime`: bull/bear regimes that persist, so trends are real and the signal can have an edge.",
        "- `random_walk`: no trend persistence, so the signal has no edge by construction.",
        "",
        "| Market | Risk/trade | P(hit $5,000) | P(ruin) | Unresolved after 3y | Median trades to finish | Median days to finish | Median trades to target | Median days to target | Trades/yr | Win rate | Avg P&L/trade | Median final (all) |",
        "|---|---|---|---|---|---|---|---|---|---|---|---|---|",
    ]
    for (model, rp), s in summary.items():
        lines.append(
            f"| {model} | {rp:g}% | {s['p_target']:.1%} | {s['p_ruin']:.1%} | {s['p_unresolved']:.1%} | "
            f"{s['median_trades_finish']:.0f} | {s['median_days_finish']:.0f} | "
            f"{s['median_trades_target']:.0f} | {s['median_days_target']:.0f} | "
            f"{s['trades_per_year']:.0f} | {s['win_rate']:.1%} | ${s['avg_pnl_per_trade']:+,.2f} | "
            f"${s['median_final_all']:,.0f} |")
    lines += ["", "Checks: risk-rule violations "
              f"{sum(s['violations'] for s in summary.values())}, reconciliation errors "
              f"{sum(s['recon_errors'] for s in summary.values())}, other halts "
              f"{sum(s['p_other_halt'] for s in summary.values()):.3f}.",
              f"", f"Elapsed {elapsed:.0f}s."]
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    path.with_suffix(".json").write_text(json.dumps(
        {f"{m}|{rp}": s for (m, rp), s in summary.items()}, indent=2))
