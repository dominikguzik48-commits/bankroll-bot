# Bankroll Challenge: Monte Carlo

5,000 runs per row. Start $1,000, target $5,000, ruin floor $200, profile CASH_L2 (long calls/puts only), max 1 position, milestones [1500, 2000, 2500] stepping risk down to 10%. Horizon 756 trading days (3 years); runs still going at the end are 'unresolved'.

**Synthetic market.** No real price history was used. Fills at mid + 75% of the half-spread + $0.01, fees $0.65/contract each side, IV = realized vol x 1.1 (option buyers pay a volatility premium), daily bars (exits checked at the close, so gaps go through stops).

- `regime`: bull/bear regimes that persist, so trends are real and the signal can have an edge.
- `random_walk`: no trend persistence, so the signal has no edge by construction.

| Market | Risk/trade | P(hit $5,000) | P(ruin) | Unresolved after 3y | Median trades to finish | Median days to finish | Median trades to target | Median days to target | Trades/yr | Win rate | Avg P&L/trade | Median final (all) |
|---|---|---|---|---|---|---|---|---|---|---|---|---|
| regime | 10% | 0.0% | 0.0% | 100.0% | nan | nan | nan | nan | 15 | 35.9% | $-7.70 | $603 |
| regime | 25% | 0.4% | 60.7% | 39.0% | 31 | 434 | 62 | 509 | 20 | 35.9% | $-12.89 | $198 |
| regime | 50% | 1.1% | 78.6% | 20.3% | 13 | 126 | 40 | 256 | 26 | 35.8% | $-18.27 | $191 |
| random_walk | 10% | 0.0% | 0.0% | 100.0% | nan | nan | nan | nan | 15 | 35.4% | $-8.43 | $582 |
| random_walk | 25% | 0.1% | 60.9% | 38.9% | 32 | 432 | 52 | 347 | 19 | 35.6% | $-14.60 | $198 |
| random_walk | 50% | 0.5% | 79.9% | 19.6% | 13 | 124 | 40 | 278 | 25 | 35.6% | $-20.59 | $191 |

Checks: risk-rule violations 0, reconciliation errors 0, other halts 0.000.

Elapsed 1995s.
