import datetime as dt

import pytest

from bot.alerts import Alerts, NullAlerts
from bot.broker import SimBroker
from bot.challenge import Challenge
from bot.config import load_config
from bot.engine import Engine
from bot.journal import Journal
from bot.ledger import Ledger
from bot.models import (IRON_CONDOR, LONG_CALL, VERTICAL, Leg, OptionContract, OptionQuote,
                        OrderIntent, Position, Signal)
from bot.profile import CASH_L2, MARGIN_L3, ProfileManager
from bot.reports import graduation_report
from bot.risk import RiskContext, RiskManager
from bot.strategy import exit_reason, select_contract

TODAY = dt.date(2027, 3, 1)  # Monday
NOW = dt.datetime(2027, 3, 1, 15, 45)
EXP = dt.date(2027, 3, 19)


@pytest.fixture
def cfg():
    c = load_config()
    c.risk["kill_switch_file"] = None
    return c


def q(und="SPY", strike=500, right="C", bid=1.00, ask=1.02, delta=0.40, S=500.0, exp=EXP,
      ts=NOW, oi=5000, vol=500):
    return OptionQuote(OptionContract(und, exp, strike, right), bid, ask, delta, 0.2, oi, vol, ts, S)


def intent_for(quotes, sides, structure, qty=1, price=None):
    legs = [Leg(x.contract, s) for x, s in zip(quotes, sides)]
    if price is None:
        price = sum(s * (x.ask if s > 0 else x.bid) for x, s in zip(quotes, sides))
    return OrderIntent(structure, quotes[0].contract.underlying, legs, qty, price,
                       {x.contract.symbol: x for x in quotes})


def ctx(bankroll=1000, settled=1000, positions=0, profile=CASH_L2, cfg=None, **kw):
    allowed = cfg.account["profiles"][profile]["allowed_structures"]
    return RiskContext(TODAY, NOW, bankroll, settled, positions, profile, allowed,
                       cfg.account["profiles"][profile]["settled_cash_only"], **kw)


def rm(cfg, challenge=None):
    ch = challenge or Challenge(cfg.challenge)
    return RiskManager(cfg.risk, cfg.challenge, ch)


# --- risk manager ----------------------------------------------------------
def test_long_call_within_budget_approved(cfg):
    d = rm(cfg).check(intent_for([q(bid=2.00, ask=2.02)], [1], LONG_CALL), ctx(cfg=cfg))
    assert d.approved, d.reasons
    assert d.max_loss == pytest.approx(202 + 1.30)


def test_challenge_risk_limit_25pct(cfg):
    big = intent_for([q(bid=2.60, ask=2.62)], [1], LONG_CALL)   # $262 + fees > $250
    d = rm(cfg).check(big, ctx(cfg=cfg))
    assert not d.approved and any("max loss" in r for r in d.reasons)


def test_max_one_open_position(cfg):
    d = rm(cfg).check(intent_for([q()], [1], LONG_CALL), ctx(cfg=cfg, positions=1))
    assert not d.approved and any("max open positions" in r for r in d.reasons)


def test_cash_l2_rejects_spreads(cfg):
    v = intent_for([q(strike=500), q(strike=501, bid=0.5, ask=0.52)], [1, -1], VERTICAL)
    d = rm(cfg).check(v, ctx(cfg=cfg))
    assert not d.approved and any("not permitted on profile CASH_L2" in r for r in d.reasons)


def test_naked_short_always_rejected_even_on_margin(cfg):
    naked = intent_for([q()], [-1], LONG_CALL)
    d = rm(cfg).check(naked, ctx(cfg=cfg, profile=MARGIN_L3, bankroll=3000, settled=3000))
    assert not d.approved and any("naked short" in r for r in d.reasons)
    # a long leg in a different expiry is not cover (calendar = not defined risk here)
    other = q(strike=501, exp=EXP + dt.timedelta(days=7))
    bad = intent_for([other, q(strike=500)], [1, -1], VERTICAL)
    d = rm(cfg).check(bad, ctx(cfg=cfg, profile=MARGIN_L3, bankroll=3000, settled=3000))
    assert any("naked short" in r for r in d.reasons)


