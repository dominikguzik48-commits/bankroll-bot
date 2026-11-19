"""Plain data types shared by the live bot and the simulator."""
from __future__ import annotations

import datetime as dt
from dataclasses import asdict, dataclass, field

MULTIPLIER = 100

# structure names
LONG_CALL = "long_call"
LONG_PUT = "long_put"
VERTICAL = "vertical_spread"
IRON_CONDOR = "iron_condor"


@dataclass(frozen=True)
class OptionContract:
    underlying: str
    expiry: dt.date
    strike: float
    right: str  # "C" or "P"

    @property
    def symbol(self) -> str:
        return f"{self.underlying} {self.expiry:%y%m%d} {self.right}{self.strike:g}"

    def to_dict(self) -> dict:
        return {"underlying": self.underlying, "expiry": self.expiry.isoformat(),
                "strike": self.strike, "right": self.right}

    @staticmethod
    def from_dict(d: dict) -> "OptionContract":
        return OptionContract(d["underlying"], dt.date.fromisoformat(d["expiry"]),
                              float(d["strike"]), d["right"])


@dataclass
class OptionQuote:
    contract: OptionContract
    bid: float
    ask: float
    delta: float
    iv: float
    open_interest: int
    volume: int
    timestamp: dt.datetime
    underlying_price: float

    @property
    def mid(self) -> float:
        return (self.bid + self.ask) / 2

    @property
    def spread(self) -> float:
        return self.ask - self.bid


@dataclass(frozen=True)
class Leg:
    contract: OptionContract
    side: int        # +1 buy, -1 sell
    ratio: int = 1   # contracts per unit of the structure

    def to_dict(self) -> dict:
        return {"contract": self.contract.to_dict(), "side": self.side, "ratio": self.ratio}

    @staticmethod
    def from_dict(d: dict) -> "Leg":
        return Leg(OptionContract.from_dict(d["contract"]), int(d["side"]), int(d["ratio"]))


@dataclass
class OrderIntent:
    """An opening (or closing) order. limit_price is net per unit: >0 debit, <0 credit."""
    structure: str
    underlying: str
    legs: list[Leg]
    qty: int
    limit_price: float
    quotes: dict[str, OptionQuote]   # by contract symbol
    reason: str = ""
    opening: bool = True

    @property
    def contracts_per_fill(self) -> int:
        return self.qty * sum(l.ratio for l in self.legs)


@dataclass
class Fill:
    price: float   # net per unit, >0 debit paid, <0 credit received (opening sense)
    qty: int
    fees: float
    timestamp: dt.datetime


@dataclass
class Position:
    id: str
    structure: str
    underlying: str
    legs: list[Leg]
    qty: int
    entry_price: float   # net per unit (debit > 0)
    entry_fees: float
    entry_date: dt.date
    max_loss: float      # dollars, incl. fees, as approved by the risk manager

    @property
    def expiry(self) -> dt.date:
        return min(l.contract.expiry for l in self.legs)

    def broker_positions(self) -> dict[str, int]:
        return {l.contract.symbol: l.side * l.ratio * self.qty for l in self.legs}

    def to_dict(self) -> dict:
        d = asdict(self)
        d["legs"] = [l.to_dict() for l in self.legs]
        d["entry_date"] = self.entry_date.isoformat()
        return d

    @staticmethod
    def from_dict(d: dict) -> "Position":
        return Position(d["id"], d["structure"], d["underlying"],
                        [Leg.from_dict(x) for x in d["legs"]], int(d["qty"]),
                        float(d["entry_price"]), float(d["entry_fees"]),
                        dt.date.fromisoformat(d["entry_date"]), float(d["max_loss"]))


@dataclass
class Signal:
    symbol: str
    side: str        # "call" or "put"
    strength: float = 0.0
