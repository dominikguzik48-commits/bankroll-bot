"""Read-only Webull market data via the official SDK (`webull-openapi-python-sdk`).

This module NEVER places orders. It only reads bars, option chains, option snapshots,
news and calendars.

Credentials stay on your machine: Windows Credential Manager (via `keyring`) or the
environment variables WEBULL_APP_KEY / WEBULL_APP_SECRET. Never commit them.

Webull's response field names are not documented in the SDK, so parsing goes through
`_pick`, which tries the likely names. Run `python -m bot webull-probe` once: it saves the
raw responses to state/webull_probe/ so the field mapping can be checked against them.
"""
from __future__ import annotations

import datetime as dt
import json
import os
import re
from pathlib import Path

from .models import OptionContract, OptionQuote
from .pricing import bs_price_delta, implied_vol

KEYRING_SERVICE = "bankroll-bot-webull"
OCC_RE = re.compile(r"^([A-Z.]{1,6})(\d{6})([CP])(\d{8})$")


# ---------------------------------------------------------------------- helpers
def occ_symbol(c: OptionContract) -> str:
    return f"{c.underlying}{c.expiry:%y%m%d}{c.right}{int(round(c.strike * 1000)):08d}"


def parse_occ(sym: str) -> OptionContract | None:
    m = OCC_RE.match(sym.replace(" ", ""))
    if not m:
        return None
    und, ymd, right, k = m.groups()
    return OptionContract(und, dt.datetime.strptime(ymd, "%y%m%d").date(), int(k) / 1000, right)


def _pick(d, *keys, default=None):
    """First present key, searched at the top level then one level down."""
    if not isinstance(d, dict):
        return default
    for k in keys:
        if k in d and d[k] not in (None, ""):
            return d[k]
    for v in d.values():
        if isinstance(v, dict):
            for k in keys:
                if k in v and v[k] not in (None, ""):
                    return v[k]
    return default


def _num(x, default=None):
    if isinstance(x, list) and x:
        x = x[0].get("price") if isinstance(x[0], dict) else x[0]
    try:
        return float(x)
    except (TypeError, ValueError):
        return default


def _rows(payload) -> list[dict]:
    if isinstance(payload, list):
        return payload
    if isinstance(payload, dict):
        for k in ("data", "list", "items", "result", "contracts", "bars"):
            v = payload.get(k)
            if isinstance(v, list):
                return v
            if isinstance(v, dict):
                inner = _rows(v)
                if inner:
                    return inner
    return []


def _ts(x) -> dt.datetime | None:
    if x is None:
        return None
    try:
        v = float(x)
        return dt.datetime.fromtimestamp(v / 1000 if v > 1e11 else v)
    except (TypeError, ValueError):
        pass
    try:
        return dt.datetime.fromisoformat(str(x).replace("Z", "+00:00")).astimezone().replace(tzinfo=None)
    except ValueError:
        return None


def load_credentials() -> tuple[str, str]:
    key = os.environ.get("WEBULL_APP_KEY")
    secret = os.environ.get("WEBULL_APP_SECRET")
    if key and secret:
        return key, secret
    try:
        import keyring
        key = keyring.get_password(KEYRING_SERVICE, "app_key")
        secret = keyring.get_password(KEYRING_SERVICE, "app_secret")
    except Exception:
        key = secret = None
    if not (key and secret):
        raise RuntimeError("No Webull credentials. Run `python -m bot webull-login` (stores them in "
                           "Windows Credential Manager) or set WEBULL_APP_KEY / WEBULL_APP_SECRET.")
    return key, secret


