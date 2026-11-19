"""Bankroll Challenge controller: target, ruin floor, milestones and the risk step-down."""
from __future__ import annotations

import datetime as dt

ACTIVE, COMPLETE, FAILED = "active", "complete", "failed"


class Challenge:
    def __init__(self, cfg: dict, alerts=None):
        self.cfg = cfg
        self.alerts = alerts
        self.status = ACTIVE
        self.milestones_hit: list[dict] = []
        self.peak = float(cfg["starting_bankroll"])
        self.started: dt.date | None = None
        self.finished: dt.date | None = None

    @property
    def milestones(self) -> list[float]:
        return sorted(float(m) for m in self.cfg.get("milestones", []))

    def risk_pct(self) -> float:
        """Current max risk per trade, % of bankroll, after milestone step-downs."""
        base = float(self.cfg["risk_per_trade_pct"])
        floor = float(self.cfg.get("min_risk_per_trade_pct", base))
        n = len(self.milestones)
        if n == 0 or base <= floor:
            return base
        k = len(self.milestones_hit)
        return base - (base - floor) * min(k, n) / n

    def update(self, bankroll: float, today: dt.date) -> str | None:
        """Feed the latest bankroll. Returns 'target', 'ruin' or None."""
        if self.status != ACTIVE:
            return None
        if self.started is None:
            self.started = today
        self.peak = max(self.peak, bankroll)
        hit = {m["level"] for m in self.milestones_hit}
        for level in self.milestones:
            if bankroll >= level and level not in hit:
                old = self.risk_pct()
                self.milestones_hit.append({"level": level, "date": today.isoformat(),
                                            "bankroll": round(bankroll, 2)})
                new = self.risk_pct()
                if self.alerts:
                    self.alerts.alert("MILESTONE",
                                      f"Bankroll ${bankroll:,.2f} passed ${level:,.0f}. Risk manager "
                                      f"re-evaluated sizing: risk per trade {old:.1f}% -> {new:.1f}%.",
                                      today)
        if bankroll >= float(self.cfg["target"]):
            return "target"
        if bankroll < float(self.cfg["ruin_floor"]):
            return "ruin"
        return None

    def finish(self, outcome: str, today: dt.date) -> None:
        self.status = COMPLETE if outcome == "target" else FAILED
        self.finished = today

    def to_dict(self) -> dict:
        return {"status": self.status, "milestones_hit": self.milestones_hit, "peak": self.peak,
                "started": self.started.isoformat() if self.started else None,
                "finished": self.finished.isoformat() if self.finished else None}

    def load(self, d: dict) -> None:
        self.status = d.get("status", ACTIVE)
        self.milestones_hit = d.get("milestones_hit", [])
        self.peak = d.get("peak", self.peak)
        self.started = dt.date.fromisoformat(d["started"]) if d.get("started") else None
        self.finished = dt.date.fromisoformat(d["finished"]) if d.get("finished") else None
