"""Broker adapters. The engine only talks to this interface."""
from __future__ import annotations

import datetime as dt
from abc import ABC, abstractmethod

from .models import Fill, OrderIntent


class Broker(ABC):
    @abstractmethod
    def positions(self) -> dict[str, int]:
        """Signed contract quantities by contract symbol for the positions THIS BOT opened
        (the challenge book), not everything in the account."""

    @abstractmethod
    def execute(self, intent: OrderIntent, now: dt.datetime) -> Fill | None:
        """Fill.price is the net per unit: sum(side * ratio * leg_price). None = not filled."""


class SimBroker(Broker):
    """Fills at mid +/- a fraction of the half-spread plus extra slippage, capped at the
    quoted market (marketable limit orders). Charges per-contract fees."""

    def __init__(self, fill_cfg: dict, fees_per_contract: float):
        self.frac = float(fill_cfg["fill_frac_of_half_spread"])
        self.extra = float(fill_cfg["extra_slippage"])
        self.fee = float(fees_per_contract)
        self._pos: dict[str, int] = {}

    def positions(self) -> dict[str, int]:
        return {k: v for k, v in self._pos.items() if v != 0}

    def execute(self, intent: OrderIntent, now: dt.datetime) -> Fill | None:
        net = 0.0
        for l in intent.legs:
            q = intent.quotes[l.contract.symbol]
            half = q.spread / 2
            if l.side > 0:
                px = min(q.mid + self.frac * half + self.extra, q.ask)
            else:
                px = max(q.mid - self.frac * half - self.extra, q.bid, 0.0)
            net += l.side * l.ratio * px
        for l in intent.legs:
            s = l.contract.symbol
            self._pos[s] = self._pos.get(s, 0) + l.side * l.ratio * intent.qty
        return Fill(round(net, 4), intent.qty, self.fee * intent.contracts_per_fill, now)


class WebullPaperBroker(Broker):
    """Placeholder for the Webull paper account.

    Not implemented: this container has no network route to Webull and no credentials,
    and the challenge must not start without your explicit go-ahead. To implement:
      - positions(): read option positions from the paper account, filtered to the order
        IDs/tags this bot placed (the account holds more than the challenge bankroll).
      - execute(): send marketable limit orders (single leg or multi-leg), wait for fill
        or cancel after a timeout, return the actual fill price and fees.
    """

    def __init__(self, *args, **kwargs):
        raise NotImplementedError(
            "Webull paper adapter not implemented yet. Nothing will be sent to Webull.")

    def positions(self) -> dict[str, int]:  # pragma: no cover
        raise NotImplementedError

    def execute(self, intent, now):  # pragma: no cover
        raise NotImplementedError
