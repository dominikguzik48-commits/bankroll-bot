"""Alerts: append to logs/alerts.log and echo. Hook push/Slack/email in `send` later."""
from __future__ import annotations

import datetime as dt
from pathlib import Path


class Alerts:
    def __init__(self, log_path: Path | None = None, echo: bool = True):
        self.log_path = Path(log_path) if log_path else None
        self.echo = echo
        self.history: list[tuple[str, str, str]] = []

    def alert(self, kind: str, msg: str, when: dt.date | None = None) -> None:
        stamp = (when or dt.date.today()).isoformat()
        self.history.append((stamp, kind, msg))
        line = f"{dt.datetime.now().isoformat(timespec='seconds')} [{stamp}] {kind}: {msg}"
        if self.log_path:
            self.log_path.parent.mkdir(parents=True, exist_ok=True)
            with open(self.log_path, "a") as f:
                f.write(line + "\n")
        if self.echo:
            print(line)
        self.send(kind, msg)

    def send(self, kind: str, msg: str) -> None:
        """Override to push alerts somewhere (phone, Slack, email). No-op by default."""


class NullAlerts(Alerts):
    def __init__(self):
        super().__init__(None, echo=False)