def test_margin_l3_allows_1_wide_vertical_on_spy(cfg):
    v = intent_for([q(strike=500, bid=1.00, ask=1.01), q(strike=501, bid=0.60, ask=0.61)], [1, -1], VERTICAL)
    d = rm(cfg).check(v, ctx(cfg=cfg, profile=MARGIN_L3, bankroll=3000, settled=3000))
    assert d.approved, d.reasons
    assert d.max_loss == pytest.approx((1.01 - 0.60) * 100 + 4 * 0.65)


def test_1_wide_vertical_rejected_on_expensive_non_index(cfg):
    v = intent_for([q("AAPL", 270, S=270, bid=1.0, ask=1.01), q("AAPL", 271, S=270, bid=0.6, ask=0.61)],
                   [1, -1], VERTICAL)
    d = rm(cfg).check(v, ctx(cfg=cfg, profile=MARGIN_L3, bankroll=3000, settled=3000))
    assert any("-wide spread only allowed" in r for r in d.reasons)


def test_iron_condor_max_loss(cfg):
    legs = [q(strike=490, right="P", bid=0.30, ask=0.31), q(strike=495, right="P", bid=0.60, ask=0.61),
            q(strike=505, right="C", bid=0.60, ask=0.61), q(strike=510, right="C", bid=0.30, ask=0.31)]
    ic = intent_for(legs, [1, -1, -1, 1], IRON_CONDOR)
    r = rm(cfg)
    credit = -ic.limit_price
    assert r.max_loss(ic) == pytest.approx((5 - credit) * 100 + 8 * 0.65)


def test_liquidity_stale_blackout_kill(cfg):
    r = rm(cfg)
    wide = intent_for([q(bid=1.00, ask=1.40)], [1], LONG_CALL)
    assert any("spread" in x for x in r.check(wide, ctx(cfg=cfg)).reasons)
    stale = intent_for([q(ts=NOW - dt.timedelta(minutes=5))], [1], LONG_CALL)
    assert any("old" in x for x in r.check(stale, ctx(cfg=cfg)).reasons)
    thin = intent_for([q(oi=10)], [1], LONG_CALL)
    assert any("open interest" in x for x in r.check(thin, ctx(cfg=cfg)).reasons)
    r2 = RiskManager(cfg.risk, cfg.challenge, None, extra_blackouts={TODAY})
    assert any("blackout" in x for x in r2.check(intent_for([q()], [1], LONG_CALL), ctx(cfg=cfg)).reasons)
    r.kill_switch_override = True
    assert any("kill switch" in x for x in r.check(intent_for([q()], [1], LONG_CALL), ctx(cfg=cfg)).reasons)
    assert not r.check(intent_for([q()], [1], LONG_CALL), ctx(cfg=cfg, reconciliation_ok=False))


def test_settled_cash_only_on_cash_l2(cfg):
    d = rm(cfg).check(intent_for([q(bid=2.0, ask=2.02)], [1], LONG_CALL),
                      ctx(cfg=cfg, bankroll=1000, settled=150))
    assert not d.approved and any("settled cash" in r for r in d.reasons)


# --- ledger / settlement -----------------------------------------------------
def test_sale_proceeds_settle_t_plus_1():
    L = Ledger(1000, 1)
    c = OptionContract("SPY", EXP, 500, "C")
    L.open(Position("P1", LONG_CALL, "SPY", [Leg(c, 1)], 1, 2.0, 0.65, TODAY, 201.95))
    assert L.settled == pytest.approx(1000 - 200.65)
    L.close("P1", 3.0, 0.65, TODAY)
    assert L.unsettled_total == pytest.approx(299.35)
    L.settle(TODAY)
    assert L.unsettled_total == pytest.approx(299.35)   # not yet
    L.settle(TODAY + dt.timedelta(days=1))
    assert L.unsettled_total == 0 and L.settled == pytest.approx(1098.70)


