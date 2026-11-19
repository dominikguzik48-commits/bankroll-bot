"""Trade journal, equity curve and counters. In memory, optionally mirrored to disk."""
from __future__ import annotations

import csv
import json
from pathlib import Path

EQUITY_FIELDS = ["date", "bankroll", "settled", "unsettled", "open_positions", "profile",
                 "risk_pct", "spy_close"]


class Journal:
    def __init__(self, directory: Path | None = None):
        self.dir = Path(directory) if directory else None
        self.trades: list[dict] = []
        self.events: list[dict] = []
        self.equity: list[dict] = []
        self.counters = {"reconciliation_errors": 0, "risk_violations": 0,
                         "orders_rejected": 0, "orders_unfilled": 0}
        if self.dir:
            self.dir.mkdir(parents=True, exist_ok=True)
            self._load()

    # --- writes -----------------------------------------------------------
    def trade(self, rec: dict) -> None:
        self.trades.append(rec)
        self._append("trades.jsonl", rec)

    def event(self, rec: dict) -> None:
        self.events.append(rec)
        self._append("events.jsonl", rec)

    def equity_row(self, row: dict) -> None:
        self.equity.append(row)
        if self.dir:
            p = self.dir / "equity.csv"
            new = not p.exists()
            with open(p, "a", newline="") as f:
                w = csv.DictWriter(f, fieldnames=EQUITY_FIELDS)
                if new:
                    w.writeheader()
                w.writerow({k: row.get(k) for k in EQUITY_FIELDS})

    def bump(self, counter: str, n: int = 1) -> None:
        self.counters[counter] = self.counters.get(counter, 0) + n
        if self.dir:
            (self.dir / "counters.json").write_text(json.dumps(self.counters, indent=2))

    # --- io ---------------------------------------------------------------
    def _append(self, name: str, rec: dict) -> None:
        if self.dir:
            with open(self.dir / name, "a") as f:
                f.write(json.dumps(rec, default=str) + "\n")

    def _load(self) -> None:
        for name, target in (("trades.jsonl", self.trades), ("events.jsonl", self.events)):
            p = self.dir / name
            if p.exists():
                target.extend(json.loads(l) for l in p.read_text().splitlines() if l.strip())
        p = self.dir / "equity.csv"
        if p.exists():
            with open(p) as f:
                self.equity.extend(csv.DictReader(f))
        p = self.dir / "counters.json"
        if p.exists():
            self.counters.update(json.loads(p.read_text()))
