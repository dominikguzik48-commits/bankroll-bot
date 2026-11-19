"""Volatility forecasting for the IV filter.

GARCH(1,1): sigma2[t+1] = omega + alpha * r[t]^2 + beta * sigma2[t].
The daily filter uses a fast numpy recursion (same code in the simulator and live).
`calibrate` fits alpha/beta per symbol with the open-source `arch` package on real
history; without calibration the defaults below are used.

Why: option buyers lose when implied vol (what they pay for) is above the volatility
that actually happens. Academic work (e.g. Goyal & Saretto 2009) finds options with
implied vol far above forecast realized vol tend to do worse. The filter only lets the
bot buy options that are not expensive against the forecast.
"""
from __future__ import annotations

import numpy as np

DEFAULT_ALPHA = 0.08
DEFAULT_BETA = 0.90
TRADING_DAYS = 252


def garch_next_var(returns: np.ndarray, alpha=DEFAULT_ALPHA, beta=DEFAULT_BETA,
                   long_run_var=None) -> tuple[np.ndarray, np.ndarray]:
    """returns: [T, ...] daily log returns. Returns (sigma2_next [T, ...], long_run_var [...]).
    sigma2_next[t] is the forecast for day t+1 using data through day t."""
    r = np.nan_to_num(np.asarray(returns, dtype=float))
    if long_run_var is None:
        long_run_var = np.maximum(r.var(axis=0), 1e-8)
    long_run_var = np.asarray(long_run_var, dtype=float)
    omega = long_run_var * (1 - alpha - beta)
    out = np.empty_like(r)
    s2 = long_run_var.copy()
    for t in range(len(r)):
        s2 = omega + alpha * r[t] ** 2 + beta * s2
        out[t] = s2
    return out, long_run_var


def term_vol(sigma2_next, long_run_var, h_days, alpha=DEFAULT_ALPHA, beta=DEFAULT_BETA):
    """Annualized average volatility expected over the next h trading days."""
    p = alpha + beta
    h = np.maximum(np.asarray(h_days, dtype=float), 1.0)
    avg = long_run_var + (sigma2_next - long_run_var) * (1 - p ** h) / ((1 - p) * h)
    return np.sqrt(np.maximum(avg, 1e-10) * TRADING_DAYS)


def calendar_to_trading_days(cal_days: float) -> float:
    return max(1.0, cal_days * TRADING_DAYS / 365.0)


def forecast_from_closes(closes: list[float], h_days: float, params: dict | None = None) -> float | None:
    """One symbol, live use: annualized vol forecast over h trading days from daily closes."""
    c = np.asarray(closes, dtype=float)
    if len(c) < 30:
        return None
    r = np.diff(np.log(c))
    a = (params or {}).get("alpha", DEFAULT_ALPHA)
    b = (params or {}).get("beta", DEFAULT_BETA)
    lr = (params or {}).get("long_run_var")
    s2, lrv = garch_next_var(r, a, b, lr)
    return float(term_vol(s2[-1], lrv, h_days, a, b))


def calibrate(closes: list[float]) -> dict:
    """Fit GARCH(1,1) with `arch` (Student-t errors). Needs ~250+ daily closes."""
    from arch import arch_model

    r = np.diff(np.log(np.asarray(closes, dtype=float))) * 100
    res = arch_model(r, mean="Constant", vol="GARCH", p=1, q=1, dist="t").fit(disp="off")
    omega, alpha, beta = float(res.params["omega"]), float(res.params["alpha[1]"]), float(res.params["beta[1]"])
    persistence = alpha + beta
    if not (0 < persistence < 0.999):
        raise ValueError(f"GARCH fit not stationary (alpha+beta={persistence:.3f}); keep defaults")
    return {"alpha": alpha, "beta": beta, "long_run_var": omega / (1 - persistence) / 1e4,
            "n_obs": int(len(r))}