def test_friday_sale_settles_monday():
    L = Ledger(0, 1)
    fri = dt.date(2027, 3, 5)
    L.unsettled.append([100.0, __import__("bot.ledger", fromlist=["x"]).add_business_days(fri, 1)])
    L.settle(dt.date(2027, 3, 6))
    assert L.settled == 0
    L.settle(dt.date(2027, 3, 8))
    assert L.settled == 100


# --- profile -------------------------------------------------------------------
def test_profile_never_switches_itself_and_alerts_once(cfg):
    a = Alerts(None, echo=False)
    pm = ProfileManager(cfg.account, a)
    assert pm.start_of_day(2100, TODAY) == CASH_L2
    assert pm.start_of_day(2200, TODAY) == CASH_L2
    assert [h[1] for h in a.history] == ["PROFILE"]
    pm.start_of_day(1900, TODAY)
    pm.start_of_day(2050, TODAY)
    assert len(a.history) == 2   # re-armed after dropping below


def test_margin_l3_needs_config_and_start_of_day_bankroll(cfg):
    cfg.account["profile"] = MARGIN_L3
    pm = ProfileManager(cfg.account, NullAlerts())
    assert pm.start_of_day(1999, TODAY) == CASH_L2
    assert pm.start_of_day(2000, TODAY) == MARGIN_L3


# --- challenge -----------------------------------------------------------------
def test_milestones_step_risk_down_and_ratchet(cfg):
    ch = Challenge(cfg.challenge)
    assert ch.risk_pct() == 25
    ch.update(1600, TODAY)
    assert ch.risk_pct() == pytest.approx(20)
    ch.update(2600, TODAY)
    assert ch.risk_pct() == pytest.approx(10)
    ch.update(1200, TODAY)
    assert ch.risk_pct() == pytest.approx(10)
    assert ch.update(5000, TODAY) == "target"
    ch2 = Challenge(cfg.challenge)
    assert ch2.update(199.99, TODAY) == "ruin"


# --- strategy ---------------------------------------------------------------------
def test_no_fit_means_no_trade(cfg):
    r = rm(cfg)
    chain = [q(bid=5.0, ask=5.05, delta=0.40)]   # $505 > $250 budget
    intent, why = select_contract(chain, "call", TODAY, cfg.strategy, r, 250, 1000, NOW)
    assert intent is None and "risk budget" in why


def test_select_respects_dte_and_delta(cfg):
    r = rm(cfg)
    chain = [q(strike=500, delta=0.60), q(strike=505, delta=0.40, exp=TODAY + dt.timedelta(days=45)),
             q(strike=506, delta=0.38, bid=1.0, ask=1.02)]
    intent, _ = select_contract(chain, "call", TODAY, cfg.strategy, r, 250, 1000, NOW)
    assert intent.legs[0].contract.strike == 506 and intent.qty == 2


def test_exit_rules(cfg):
    c = OptionContract("SPY", TODAY + dt.timedelta(days=10), 500, "C")
    p = Position("P1", LONG_CALL, "SPY", [Leg(c, 1)], 1, 2.0, 0.65, TODAY, 200)
    assert exit_reason(p, 3.0, TODAY, cfg.strategy) == "take_profit"
    assert exit_reason(p, 1.2, TODAY, cfg.strategy) == "stop_loss"
    assert exit_reason(p, 2.1, TODAY, cfg.strategy) is None
    assert exit_reason(p, 2.1, TODAY + dt.timedelta(days=7), cfg.strategy) == "dte_exit"


# --- engine end-to-end ---------------------------------------------------------
class FakeSnap:
    def __init__(self, date, price, sigs=()):
        self.date = date
        self.now = dt.datetime.combine(date, dt.time(15, 45))
        self.spy_close = 500.0
        self.price = price
        self._sigs = list(sigs)

    def signals(self):
        return self._sigs

    def _q(self, c):
        return OptionQuote(c, self.price, self.price + 0.02, 0.4, 0.2, 5000, 500, self.now, 500)

    def chain(self, sym, side):
        return [self._q(OptionContract(sym, self.date + dt.timedelta(days=20), 505, "C"))]

    def quote(self, c):
        return self._q(c)


