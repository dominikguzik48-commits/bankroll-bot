"""Virtual bankroll ledger, separate from whatever the broker account holds.

Cash is split into settled and unsettled. Sale proceeds become settled after
`settlement_days` business days (T+1 for options); until then they count toward the
bankroll's value but cannot fund a new purchase in a cash account.
"""
from __future__ import annotations

import datetime as dt

import numpy as np

from .models import MULTIPLIER, Position


def add_business_days(d: dt.date, n: int) -> dt.date:
    return np.busday_offset(np.datetime64(d, "D"), n, roll="forward").astype(dt.date)


class Ledger:
    def __init__(self, starting_bankroll: float, settlement_days: int = 1):
        self.starting_bankroll = float(starting_bankroll)
        self.settlement_days = int(settlement_days)
        self.settled = float(starting_bankroll)
        self.unsettled: list[list] = []   # [amount, settle_date]
        self.positions: dict[str, Position] = {}
        self.realized_pnl = 0.0
        self.fees_paid = 0.0
        self._next_id = 1

    # --- cash -----------------------------------------------------------
    def settle(self, today: dt.date) -> float:
        moved, keep = 0.0, []
        for amt, d in self.unsettled:
            if d <= today:
                moved += amt
            else:
                keep.append([amt, d])
        self.unsettled = keep
        self.settled += moved
        return moved

    @property
    def unsettled_total(self) -> float:
        return sum(a for a, _ in self.unsettled)

    @property
    def cash(self) -> float:
        return self.settled + self.unsettled_total

    def equity(self, liquidation_values: dict[str, float]) -> float:
        """Bankroll = all cash + what open positions would fetch if closed now (after fees)."""
        return self.cash + sum(liquidation_values.get(pid, 0.0) for pid in self.positions)

    # --- trades ---------------------------------------------------------
    def new_position_id(self) -> str:
        pid = f"P{self._next_id:05d}"
        self._next_id += 1
        return pid

    def open(self, pos: Position) -> None:
        """Debit (or credit) the entry. Debits must come out of settled cash."""
        cash_flow = -pos.entry_price * MULTIPLIER * pos.qty - pos.entry_fees
        if cash_flow < 0:
            if -cash_flow > self.settled + 1e-9:
                raise ValueError("entry exceeds settled cash")
            self.settled += cash_flow
        else:
            self.settled += cash_flow  # credit received at open (margin profile only)
        self.fees_paid += pos.entry_fees
        self.positions[pos.id] = pos

    def close(self, pid: str, exit_price: float, exit_fees: float, date: dt.date) -> float:
        """exit_price is net per unit in the closing direction (>0 = we receive)."""
        pos = self.positions.pop(pid)
        proceeds = exit_price * MULTIPLIER * pos.qty - exit_fees
        if proceeds >= 0:
            self.unsettled.append([proceeds, add_business_days(date, self.settlement_days)])
        else:
            self.settled += proceeds  # paying to close a credit structure
        self.fees_paid += exit_fees
        pnl = (exit_price - pos.entry_price) * MULTIPLIER * pos.qty - pos.entry_fees - exit_fees
        self.realized_pnl += pnl
        return pnl

    # --- persistence ----------------------------------------------------
    def to_dict(self) -> dict:
        return {
            "starting_bankroll": self.starting_bankroll,
            "settlement_days": self.settlement_days,
            "settled": self.settled,
            "unsettled": [[a, d.isoformat()] for a, d in self.unsettled],
            "positions": [p.to_dict() for p in self.positions.values()],
            "realized_pnl": self.realized_pnl,
            "fees_paid": self.fees_paid,
            "next_id": self._next_id,
        }

    @staticmethod
    def from_dict(d: dict) -> "Ledger":
        L = Ledger(d["starting_bankroll"], d["settlement_days"])
        L.settled = d["settled"]
        L.unsettled = [[a, dt.date.fromisoformat(s)] for a, s in d["unsettled"]]
        L.positions = {p["id"]: Position.from_dict(p) for p in d["positions"]}
        L.realized_pnl = d["realized_pnl"]
        L.fees_paid = d["fees_paid"]
        L._next_id = d["next_id"]
        return L
