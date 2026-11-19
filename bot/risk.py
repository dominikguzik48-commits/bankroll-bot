"""Risk manager. Every opening order must be approved here; it has the final say.

Challenge mode changes only two limits: max risk per trade (% of the challenge
bankroll, with milestone step-downs) and max open positions. Everything else is the
same in every mode: defined risk only, liquidity, stale data, blackout days,
reconciliation, kill switch, account-profile permissions, settled cash.
"""
from __future__ import annotations

import datetime as dt
from dataclasses import dataclass, field
from pathlib import Path

from .models import (IRON_CONDOR, LONG_CALL, LONG_PUT, MULTIPLIER, VERTICAL, Leg,
                     OptionQuote, OrderIntent, Position)


@dataclass
class RiskContext:
    today: dt.date
    now: dt.datetime
    bankroll: float
    settled_cash: float
    open_positions: int
    profile: str
    allowed_structures: list[str]
    settled_cash_only: bool
    reconciliation_ok: bool = True
    halted: str | None = None


@dataclass
class RiskDecision:
    approved: bool
    reasons: list[str] = field(default_factory=list)
    max_loss: float = 0.0
    budget: float = 0.0

    def __bool__(self) -> bool:
        return self.approved


class RiskManager:
    def __init__(self, risk_cfg: dict, challenge_cfg: dict | None = None, challenge=None,
                 root: Path | None = None, extra_blackouts: set[dt.date] | None = None):
        self.cfg = risk_cfg
        self.challenge_cfg = challenge_cfg if (challenge_cfg or {}).get("enabled") else None
        self.challenge = challenge
        self.root = Path(root) if root else None
        self.blackouts = {dt.date.fromisoformat(str(d)) for d in risk_cfg.get("blackout_dates") or []}
        if extra_blackouts:
            self.blackouts |= set(extra_blackouts)
        self.kill_switch_override: bool | None = None   # tests / sim

    # --- limits -----------------------------------------------------------
    @property
    def fees_per_contract(self) -> float:
        return float(self.cfg.get("fees_per_contract", 0.0))

    def risk_pct(self) -> float:
        if self.challenge_cfg:
            pct = self.challenge.risk_pct() if self.challenge else float(self.challenge_cfg["risk_per_trade_pct"])
            return min(pct, float(self.challenge_cfg["risk_per_trade_pct"]))
        return float(self.cfg["max_risk_per_trade_pct"])

    def max_open_positions(self) -> int:
        if self.challenge_cfg:
            return int(self.challenge_cfg["max_open_positions"])
        return int(self.cfg["max_open_positions"])

    def budget(self, bankroll: float) -> float:
        return max(0.0, bankroll) * self.risk_pct() / 100.0

    def kill_switch_active(self) -> bool:
        if self.kill_switch_override is not None:
            return self.kill_switch_override
        f = self.cfg.get("kill_switch_file")
        if not f:
            return False
        p = Path(f)
        if not p.is_absolute() and self.root:
            p = self.root / p
        return p.exists()

    def is_blackout(self, d: dt.date) -> bool:
        return d in self.blackouts

    # --- individual checks --------------------------------------------------
    def liquidity_problems(self, q: OptionQuote) -> list[str]:
        L = self.cfg["liquidity"]
        out = []
        sym = q.contract.symbol
        if q.bid < float(L["min_bid"]):
            out.append(f"{sym}: bid {q.bid:.2f} < min {L['min_bid']}")
        if q.spread > float(L["max_spread_abs"]) + 1e-9:
            out.append(f"{sym}: spread {q.spread:.2f} > max {L['max_spread_abs']}")
        if q.mid <= 0 or q.spread / q.mid > float(L["max_spread_pct_of_mid"]) + 1e-9:
            out.append(f"{sym}: spread is {q.spread / max(q.mid, 1e-9):.0%} of mid")
        if q.open_interest < int(L["min_open_interest"]):
            out.append(f"{sym}: open interest {q.open_interest} < {L['min_open_interest']}")
        if q.volume < int(L["min_volume"]):
            out.append(f"{sym}: volume {q.volume} < {L['min_volume']}")
        return out

    def stale_problems(self, q: OptionQuote, now: dt.datetime) -> list[str]:
        age = (now - q.timestamp).total_seconds()
        lim = float(self.cfg["stale_data_max_age_seconds"])
        return [f"{q.contract.symbol}: quote is {age:.0f}s old (max {lim:.0f}s)"] if age > lim else []

    @staticmethod
    def naked_short_problems(legs: list[Leg]) -> list[str]:
        """Every short leg must be covered by long legs of the same underlying, right and expiry,
        with at least the same quantity. (Any strike caps the loss: a lower-strike long call
        makes a debit spread, a higher-strike one a credit spread; both are defined risk.)
        Different expiries do not count as cover."""
        longs = [[l.contract, l.ratio] for l in legs if l.side > 0]
        out = []
        for s in (l for l in legs if l.side < 0):
            need = s.ratio
            c = s.contract
            for cand in sorted(longs, key=lambda x: abs(x[0].strike - c.strike)):
                lc, cap = cand
                if cap <= 0 or lc.right != c.right or lc.expiry != c.expiry or lc.underlying != c.underlying:
                    continue
                take = min(cap, need)
                cand[1] -= take
                need -= take
                if need == 0:
                    break
            if need > 0:
                out.append(f"naked short: {c.symbol} not covered by a long leg")
        return out

    @staticmethod
    def structure_problems(intent: OrderIntent) -> list[str]:
        legs, s = intent.legs, intent.structure
        rights = [l.contract.right for l in legs]
        sides = [l.side for l in legs]
        if s == LONG_CALL:
            ok = len(legs) == 1 and rights == ["C"] and sides == [1]
        elif s == LONG_PUT:
            ok = len(legs) == 1 and rights == ["P"] and sides == [1]
        elif s == VERTICAL:
            ok = (len(legs) == 2 and rights[0] == rights[1] and sorted(sides) == [-1, 1]
                  and legs[0].contract.expiry == legs[1].contract.expiry
                  and legs[0].ratio == legs[1].ratio
                  and legs[0].contract.strike != legs[1].contract.strike)
        elif s == IRON_CONDOR:
            puts = [l for l in legs if l.contract.right == "P"]
            calls = [l for l in legs if l.contract.right == "C"]
            ok = (len(legs) == 4 and len(puts) == 2 and len(calls) == 2
                  and sorted(l.side for l in puts) == [-1, 1]
                  and sorted(l.side for l in calls) == [-1, 1]
                  and len({l.contract.expiry for l in legs}) == 1
                  and len({l.ratio for l in legs}) == 1)
        else:
            return [f"unknown structure {s!r}"]
        if any(l.contract.underlying != intent.underlying for l in legs):
            ok = False
        return [] if ok else [f"legs do not form a valid {s}"]

    def max_loss(self, intent: OrderIntent) -> float:
        """Worst-case loss in dollars at expiry, plus entry and exit fees."""
        fees = 2 * self.fees_per_contract * intent.contracts_per_fill
        strikes = sorted({l.contract.strike for l in intent.legs})
        grid = [0.0] + strikes + [strikes[-1] * 10 + 1000]

        def payoff(S):
            v = 0.0
            for l in intent.legs:
                k = l.contract.strike
                intrinsic = max(S - k, 0.0) if l.contract.right == "C" else max(k - S, 0.0)
                v += l.side * l.ratio * intrinsic
            return v - intent.limit_price

        worst = min(payoff(S) for S in grid)
        return max(0.0, -worst) * MULTIPLIER * intent.qty + fees

    def width_problems(self, intent: OrderIntent) -> list[str]:
        if intent.structure not in (VERTICAL, IRON_CONDOR):
            return []
        out = []
        min_w = float(self.cfg.get("min_spread_width", 0))
        nv = (self.challenge_cfg or {}).get("narrow_verticals")
        by_right: dict[str, list[float]] = {}
        for l in intent.legs:
            by_right.setdefault(l.contract.right, []).append(l.contract.strike)
        for right, ks in by_right.items():
            w = max(ks) - min(ks)
            if w >= min_w - 1e-9:
                continue
            if not nv or w < float(nv["width"]) - 1e-9:
                out.append(f"{right} spread width {w:g} < minimum {min_w:g}")
                continue
            und = intent.underlying
            q0 = next(iter(intent.quotes.values()))
            penny = all(q.spread <= float(nv["max_leg_spread"]) + 1e-9 for q in intent.quotes.values())
            if not (und in nv["always_allowed"]
                    or (q0.underlying_price < float(nv["max_underlying_price"]) and penny)):
                out.append(f"${w:g}-wide spread only allowed on {nv['always_allowed']} or "
                           f"penny-wide names under ${nv['max_underlying_price']}")
        return out

    # --- the gate -----------------------------------------------------------
    def check(self, intent: OrderIntent, ctx: RiskContext) -> RiskDecision:
        r: list[str] = []
        if not intent.opening:
            return RiskDecision(True, ["closing order"])
        if ctx.halted:
            r.append(f"trading halted: {ctx.halted}")
        if self.kill_switch_active():
            r.append("kill switch is on")
        if not ctx.reconciliation_ok:
            r.append("reconciliation failed")
        if self.is_blackout(ctx.today):
            r.append(f"{ctx.today} is a blackout day")
        if intent.qty < 1:
            r.append("quantity < 1")
        r += self.structure_problems(intent)
        if intent.structure not in ctx.allowed_structures:
            r.append(f"{intent.structure} not permitted on profile {ctx.profile}")
        r += self.naked_short_problems(intent.legs)   # always, no exceptions
        r += self.width_problems(intent)
        for l in intent.legs:
            q = intent.quotes.get(l.contract.symbol)
            if q is None:
                r.append(f"no quote for {l.contract.symbol}")
                continue
            r += self.stale_problems(q, ctx.now)
            r += self.liquidity_problems(q)
        if ctx.open_positions + 1 > self.max_open_positions():
            r.append(f"max open positions ({self.max_open_positions()}) reached")
        budget = self.budget(ctx.bankroll)
        ml = self.max_loss(intent) if intent.legs else 0.0
        if ml > budget + 1e-9:
            r.append(f"max loss ${ml:,.2f} > {self.risk_pct():.1f}% of bankroll (${budget:,.2f})")
        if intent.limit_price > 0:
            cost = intent.limit_price * MULTIPLIER * intent.qty + self.fees_per_contract * intent.contracts_per_fill
            if ctx.settled_cash_only and cost > ctx.settled_cash + 1e-9:
                r.append(f"cost ${cost:,.2f} > settled cash ${ctx.settled_cash:,.2f}")
            if cost > ctx.bankroll + 1e-9:
                r.append(f"cost ${cost:,.2f} > bankroll ${ctx.bankroll:,.2f}")
        return RiskDecision(not r, r, ml, budget)

    def audit(self, positions: list[Position]) -> list[str]:
        """Post-hoc check of what is actually open. Anything found here is a violation."""
        v = []
        if len(positions) > self.max_open_positions():
            v.append(f"{len(positions)} open positions > max {self.max_open_positions()}")
        for p in positions:
            for msg in self.naked_short_problems(p.legs):
                v.append(f"{p.id}: {msg}")
        return v
