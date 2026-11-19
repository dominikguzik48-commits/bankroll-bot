"""Black-Scholes pricing (numpy, no scipy)."""
from __future__ import annotations

import numpy as np


def _erf(x):
    # Abramowitz & Stegun 7.1.26, |error| < 1.5e-7
    x = np.asarray(x, dtype=float)
    s = np.sign(x)
    a = np.abs(x)
    t = 1.0 / (1.0 + 0.3275911 * a)
    y = 1.0 - (((((1.061405429 * t - 1.453152027) * t) + 1.421413741) * t
                - 0.284496736) * t + 0.254829592) * t * np.exp(-a * a)
    return s * y


def norm_cdf(x):
    return 0.5 * (1.0 + _erf(np.asarray(x, dtype=float) / np.sqrt(2.0)))


def bs_price_delta(S, K, T, r, sigma, is_call):
    """Vectorised Black-Scholes. T in years. Returns (price, delta)."""
    S = np.asarray(S, dtype=float)
    K = np.asarray(K, dtype=float)
    T = np.maximum(np.asarray(T, dtype=float), 1e-6)
    sigma = np.maximum(np.asarray(sigma, dtype=float), 1e-4)
    is_call = np.asarray(is_call, dtype=bool)
    sq = sigma * np.sqrt(T)
    d1 = (np.log(S / K) + (r + 0.5 * sigma * sigma) * T) / sq
    d2 = d1 - sq
    disc = np.exp(-r * T)
    n1, n2 = norm_cdf(d1), norm_cdf(d2)
    call = S * n1 - K * disc * n2
    put = K * disc * (1 - n2) - S * (1 - n1)
    price = np.where(is_call, call, put)
    delta = np.where(is_call, n1, n1 - 1.0)
    return np.maximum(price, 0.0), delta


def implied_vol(price, S, K, T, r, is_call, lo=0.01, hi=5.0, iters=60):
    """Bisection IV solve for one option. Returns None if price is outside model bounds."""
    p_lo, _ = bs_price_delta(S, K, T, r, lo, is_call)
    p_hi, _ = bs_price_delta(S, K, T, r, hi, is_call)
    if not (float(p_lo) <= price <= float(p_hi)):
        return None
    for _ in range(iters):
        mid = 0.5 * (lo + hi)
        p, _ = bs_price_delta(S, K, T, r, mid, is_call)
        if float(p) < price:
            lo = mid
        else:
            hi = mid
    return 0.5 * (lo + hi)
