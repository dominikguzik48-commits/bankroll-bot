"""Synthetic market for the simulator.

Generates correlated underlying paths (market factor with optional bull/bear regimes
and jumps, plus idiosyncratic noise), an implied-vol surface (realized regime vol x a
variance risk premium x AR(1) noise, with put skew), and option quotes with realistic
bid/ask widths. No real price history is used anywhere.
"""
from __future__ import annotations

import datetime as dt
import math

import numpy as np

from bot.models import OptionContract, OptionQuote, Signal
from bot.pricing import bs_price_delta
from bot.strategy import compute_signals
from bot.volatility import calendar_to_trading_days, garch_next_var, term_vol

EXTRA_EXPIRY_DAYS = 45


def _bs_scalar(S, K, T, r, sigma, is_call):
    T = max(T, 1e-6)
    sigma = max(sigma, 1e-4)
    sq = sigma * math.sqrt(T)
    d1 = (math.log(S / K) + (r + 0.5 * sigma * sigma) * T) / sq
    d2 = d1 - sq
    n1 = 0.5 * (1 + math.erf(d1 / math.sqrt(2)))
    n2 = 0.5 * (1 + math.erf(d2 / math.sqrt(2)))
    disc = math.exp(-r * T)
    if is_call:
        return max(S * n1 - K * disc * n2, 0.0), n1
    return max(K * disc * (1 - n2) - S * (1 - n1), 0.0), n1 - 1