def test_engine_target_closes_everything_writes_report_and_waits(cfg, tmp_path):
    cfg.challenge["target"] = 1100
    J = Journal(None)
    e = Engine(cfg, SimBroker(cfg.sim["fills"], 0.65), J, NullAlerts(), reports_dir=tmp_path)
    d0 = dt.date(2027, 3, 1)
    e.on_day(FakeSnap(d0, 2.00, [Signal("SPY", "call", 1)]))
    assert len(e.ledger.positions) == 1
    e.on_day(FakeSnap(d0 + dt.timedelta(days=1), 3.20))   # +60%: take profit, bankroll > 1100
    e.on_day(FakeSnap(d0 + dt.timedelta(days=2), 3.20))
    assert e.halt_reason and "complete" in e.halt_reason
    assert not e.ledger.positions and not e.broker.positions()
    assert (tmp_path / "challenge_complete.md").exists()
    e.on_day(FakeSnap(d0 + dt.timedelta(days=3), 2.00, [Signal("SPY", "call", 1)]))
    assert not e.ledger.positions   # waits, no new trades


def test_engine_ruin_floor(cfg, tmp_path):
    cfg.challenge["ruin_floor"] = 900
    e = Engine(cfg, SimBroker(cfg.sim["fills"], 0.65), Journal(None), NullAlerts(), reports_dir=tmp_path)
    d0 = dt.date(2027, 3, 1)
    e.on_day(FakeSnap(d0, 2.00, [Signal("SPY", "call", 1)]))
    e.on_day(FakeSnap(d0 + dt.timedelta(days=1), 0.50))
    assert "failed" in e.halt_reason and (tmp_path / "challenge_failed.md").exists()
    assert not e.ledger.positions


def test_engine_reconciliation_mismatch_halts(cfg):
    J = Journal(None)
    e = Engine(cfg, SimBroker(cfg.sim["fills"], 0.65), J, NullAlerts())
    d0 = dt.date(2027, 3, 1)
    e.on_day(FakeSnap(d0, 2.00, [Signal("SPY", "call", 1)]))
    e.broker._pos.clear()   # broker lost the position
    e.on_day(FakeSnap(d0 + dt.timedelta(days=1), 2.0, [Signal("QQQ", "call", 1)]))
    assert e.halt_reason == "reconciliation mismatch" and J.counters["reconciliation_errors"] == 1


def test_sizes_off_challenge_bankroll_only(cfg):
    e = Engine(cfg, SimBroker(cfg.sim["fills"], 0.65), Journal(None), NullAlerts())
    e.on_day(FakeSnap(dt.date(2027, 3, 1), 0.50, [Signal("SPY", "call", 1)]))
    pos = next(iter(e.ledger.positions.values()))
    assert pos.max_loss <= 250 + 1e-9 and pos.qty == 4   # 4 x $52 + fees, not sized off any broker balance


# --- graduation ------------------------------------------------------------------
def test_graduation_gates(cfg):
    J = Journal(None)
    start = dt.date(2027, 1, 4)
    for i in range(70):
        J.trade({"underlying": "SPY", "legs": ["x"], "qty": 1, "contracts_per_fill": 1,
                 "entry_price": 1, "exit_price": 1.2, "entry_date": "", "exit_date": "",
                 "reason": "tp", "pnl": 15.0})
    for i in range(70):
        d = start + dt.timedelta(days=i)
        J.equity_row({"date": d.isoformat(), "bankroll": 1000 + 15 * i, "spy_close": 500 + 0.1 * i})
    md, res = graduation_report(J, cfg)
    assert res["all_pass"], md
    J.counters["reconciliation_errors"] = 1
    md, res = graduation_report(J, cfg)
    assert not res["all_pass"] and "Zero reconciliation errors | 1 | **FAIL**" in md
    # $0.05 haircut x 100 x 2 fills = $10 per trade; $8 edge turns negative
    for t in J.trades:
        t["pnl"] = 8.0
    _, res = graduation_report(J, cfg)
    assert dict(res["gates"])["Positive expectancy after commissions and a $0.05/contract haircut"] is False
