"""Walk-forward parameter tuning with guardrails (optuna).

The tuner NEVER changes the live config. It writes a proposal and a report:
  reports/tuning.md
  config/strategy_proposed.yaml   (only if every guardrail passes)
You review it and copy values into config/strategy.yaml yourself.

Method: split the history into rolling folds (train N years, then test the next M
months). Optuna searches the bounded parameter space on each TRAIN window only; the best
parameters are then scored on the following TEST window they never saw, next to the
current parameters on that same window. A proposal needs to beat the current
parameters out of sample in most folds AND in aggregate, by a margin, with enough trades.
The risk manager, challenge limits, ruin floor and kill switch are not tunable.

Data: real daily closes (state/history/*.csv from `python -m bot fetch-history`).
Option prices in the backtest are modeled (Black-Scholes at GARCH vol x premium), since
free historical option quotes are not available.
"""
from __future__ import annotations

import copy
import csv
import datetime as dt
import math
from pathlib import Path

import numpy as np
import yaml

from .alerts import NullAlerts
from .broker import SimBroker
from .config import Config
from .engine import Engine
from .journal import Journal

# Parameter space. Bounds keep the search inside sane values; risk settings are not here.
SPACE = {
    "signal.fast_sma": ("int", 10, 30),
    "signal.slow_sma": ("int", 40, 100),
    "signal.rsi_period": ("int", 7, 21),
    "signal.call_rsi_min": ("int", 45, 55),
    "signal.put_rsi_max": ("int", 45, 55),
    "contract.target_delta": ("float", 0.30, 0.50),
    "contract.target_dte": ("int", 10, 28),
    "exits.take_profit_pct": ("int", 30, 100),
    "exits.stop_loss_pct": ("int", 25, 60),
}

GUARDS = {"min_trades_per_fold": 8, "min_fold_win_share": 0.6, "min_aggregate_edge": 0.05}


# ------------------------------------------------------------------ data
def load_history(folder: Path, symbols: list[str]) -> tuple[list[dt.date], np.ndarray]:
    """Align CSVs (date,close) on common dates. Returns (dates, closes [T, N])."""
    series = {}
    for s in symbols:
        p = folder / f"{s}.csv"
        if not p.exists():
            raise FileNotFoundError(f"missing history for {s}: {p} (run `python -m bot fetch-history`)")
        with open(p) as f:
            series[s] = {dt.date.fromisoformat(r["date"]): float(r["close"]) for r in csv.DictReader(f)}
    common = sorted(set.intersection(*(set(v) for v in series.values())))
    return common, np.array([[series[s][d] for s in symbols] for d in common])


def save_history(folder: Path, bars: dict[str, list[tuple[dt.date, float]]]) -> None:
    folder.mkdir(parents=True, exist_ok=True)
    for sym, rows in bars.items():
        with open(folder / f"{sym}.csv", "w", newline="") as f:
            w = csv.writer(f)
            w.writerow(["date", "close"])
            for d, c in rows:
                w.writerow([d.isoformat(), c])


# ------------------------------------------------------------------ backtest
def _set(strategy: dict, key: str, val) -> None:
    a, b = key.split(".")
    strategy[a][b] = val


def params_of(strategy: dict) -> dict:
    return {k: strategy[k.split(".")[0]][k.split(".")[1]] for k in SPACE}


def backtest(cfg: Config, dates, closes, params: dict | None = None, warmup: int = 110) -> dict:
    """One pass of the real engine over real closes. Challenge target/ruin halts are
    disabled so every window is scored over its full length."""
    from sim.market import MarketBatch

    c = cfg.copy()
    c.risk["kill_switch_file"] = None
    c.challenge["target"] = 1e12
    c.challenge["ruin_floor"] = 0
    for k, v in (params or {}).items():
        _set(c.strategy, k, v)
    if c.strategy["signal"]["slow_sma"] <= c.strategy["signal"]["fast_sma"] + 5:
        return {"log_growth": -9.0, "trades": 0, "final": 0.0}
    b = MarketBatch.from_history(c, dates, closes, warmup=warmup)
    J = Journal(None)
    eng = Engine(c, SimBroker(c.sim["fills"], c.risk["fees_per_contract"]), J, NullAlerts(),
                 extra_blackouts=b.blackout_dates, verbose_events=False)
    snap = None
    for t in range(b.warmup, b.T):
        snap = b.snapshot(0, t)
        eng.on_day(snap)
    if snap is not None and eng.ledger.positions:
        eng.close_all(snap, "end_of_window")
    start = float(c.challenge["starting_bankroll"])
    final = max(eng.ledger.cash, 1.0)
    return {"log_growth": math.log(final / start), "trades": len(J.trades), "final": final}


# ------------------------------------------------------------------ walk-forward
def folds(n_days: int, train: int, test: int, warmup: int):
    out, start = [], 0
    while start + warmup + train + test <= n_days:
        tr = (start, start + warmup + train)
        te = (start + train, start + warmup + train + test)   # test reuses the warmup bars before it
        out.append((tr, te))
        start += test
    return out