class MarketBatch:
    """Paths for many independent runs at once. Arrays are [T, runs, names]."""

    def __init__(self, cfg, model: str, n_runs: int, seed: int):
        sim = cfg.sim
        self.cfg = cfg
        self.names = list(cfg.strategy["universe"])
        self.info = [sim["underlyings"][n] for n in self.names]
        self.r = float(sim["risk_free_rate"])
        self.warmup = int(sim["warmup_days"])
        self.horizon = int(sim["horizon_trading_days"])
        T, R, N = self.warmup + self.horizon, n_runs, len(self.names)
        self.T, self.R, self.N = T, R, N
        rng = np.random.default_rng(seed)

        start = np.datetime64(sim["start_date"], "D")
        offs = np.arange(-self.warmup, self.horizon + EXTRA_EXPIRY_DAYS)
        d64 = np.busday_offset(start, offs, roll="forward")
        self.dates = [d.astype(dt.date) for d in d64]
        self.weekday = np.array([d.weekday() for d in self.dates])
        self.ordinal = np.array([d.toordinal() for d in self.dates])

        m = sim["market_models"][model]
        regs = list(m["regimes"].values())
        mus = np.array([g["mu"] for g in regs])
        sigs = np.array([g["sigma"] for g in regs])
        leave = np.array([g["p_leave"] for g in regs])
        # start in the stationary distribution (two-regime case) or regime 0
        if len(regs) == 2 and leave.sum() > 0:
            p0 = leave[1] / leave.sum()
            state = (rng.random(R) > p0).astype(int)
        else:
            state = np.zeros(R, dtype=int)
        mu = np.empty((T, R))
        sg = np.empty((T, R))
        for t in range(T):
            mu[t], sg[t] = mus[state], sigs[state]
            if len(regs) > 1:
                flip = rng.random(R) < leave[state]
                state = np.where(flip, (state + 1) % len(regs), state)

        dtau = 1 / 252
        z = rng.standard_normal((T, R))
        rm = (mu - 0.5 * sg ** 2) * dtau + sg * math.sqrt(dtau) * z
        if m["jump_prob"] > 0:
            j = rng.random((T, R)) < m["jump_prob"]
            rm += j * rng.normal(m["jump_mean"], m["jump_sd"], (T, R))
        beta = np.array([i["beta"] for i in self.info])
        idio = np.array([i["idio"] for i in self.info])
        zi = rng.standard_normal((T, R, N))
        ret = beta * rm[..., None] + (-0.5 * idio ** 2 * dtau + idio * math.sqrt(dtau) * zi)
        ret[0] = 0.0
        s0 = np.array([i["price"] for i in self.info], dtype=float)
        self.close = s0 * np.exp(np.cumsum(ret, axis=0))

        vol = np.sqrt((beta * sg[..., None]) ** 2 + idio ** 2)
        phi, sd = float(sim["iv_noise_phi"]), float(sim["iv_noise_sd"])
        e = np.zeros((T, R, N))
        eps = rng.standard_normal((T, R, N)) * sd * math.sqrt(1 - phi ** 2)
        for t in range(1, T):
            e[t] = phi * e[t - 1] + eps[t]
        self.atm_iv = vol * float(sim["variance_risk_premium"]) * np.exp(e)
        self._derive()

    @classmethod
    def from_history(cls, cfg, dates: list[dt.date], closes: np.ndarray, warmup: int | None = None):
        """Replay REAL daily closes [T, N] (one run). Option prices are modeled with
        Black-Scholes at IV = GARCH forecast x variance risk premium, since free historical
        option quotes are not available; spreads/fills/fees as in the simulator."""
        self = cls.__new__(cls)
        sim = cfg.sim
        self.cfg = cfg
        self.names = list(cfg.strategy["universe"])
        self.info = [sim["underlyings"][n] for n in self.names]
        self.r = float(sim["risk_free_rate"])
        T, N = closes.shape
        self.warmup = int(warmup if warmup is not None else sim["warmup_days"])
        self.horizon = T - self.warmup
        self.T, self.R, self.N = T, 1, N
        last = np.datetime64(dates[-1], "D")
        extra = [np.busday_offset(last, i, roll="forward").astype(dt.date) for i in range(1, EXTRA_EXPIRY_DAYS + 1)]
        self.dates = list(dates) + extra
        self.weekday = np.array([d.weekday() for d in self.dates])
        self.ordinal = np.array([d.toordinal() for d in self.dates])
        self.close = closes.reshape(T, 1, N).astype(float)
        lr = np.diff(np.log(self.close), axis=0, prepend=np.log(self.close[:1]))
        s2, _ = garch_next_var(lr.reshape(T, N))
        prev = np.vstack([s2[:1], s2[:-1]])   # forecast made with data through yesterday
        self.atm_iv = (np.sqrt(prev * 252) * float(sim["variance_risk_premium"])).reshape(T, 1, N)
        self._derive()
        return self

    def _derive(self):
        cfg, T, R, N = self.cfg, self.T, self.R, self.N
        self.skew = float(cfg.sim["skew"])

        call, put, strength = compute_signals(self.close.reshape(T, R * N), cfg.strategy)
        self.sig_call = call.reshape(T, R, N)
        self.sig_put = put.reshape(T, R, N)
        self.sig_strength = strength.reshape(T, R, N)

        # GARCH(1,1) forecast from returns known so far (long-run var from the warmup window)
        lr = np.diff(np.log(self.close), axis=0, prepend=np.log(self.close[:1]))
        flat = lr.reshape(T, R * N)
        lrv = np.maximum(flat[1:max(self.warmup, 3)].var(axis=0), 1e-8)
        s2, _ = garch_next_var(flat, long_run_var=lrv)
        self.garch_s2 = s2.reshape(T, R, N)
        self.garch_lrv = lrv.reshape(R, N)

        # synthetic blackout days (~19/yr, stand-ins for FOMC/CPI)
        self.blackout_dates = {self.dates[i] for i in range(T) if i % 13 == 6}
        self.spy_idx = self.names.index("SPY") if "SPY" in self.names else None

    def iv_at(self, t, r, n, K):
        S = self.close[t, r, n]
        return self.atm_iv[t, r, n] * max(0.5, 1 + self.skew * math.log(S / K))

    def snapshot(self, r: int, t: int) -> "SimSnapshot":
        return SimSnapshot(self, r, t)


