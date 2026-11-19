import datetime as dt
import json
import shutil

import pytest

import bot.live as live
from bot.config import load_config
from bot.models import OptionContract, OptionQuote
from bot.webull_data import _pick, _rows, occ_symbol, parse_occ


def test_occ_round_trip():
    c = OptionContract("AAPL", dt.date(2026, 5, 22), 300.0, "C")
    assert occ_symbol(c) == "AAPL260522C00300000"
    assert parse_occ("AAPL260522C00300000") == c
    assert parse_occ("F270115P00012500").strike == 12.5


def test_tolerant_parsing():
    payload = {"data": [{"symbol": "SPY", "quote": {"bid_price": "1.01"}}]}
    rows = _rows(payload)
    assert rows[0]["symbol"] == "SPY"
    assert _pick(rows[0], "bid", "bid_price") == "1.01"


class FakeWB:
    """Stands in for WebullData: a gently rising market so a call signal appears."""
    def __init__(self, root, r=0.04):
        self.today = FakeWB.today

    def daily_bars(self, syms, count=150):
        out = {}
        days = [self.today - dt.timedelta(days=i) for i in range(200)][::-1]
        days = [d for d in days if d.weekday() < 5]
        for s in syms:
            px = []
            for i, d in enumerate(days):
                base = 100 + 0.3 * i
                # dip near the end then recover: RSI crosses back above 50 in an uptrend
                if i in (len(days) - 4, len(days) - 3):
                    base -= 6
                px.append((d, base))
            out[s] = px
        return out

    def last_prices(self, syms):
        return {s: 130.0 for s in syms}

    def option_contracts(self, und, right, lo, hi, e0, e1):
        exp = self.today + dt.timedelta(days=21)
        return [OptionContract(und, exp, k, right) for k in (130, 132, 134)]

    def option_quotes(self, contracts, S, now):
        return [OptionQuote(c, 0.80, 0.82, 0.40 if c.right == "C" else -0.40, 0.25, 5000, 800, now, S)
                for c in contracts]


@pytest.fixture
def tmp_cfg(tmp_path):
    cfg = load_config()
    shutil.copytree(cfg.root / "config", tmp_path / "config")
    c = load_config(tmp_path)
    c.risk["kill_switch_file"] = None
    c.strategy["universe"] = ["SPY", "IWM"]
    return c


def test_run_day_refuses_without_go_ahead(tmp_cfg):
    tmp_cfg.challenge["approved_to_start"] = False
    assert live.run_day(tmp_cfg).startswith("Not started")


def test_run_day_paper_session(tmp_cfg, monkeypatch):
    tmp_cfg.challenge["approved_to_start"] = True
    today = dt.date(2027, 3, 3)
    FakeWB.today = today
    monkeypatch.setattr(live, "WebullData", FakeWB)
    msg = live.run_day(tmp_cfg, now=dt.datetime.combine(today, dt.time(15, 45)))
    assert today.isoformat() in msg
    state = json.loads((tmp_cfg.root / "state/paper/state.json").read_text())
    pos = state["ledger"]["positions"]
    assert len(pos) == 1
    # sized off the $1,000 challenge bankroll (25% = $250), never the broker balance
    assert pos[0]["max_loss"] <= 250 + 1e-9
    assert state["broker_positions"]
    # running twice the same day does nothing
    assert "Already ran" in live.run_day(tmp_cfg, now=dt.datetime.combine(today, dt.time(15, 50)))