# ---------------------------------------------------------------------- client
class WebullData:
    def __init__(self, root: Path, region: str = "us", r: float = 0.04):
        from webull.core.client import ApiClient
        from webull.data.data_client import DataClient

        key, secret = load_credentials()
        self.api = ApiClient(key, secret, region)
        token_dir = root / "state" / "webull_token"
        token_dir.mkdir(parents=True, exist_ok=True)
        if hasattr(self.api, "set_token_dir"):
            self.api.set_token_dir(str(token_dir))
        self.data = DataClient(self.api)
        self.r = r

    @staticmethod
    def _json(res):
        if getattr(res, "status_code", 200) != 200:
            raise RuntimeError(f"Webull HTTP {res.status_code}: {getattr(res, 'text', '')[:300]}")
        return res.json()

    # --- stocks -----------------------------------------------------------------
    def daily_bars(self, symbols: list[str], count: int = 150) -> dict[str, list[tuple[dt.date, float]]]:
        res = self.data.market_data.get_batch_history_bar(",".join(symbols), "US_STOCK", "D", count=str(count))
        payload = self._json(res)
        out: dict[str, list] = {}
        groups = _rows(payload)
        for g in groups:
            sym = _pick(g, "symbol", "ticker")
            bars = _rows(g) or g.get("result") or []
            series = []
            for b in bars:
                t = _ts(_pick(b, "time", "timestamp", "trade_time", "date"))
                c = _num(_pick(b, "close", "c"))
                if t and c:
                    series.append((t.date(), c))
            if sym:
                out[sym] = sorted(series)
        return out

    def last_prices(self, symbols: list[str]) -> dict[str, float]:
        payload = self._json(self.data.market_data.get_snapshot(",".join(symbols), "US_STOCK"))
        out = {}
        for row in _rows(payload):
            sym = _pick(row, "symbol", "ticker")
            px = _num(_pick(row, "price", "last_price", "close", "last"))
            if sym and px:
                out[sym] = px
        return out

    # --- options ----------------------------------------------------------------
    def option_contracts(self, underlying: str, right: str, k_lo: float, k_hi: float,
                         exp_lo: dt.date, exp_hi: dt.date) -> list[OptionContract]:
        out, key = [], None
        for _ in range(20):  # pagination guard
            res = self.data.instrument.list_option_contracts(
                underlying_symbols=underlying, option_type="CALL" if right == "C" else "PUT",
                strike_price_gte=str(k_lo), strike_price_lte=str(k_hi), pagination_key=key)
            payload = self._json(res)
            for row in _rows(payload):
                sym = _pick(row, "symbol", "option_symbol", "occ_symbol")
                c = parse_occ(str(sym)) if sym else None
                if c and exp_lo <= c.expiry <= exp_hi:
                    out.append(c)
            key = _pick(payload, "pagination_key", "next_pagination_key") if isinstance(payload, dict) else None
            if not key:
                break
        return out

    def option_quotes(self, contracts: list[OptionContract], S: float, now: dt.datetime) -> list[OptionQuote]:
        by_sym = {occ_symbol(c): c for c in contracts}
        syms = list(by_sym)
        quotes = []
        for i in range(0, len(syms), 20):
            payload = self._json(self.data.option_market_data.get_option_snapshot(",".join(syms[i:i + 20]), "US_OPTION"))
            for row in _rows(payload):
                c = by_sym.get(str(_pick(row, "symbol", "option_symbol")))
                if c is None:
                    continue
                bid = _num(_pick(row, "bid", "bid_price", "bids"), 0.0)
                ask = _num(_pick(row, "ask", "ask_price", "asks"), 0.0)
                if ask <= 0 or ask < bid:
                    continue
                ts = _ts(_pick(row, "quote_time", "trade_time", "timestamp", "time")) or now
                T = max((c.expiry - now.date()).days, 0) / 365 + 1e-6
                delta = _num(_pick(row, "delta"))
                iv = _num(_pick(row, "implied_volatility", "iv", "impVol"))
                if iv and iv > 3:   # percent -> fraction
                    iv /= 100
                if delta is None:
                    iv = iv or implied_vol((bid + ask) / 2, S, c.strike, T, self.r, c.right == "C")
                    if iv is None:
                        continue
                    _, d = bs_price_delta(S, c.strike, T, self.r, iv, c.right == "C")
                    delta = float(d)
                quotes.append(OptionQuote(
                    c, bid, ask, delta, iv or 0.0,
                    int(_num(_pick(row, "open_interest", "openInterest", "oi"), 0)),
                    int(_num(_pick(row, "volume", "vol"), 0)), ts, S))
        return quotes

    # --- news / calendars (read only; used by later filters) --------------------------
    def stock_news(self, symbol: str):
        return self._json(self.data.news.get_stock_news(symbol, "US_STOCK"))

    def economic_calendar(self, start: dt.date, end: dt.date):
        return self._json(self.data.fundamentals.get_economic_calendars("US", start.isoformat(), end.isoformat()))

    def earnings_calendar(self, symbol: str):
        return self._json(self.data.fundamentals.get_earnings_calendar(symbol, "US_STOCK"))

    # --- probe ---------------------------------------------------------------------
    def probe(self, out_dir: Path, symbol: str = "SPY") -> list[str]:
        """Save raw responses from every endpoint the bot uses. Read-only."""
        out_dir.mkdir(parents=True, exist_ok=True)
        today = dt.date.today()
        log = []

        def save(name, fn):
            try:
                res = fn()
                body = res.json() if hasattr(res, "json") else res
                (out_dir / f"{name}.json").write_text(json.dumps(body, indent=2, default=str)[:400_000])
                log.append(f"OK    {name}  (HTTP {getattr(res, 'status_code', '?')})")
            except Exception as e:  # noqa: BLE001
                log.append(f"FAIL  {name}: {type(e).__name__}: {e}")

        d = self.data
        save("stock_snapshot", lambda: d.market_data.get_snapshot(symbol, "US_STOCK"))
        save("stock_daily_bars", lambda: d.market_data.get_batch_history_bar(f"{symbol},IWM", "US_STOCK", "D", count="5"))
        save("option_contracts", lambda: d.instrument.list_option_contracts(underlying_symbols=symbol, option_type="CALL"))
        try:
            rows = _rows(json.loads((out_dir / "option_contracts.json").read_text()))
            first = next((str(_pick(r, "symbol", "option_symbol")) for r in rows if _pick(r, "symbol", "option_symbol")), None)
        except Exception:  # noqa: BLE001
            first = None
        if first:
            save("option_snapshot", lambda: d.option_market_data.get_option_snapshot(first, "US_OPTION"))
        save("stock_news", lambda: d.news.get_stock_news(symbol, "US_STOCK"))
        save("economic_calendar", lambda: d.fundamentals.get_economic_calendars("US", today.isoformat(),
                                                                                (today + dt.timedelta(days=30)).isoformat()))
        save("earnings_calendar", lambda: d.fundamentals.get_earnings_calendar("AAPL", "US_STOCK"))
        (out_dir / "probe_log.txt").write_text("\n".join(log) + "\n")
        return log