class SimSnapshot:
    def __init__(self, b: MarketBatch, r: int, t: int):
        self.b, self.r, self.t = b, r, t
        self.date = b.dates[t]
        self.now = dt.datetime.combine(self.date, dt.time(15, 45))
        self.spy_close = float(b.close[t, r, b.spy_idx]) if b.spy_idx is not None else None
        self._cache: dict[str, OptionQuote] = {}

    def vol_forecast(self, symbol: str, cal_days: int) -> float:
        b, n = self.b, self.b.names.index(symbol)
        return float(term_vol(b.garch_s2[self.t - 1, self.r, n], b.garch_lrv[self.r, n],
                              calendar_to_trading_days(cal_days)))

    # signals known at the prior close; trade today
    def signals(self) -> list[Signal]:
        b, t, r = self.b, self.t - 1, self.r
        out = []
        for n, name in enumerate(b.names):
            if b.sig_call[t, r, n]:
                out.append(Signal(name, "call", float(b.sig_strength[t, r, n])))
            elif b.sig_put[t, r, n]:
                out.append(Signal(name, "put", float(b.sig_strength[t, r, n])))
        return out

    def _make_quote(self, contract, n, mid, delta, iv, S):
        info = self.b.info[n]
        width = max(info["min_spread"], info["spread_pct"] * mid)
        bid = max(0.0, math.floor((mid - width / 2) * 100 + 1e-9) / 100)
        ask = math.ceil((mid + width / 2) * 100 - 1e-9) / 100
        if ask <= bid:
            ask = bid + 0.01
        mny = abs(math.log(contract.strike / S))
        oi = int(20000 * math.exp(-15 * mny)) if info["expiries"] == "daily" else int(8000 * math.exp(-12 * mny))
        return OptionQuote(contract, bid, ask, float(delta), float(iv), oi, max(oi // 8, 0), self.now, S)

    def chain(self, symbol: str, side: str) -> list[OptionQuote]:
        b, t, r = self.b, self.t, self.r
        n = b.names.index(symbol)
        info = b.info[n]
        S = float(b.close[t, r, n])
        c = b.cfg.strategy["contract"]
        today_ord = b.ordinal[t]
        idx = np.arange(t + 1, min(t + 60, len(b.dates)))
        dte = b.ordinal[idx] - today_ord
        ok = (dte >= c["min_dte"]) & (dte <= c["max_dte"])
        if info["expiries"] != "daily":
            ok &= b.weekday[idx] == 4
        idx, dte = idx[ok], dte[ok]
        if len(idx) == 0:
            return []
        step = info["strike_step"]
        lo, hi = (S * 0.97, S * 1.15) if side == "call" else (S * 0.85, S * 1.03)
        strikes = np.arange(math.ceil(lo / step) * step, hi + 1e-9, step)
        if len(strikes) == 0:
            return []
        KK, DD = np.meshgrid(strikes, dte)
        K, D = KK.ravel(), DD.ravel()
        E = np.repeat(idx, len(strikes))
        iv = b.atm_iv[t, r, n] * np.maximum(0.5, 1 + b.skew * np.log(S / K))
        is_call = side == "call"
        price, delta = bs_price_delta(S, K, D / 365.0, b.r, iv, is_call)
        right = "C" if is_call else "P"
        out = []
        for i in range(len(K)):
            ad = abs(delta[i])
            if ad < c["min_delta"] - 0.05 or ad > c["max_delta"] + 0.05:
                continue
            k = OptionContract(symbol, b.dates[E[i]], float(round(K[i], 2)), right)
            q = self._make_quote(k, n, float(price[i]), delta[i], iv[i], S)
            self._cache[k.symbol] = q
            out.append(q)
        return out

    def quote(self, contract: OptionContract) -> OptionQuote | None:
        q = self._cache.get(contract.symbol)
        if q is not None:
            return q
        b, t, r = self.b, self.t, self.r
        n = b.names.index(contract.underlying)
        S = float(b.close[t, r, n])
        days = (contract.expiry - self.date).days
        iv = b.iv_at(t, r, n, contract.strike)
        if days <= 0:
            mid = max(S - contract.strike, 0) if contract.right == "C" else max(contract.strike - S, 0)
            delta = 0.0
        else:
            mid, delta = _bs_scalar(S, contract.strike, days / 365.0, b.r, iv, contract.right == "C")
        q = self._make_quote(contract, n, mid, delta, iv, S)
        self._cache[contract.symbol] = q
        return q
