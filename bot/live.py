"""Daily paper run on live Webull data.

Orders go to the bot's own simulated broker (SimBroker), not to Webull. Fills use live
Webull bid/ask with the same fill model as the Monte Carlo. Webull's API exposes no
paper-trading endpoint that we have found, so this is how the paper test runs.

Run once per trading day at about 3:45 PM US/Eastern (Task Scheduler; see WINDOWS.md).
"""
from __future__ import annotations

import datetime as dt
import json

import numpy as np

from .alerts import Alerts
from .broker import SimBroker
from .config import Config
from .engine import Engine
from .journal import Journal
from .models import OptionContract, OptionQuote, Signal
from .reports import graduation_report
from .strategy import compute_signals
from .webull_data import WebullData


class LiveSnapshot:
    def __init__(self, cfg: Config, wb: WebullData, today: dt.date, now: dt.datetime,
                 bars: dict[str, list[tuple[dt.date, float]]], prices: dict[str, float]):
        self.cfg, self.wb = cfg, wb
        self.date, self.now = today, now
        self.bars, self.prices = bars, prices
        self.spy_close = prices.get("SPY")
        self._quotes: dict[str, OptionQuote] = {}

    def signals(self) -> list[Signal]:
        """Signals from closes through YESTERDAY, traded today (same timing as the simulator)."""
        uni = [s for s in self.cfg.strategy["universe"] if self.bars.get(s)]
        series = {s: [c for d, c in self.bars[s] if d < self.date] for s in uni}
        n = min(len(v) for v in series.values()) if series else 0
        need = int(self.cfg.strategy["signal"]["slow_sma"]) + 2
        if n < need:
            return []
        closes = np.array([series[s][-n:] for s in uni]).T
        call, put, strength = compute_signals(closes, self.cfg.strategy)
        out = []
        for i, s in enumerate(uni):
            if call[-1, i]:
                out.append(Signal(s, "call", float(strength[-1, i])))
            elif put[-1, i]:
                out.append(Signal(s, "put", float(strength[-1, i])))
        return out

    def chain(self, symbol: str, side: str) -> list[OptionQuote]:
        S = self.prices.get(symbol)
        if not S:
            return []
        c = self.cfg.strategy["contract"]
        right = "C" if side == "call" else "P"
        lo, hi = (S * 0.97, S * 1.15) if right == "C" else (S * 0.85, S * 1.03)
        contracts = self.wb.option_contracts(symbol, right, round(lo, 2), round(hi, 2),
                                             self.date + dt.timedelta(days=c["min_dte"]),
                                             self.date + dt.timedelta(days=c["max_dte"]))
        qs = self.wb.option_quotes(contracts, S, self.now)
        for q in qs:
            self._quotes[q.contract.symbol] = q
        return qs

    def quote(self, contract: OptionContract) -> OptionQuote | None:
        q = self._quotes.get(contract.symbol)
        if q:
            return q
        S = self.prices.get(contract.underlying)
        if not S:
            return None
        qs = self.wb.option_quotes([contract], S, self.now)
        if qs:
            self._quotes[contract.symbol] = qs[0]
            return qs[0]
        return None


def _held_underlyings(state_path) -> set[str]:
    if not state_path.exists():
        return set()
    d = json.loads(state_path.read_text())
    return {p["underlying"] for p in d["ledger"]["positions"]}


def run_day(cfg: Config, now: dt.datetime | None = None) -> str:
    if not cfg.challenge.get("approved_to_start"):
        return ("Not started: set approved_to_start: true in config/challenge.yaml once you have "
                "reviewed the Monte Carlo and want the paper challenge to begin.")
    now = now or dt.datetime.now()
    today = now.date()
    paper = cfg.path("state/paper")
    state_path = paper / "state.json"

    wb = WebullData(cfg.root, r=float(cfg.sim.get("risk_free_rate", 0.04)))
    universe = list(cfg.strategy["universe"])
    syms = sorted(set(universe) | {"SPY"} | _held_underlyings(state_path))
    bars = wb.daily_bars(syms, count=150)
    spy = bars.get("SPY") or []
    if not spy or spy[-1][0] != today:
        return f"Market closed or no SPY bar for {today}; nothing done."
    prices = wb.last_prices(syms)

    alerts = Alerts(cfg.path("logs/alerts.log"))
    broker = SimBroker(cfg.sim["fills"], cfg.risk["fees_per_contract"])
    eng = Engine(cfg, broker, Journal(paper), alerts, reports_dir=cfg.path("reports"))
    eng.load_state(state_path)
    if state_path.exists():
        broker._pos = {k: int(v) for k, v in json.loads(state_path.read_text()).get("broker_positions", {}).items()}

    if eng.journal.equity and eng.journal.equity[-1]["date"] == today.isoformat():
        return f"Already ran for {today}."
    eng.on_day(LiveSnapshot(cfg, wb, today, now, bars, prices))
    eng.save_state(state_path)
    d = json.loads(state_path.read_text())
    d["broker_positions"] = broker.positions()
    state_path.write_text(json.dumps(d, indent=2, default=str))

    if today.weekday() == 4:
        md, _ = graduation_report(eng.journal, cfg, today=today)
        cfg.path("reports/graduation.md").write_text(md, encoding="utf-8")
    row = eng.journal.equity[-1]
    return (f"{today}: bankroll ${float(row['bankroll']):,.2f}, open positions {row['open_positions']}, "
            f"risk/trade {row['risk_pct']}%, halt: {eng.halt_reason or 'no'}")