def tune(cfg: Config, dates, closes, trials: int = 40, train_days: int = 504, test_days: int = 126,
         warmup: int = 110, seed: int = 7, log=print) -> dict:
    import optuna

    optuna.logging.set_verbosity(optuna.logging.WARNING)
    fs = folds(len(dates), train_days, test_days, warmup)
    if not fs:
        raise ValueError(f"not enough history: {len(dates)} days, need {warmup + train_days + test_days}")
    current = params_of(cfg.strategy)
    results = []
    for i, ((a, b), (c, d)) in enumerate(fs):
        tr_dates, tr_closes = dates[a:b], closes[a:b]

        def objective(trial):
            p = {}
            for k, (kind, lo, hi) in SPACE.items():
                p[k] = trial.suggest_int(k, lo, hi) if kind == "int" else round(trial.suggest_float(k, lo, hi), 2)
            r = backtest(cfg, tr_dates, tr_closes, p, warmup)
            return r["log_growth"] if r["trades"] >= GUARDS["min_trades_per_fold"] else -9.0

        study = optuna.create_study(direction="maximize", sampler=optuna.samplers.TPESampler(seed=seed + i))
        study.enqueue_trial(current)
        study.optimize(objective, n_trials=trials)
        best = study.best_params
        te_new = backtest(cfg, dates[c:d], closes[c:d], best, warmup)
        te_cur = backtest(cfg, dates[c:d], closes[c:d], current, warmup)
        results.append({"fold": i + 1, "train": f"{dates[a + warmup]} to {dates[b - 1]}",
                        "test": f"{dates[c + warmup]} to {dates[d - 1]}", "best": best,
                        "train_score": study.best_value, "test_new": te_new, "test_cur": te_cur})
        log(f"fold {i + 1}/{len(fs)}: test log-growth new {te_new['log_growth']:+.3f} "
            f"vs current {te_cur['log_growth']:+.3f}")
    return {"current": current, "folds": results}


def consensus(results: list[dict]) -> dict:
    """Median of each parameter across folds (more stable than any single fold's best)."""
    out = {}
    for k, (kind, _, _) in SPACE.items():
        v = float(np.median([r["best"][k] for r in results]))
        out[k] = int(round(v)) if kind == "int" else round(v, 2)
    return out


def verdict(cfg: Config, out: dict, dates, closes, warmup: int) -> dict:
    fs = out["folds"]
    wins = sum(r["test_new"]["log_growth"] > r["test_cur"]["log_growth"] for r in fs)
    agg_new = sum(r["test_new"]["log_growth"] for r in fs)
    agg_cur = sum(r["test_cur"]["log_growth"] for r in fs)
    enough = all(r["test_new"]["trades"] >= GUARDS["min_trades_per_fold"] // 2 for r in fs)
    gates = [
        (f"Beats current params out of sample in at least {GUARDS['min_fold_win_share']:.0%} of folds",
         f"{wins}/{len(fs)}", wins / len(fs) >= GUARDS["min_fold_win_share"]),
        (f"Aggregate out-of-sample log-growth better by at least {GUARDS['min_aggregate_edge']}",
         f"{agg_new:+.3f} vs {agg_cur:+.3f}", agg_new - agg_cur >= GUARDS["min_aggregate_edge"]),
        ("Enough trades in every test window", "yes" if enough else "no", enough),
        ("Tuned params make money out of sample", f"{agg_new:+.3f}", agg_new > 0),
    ]
    return {"gates": gates, "approve": all(g[2] for g in gates), "proposal": consensus(fs)}


def write_tuning_report(cfg: Config, out: dict, v: dict, path: Path, data_note: str) -> None:
    rows = "\n".join(
        f"| {r['fold']} | {r['test']} | {r['test_new']['log_growth']:+.3f} ({r['test_new']['trades']} tr) | "
        f"{r['test_cur']['log_growth']:+.3f} ({r['test_cur']['trades']} tr) |" for r in out["folds"])
    gates = "\n".join(f"| {n} | {val} | **{'PASS' if ok else 'FAIL'}** |" for n, val, ok in v["gates"])
    params = "\n".join(f"| `{k}` | {out['current'][k]} | {v['proposal'][k]} |" for k in SPACE)
    md = f"""# Walk-forward tuning

{data_note}

Nothing was changed. {"All guardrails pass: a proposal was written to `config/strategy_proposed.yaml`. Review it, then copy the values you accept into `config/strategy.yaml`." if v["approve"] else "At least one guardrail failed, so no proposal was written. Keep the current parameters."}

## Guardrails

| Check | Result | |
|---|---|---|
{gates}

## Out-of-sample results by fold

Log growth of the bankroll over each test window (0 = flat, +0.10 ~ +10.5%, -0.10 ~ -9.5%).

| Fold | Test window | Tuned params | Current params |
|---|---|---|---|
{rows}

## Parameters (median across folds)

| Parameter | Current | Proposed |
|---|---|---|
{params}
"""
    path.write_text(md, encoding="utf-8")
    if v["approve"]:
        proposed = copy.deepcopy(cfg.strategy)
        proposed.pop("vol_params", None)
        for k, val in v["proposal"].items():
            _set(proposed, k, val)
        cfg.path("config/strategy_proposed.yaml").write_text(yaml.safe_dump(proposed, sort_keys=False), encoding="utf-8")
