"""Daily trading loop. Identical for the simulator and for paper trading; only the
broker and the market snapshot differ."""
from __future__ import annotations

import datetime as dt
import json
from pathlib import Path
from typing import Protocol

from .alerts import Alerts
from .challenge import ACTIVE, Challenge
from .config import Config
from .journal import Journal
from .ledger import Ledger
from .models import MULTIPLIER, Leg, OptionContract, OptionQuote, OrderIntent, Position, Signal
from .profile import ProfileManager
from .risk import RiskContext, RiskManager
from .strategy import exit_reason, select_contract


class MarketSnapshot(Protocol):
    date: dt.date
    now: dt.datetime
    spy_close: float | None

    def signals(self) -> list[Signal]: ...
    def chain(self, symbol: str, side: str) -> list[OptionQuote]: ...
    def quote(self, contract: OptionContract) -> OptionQuote | None: ...


class Engine:
    def __init__(self, cfg: Config, broker, journal: Journal, alerts: Alerts,
                 reports_dir: Path | None = None, extra_blackouts=None, verbose_events=True):
        self.cfg = cfg
        self.broker = broker
        self.journal = journal
        self.alerts = alerts
        self.reports_dir = Path(reports_dir) if reports_dir else None
        self.verbose = verbose_events
        ch = cfg.challenge
        self.challenge_mode = bool(ch.get("enabled"))
        self.challenge = Challenge(ch, alerts)
        self.ledger = Ledger(ch["starting_bankroll"], cfg.account.get("settlement_days", 1))
        self.profiles = ProfileManager(cfg.account, alerts)
        self.risk = RiskManager(cfg.risk, ch, self.challenge, cfg.root, extra_blackouts)
        self.halt_reason: str | None = None
        self._last_marks: dict[str, tuple[float, float]] = {}
        self._kill_alerted = False
        self.today_profile = "CASH_L2"

    # ------------------------------------------------------------------ helpers
    def _event(self, today, kind, **kw):
        if self.verbose or kind not in ("skip", "reject_detail"):
            self.journal.event({"date": today.isoformat(), "kind": kind, **kw})

    def _mark(self, pos: Position, snap) -> tuple[float, float]:
        """(mid value per unit, liquidation dollars after exit fees)."""
        mid = bid_side = 0.0
        for l in pos.legs:
            q = snap.quote(l.contract)
            if q is None:
                return self._last_marks.get(pos.id, (pos.entry_price, 0.0))
            mid += l.side * l.ratio * q.mid
            bid_side += l.side * l.ratio * (q.bid if l.side > 0 else q.ask)
        fees = self.risk.fees_per_contract * pos.qty * sum(l.ratio for l in pos.legs)
        m = (mid, bid_side * MULTIPLIER * pos.qty - fees)
        self._last_marks[pos.id] = m
        return m

    def bankroll(self, snap) -> float:
        return self.ledger.equity({p.id: self._mark(p, snap)[1] for p in self.ledger.positions.values()})

    def _close(self, pos: Position, snap, reason: str) -> bool:
        legs = [Leg(l.contract, -l.side, l.ratio) for l in pos.legs]
        quotes = {}
        for l in legs:
            q = snap.quote(l.contract)
            if q is None:
                self.alerts.alert("EXIT_BLOCKED", f"no quote to close {pos.id} {l.contract.symbol}", snap.date)
                return False
            quotes[l.contract.symbol] = q
        intent = OrderIntent(pos.structure, pos.underlying, legs, pos.qty,
                             limit_price=sum(l.side * l.ratio * quotes[l.contract.symbol].mid for l in legs),
                             quotes=quotes, reason=reason, opening=False)
        fill = self.broker.execute(intent, snap.now)
        if fill is None:
            self.journal.bump("orders_unfilled")
            self.alerts.alert("EXIT_UNFILLED", f"close of {pos.id} not filled ({reason})", snap.date)
            return False
        exit_price = -fill.price
        pnl = self.ledger.close(pos.id, exit_price, fill.fees, snap.date)
        self._last_marks.pop(pos.id, None)
        self.journal.trade({
            "id": pos.id, "underlying": pos.underlying, "structure": pos.structure,
            "legs": [l.contract.symbol for l in pos.legs], "qty": pos.qty,
            "contracts_per_fill": pos.qty * sum(l.ratio for l in pos.legs),
            "entry_date": pos.entry_date.isoformat(), "exit_date": snap.date.isoformat(),
            "entry_price": round(pos.entry_price, 4), "exit_price": round(exit_price, 4),
            "fees": round(pos.entry_fees + fill.fees, 2), "pnl": round(pnl, 2),
            "max_loss": round(pos.max_loss, 2), "reason": reason,
            "bankroll_after": round(self.ledger.cash + sum(
                self._mark(p, snap)[1] for p in self.ledger.positions.values()), 2),
        })
        return True

    def close_all(self, snap, reason: str) -> bool:
        ok = True
        for pos in list(self.ledger.positions.values()):
            ok &= self._close(pos, snap, reason)
        return ok

    def _reconcile(self, today) -> bool:
        expected: dict[str, int] = {}
        for p in self.ledger.positions.values():
            for sym, q in p.broker_positions().items():
                expected[sym] = expected.get(sym, 0) + q
        expected = {k: v for k, v in expected.items() if v}
        actual = self.broker.positions()
        if expected == actual:
            return True
        self.journal.bump("reconciliation_errors")
        self.halt_reason = "reconciliation mismatch"
        self.alerts.alert("RECONCILIATION",
                          f"ledger {expected} != broker {actual}. Trading halted until you clear it.", today)
        return False

    def _challenge_check(self, bankroll: float, snap) -> bool:
        if not self.challenge_mode:
            return False
        outcome = self.challenge.update(bankroll, snap.date)
        if not outcome:
            return False
        self.close_all(snap, "challenge_target" if outcome == "target" else "challenge_ruin")
        self.challenge.finish(outcome, snap.date)
        final = self.ledger.cash
        if outcome == "target":
            self.halt_reason = "challenge complete: waiting for new instructions"
            self.alerts.alert("CHALLENGE_COMPLETE",
                              f"Target hit. Bankroll ${final:,.2f}. All positions closed, trading halted.",
                              snap.date)
        else:
            self.halt_reason = "challenge failed: ruin floor"
            self.alerts.alert("CHALLENGE_FAILED",
                              f"Bankroll ${bankroll:,.2f} fell below the "
                              f"${self.cfg.challenge['ruin_floor']:,.0f} floor. All closed, halted.",
                              snap.date)
        if self.reports_dir:
            from .reports import write_challenge_report
            write_challenge_report(self, outcome, snap.date)
        return True

    def _record(self, snap) -> None:
        self.journal.equity_row({
            "date": snap.date.isoformat(), "bankroll": round(self.bankroll(snap), 2),
            "settled": round(self.ledger.settled, 2), "unsettled": round(self.ledger.unsettled_total, 2),
            "open_positions": len(self.ledger.positions), "profile": self.today_profile,
            "risk_pct": round(self.risk.risk_pct(), 2), "spy_close": snap.spy_close,
        })

    # ------------------------------------------------------------------ the day
    def on_day(self, snap) -> None:
        today = snap.date
        if self.halt_reason:
            self._record(snap)
            return
        self.ledger.settle(today)
        if not self._reconcile(today):
            self._record(snap)
            return

        if self.risk.kill_switch_active():
            if not self._kill_alerted:
                self.alerts.alert("KILL_SWITCH", "Kill switch on: flattening, no new trades.", today)
                self._kill_alerted = True
            self.close_all(snap, "kill_switch")
            self._record(snap)
            return
        self._kill_alerted = False

        sod = self.bankroll(snap)
        self.today_profile = self.profiles.start_of_day(sod, today)
        if self.profiles.today_note:
            self._event(today, "profile", note=self.profiles.today_note)
        if self._challenge_check(sod, snap):
            self._record(snap)
            return

        # exits first
        for pos in list(self.ledger.positions.values()):
            mid, _ = self._mark(pos, snap)
            why = exit_reason(pos, mid, today, self.cfg.strategy)
            if why:
                self._close(pos, snap, why)

        bankroll = self.bankroll(snap)
        if self._challenge_check(bankroll, snap):
            self._record(snap)
            return

        violations = self.risk.audit(list(self.ledger.positions.values()))
        if violations:
            self.journal.bump("risk_violations", len(violations))
            self.halt_reason = "risk rule violation"
            self.alerts.alert("RISK_VIOLATION", "; ".join(violations), today)
            self._record(snap)
            return

        self._try_entry(snap, bankroll)
        self._record(snap)

    def _try_entry(self, snap, bankroll: float) -> None:
        today = snap.date
        if len(self.ledger.positions) >= self.risk.max_open_positions():
            return
        if self.risk.is_blackout(today):
            return
        sigs = snap.signals()
        if not sigs:
            return
        held = {p.underlying for p in self.ledger.positions.values()}
        settled_only = self.profiles.settled_cash_only(self.today_profile)
        spendable = self.ledger.settled if settled_only else self.ledger.cash
        budget = self.risk.budget(bankroll)
        ctx = RiskContext(today=today, now=snap.now, bankroll=bankroll,
                          settled_cash=self.ledger.settled,
                          open_positions=len(self.ledger.positions), profile=self.today_profile,
                          allowed_structures=self.profiles.allowed_structures(self.today_profile),
                          settled_cash_only=settled_only, reconciliation_ok=True,
                          halted=self.halt_reason)
        for sig in sorted(sigs, key=lambda s: -s.strength):
            if sig.symbol in held:
                continue
            intent, why = select_contract(snap.chain(sig.symbol, sig.side), sig.side, today,
                                          self.cfg.strategy, self.risk, budget, spendable, snap.now)
            if intent is None:
                self._event(today, "skip", symbol=sig.symbol, side=sig.side, why=why)
                continue
            decision = self.risk.check(intent, ctx)
            if not decision:
                self.journal.bump("orders_rejected")
                self._event(today, "reject", symbol=sig.symbol, reasons=decision.reasons)
                continue
            fill = self.broker.execute(intent, snap.now)
            if fill is None:
                self.journal.bump("orders_unfilled")
                continue
            pos = Position(self.ledger.new_position_id(), intent.structure, intent.underlying,
                           intent.legs, intent.qty, fill.price, fill.fees, today, decision.max_loss)
            self.ledger.open(pos)
            self._event(today, "open", id=pos.id, symbol=sig.symbol, side=sig.side,
                        contract=intent.legs[0].contract.symbol, qty=pos.qty,
                        price=fill.price, max_loss=round(decision.max_loss, 2),
                        budget=round(decision.budget, 2), note=intent.reason)
            return

    # ------------------------------------------------------------------ state
    def save_state(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({
            "ledger": self.ledger.to_dict(), "challenge": self.challenge.to_dict(),
            "profiles": self.profiles.to_dict(), "halt_reason": self.halt_reason,
        }, indent=2, default=str))

    def load_state(self, path: Path) -> None:
        if not path.exists():
            return
        d = json.loads(path.read_text())
        self.ledger = Ledger.from_dict(d["ledger"])
        self.challenge.load(d["challenge"])
        self.profiles.load(d["profiles"])
        self.halt_reason = d.get("halt_reason")
        if self.challenge.status != ACTIVE and not self.halt_reason:
            self.halt_reason = f"challenge {self.challenge.status}"
