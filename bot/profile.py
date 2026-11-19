"""Account profile (CASH_L2 / MARGIN_L3). The bot never switches profiles on its own."""
from __future__ import annotations

import datetime as dt

CASH_L2 = "CASH_L2"
MARGIN_L3 = "MARGIN_L3"


class ProfileManager:
    def __init__(self, account_cfg: dict, alerts=None):
        self.cfg = account_cfg
        self.alerts = alerts
        self.above_alert_level = False   # re-arms when the bankroll drops back below
        self.today_profile = CASH_L2
        self.today_note = ""

    @property
    def configured(self) -> str:
        p = self.cfg.get("profile", CASH_L2)
        if p not in (CASH_L2, MARGIN_L3):
            raise ValueError(f"unknown profile {p!r}")
        return p

    def start_of_day(self, bankroll: float, today: dt.date) -> str:
        """Decide today's effective profile from the start-of-day bankroll."""
        threshold = float(self.cfg.get("margin_l3_min_bankroll", 2000))
        alert_level = float(self.cfg.get("margin_alert_bankroll", threshold))

        if bankroll >= alert_level and not self.above_alert_level:
            self.above_alert_level = True
            if self.configured == CASH_L2 and self.alerts:
                self.alerts.alert(
                    "PROFILE",
                    f"Bankroll ${bankroll:,.2f} crossed ${alert_level:,.0f}. MARGIN_L3 is now "
                    f"eligible, but the bot stays on CASH_L2 until you change "
                    f"config/account.yaml yourself.", today)
        elif bankroll < alert_level:
            self.above_alert_level = False

        if self.configured == MARGIN_L3 and bankroll >= threshold:
            self.today_profile, self.today_note = MARGIN_L3, ""
        elif self.configured == MARGIN_L3:
            self.today_profile = CASH_L2
            self.today_note = (f"MARGIN_L3 configured but start-of-day bankroll ${bankroll:,.2f} "
                               f"< ${threshold:,.0f}; trading as CASH_L2 today")
        else:
            self.today_profile, self.today_note = CASH_L2, ""
        return self.today_profile

    def allowed_structures(self, profile: str | None = None) -> list[str]:
        return list(self.cfg["profiles"][profile or self.today_profile]["allowed_structures"])

    def settled_cash_only(self, profile: str | None = None) -> bool:
        return bool(self.cfg["profiles"][profile or self.today_profile]["settled_cash_only"])

    def to_dict(self) -> dict:
        return {"above_alert_level": self.above_alert_level}

    def load(self, d: dict) -> None:
        self.above_alert_level = bool(d.get("above_alert_level", False))
