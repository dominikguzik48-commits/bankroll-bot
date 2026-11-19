# Bankroll Challenge: Monte Carlo

5,000 runs per row. Start $1,000, target $5,000, ruin floor $200, profile CASH_L2 (long calls/puts only), max 1 position, milestones [1500, 2000, 2500] stepping risk down to 10%. Horizon 756 trading days (3 years); runs still going at the end are 'unresolved'.

**Synthetic market.** No real price history was used. Fills at mid + 75% of the half-spread + $0.01, fees $0.65/contract each side, IV = realized vol x 1.1 (option buyers pay a volatility premium), daily bars (exits checked at the close, so gaps go through stops).

- `regime`: bull/bear regimes that persist, so trends are real and the signal can have an edge.
- `random_walk`: no trend persistence, so the signal has no edge by construction.

| Market | Risk/trade | P(hit $5,000) | P(ruin) | Unresolved after 3y | Median trades to finish | Median days to finish | Median trades to target | Median days to target | Trades/yr | Win rate | Avg P&L/trade | Median final (all) |
|---|---|---|---|---|---|---|---|---|---|---|---|---|
| regime | 25% | 0.4% | 14.2% | 85.4% | 23 | 562 | 35 | 488 | 10 | 37.3% | $-9.71 | $456 |
| random_walk | 25% | 0.2% | 5.1% | 94.8% | 19 | 617 | 38 | 466 | 8 | 37.2% | $-12.54 | $529 |

Checks: risk-rule violations 0, reconciliation errors 0, other halts 0.000.

Elapsed 1146s.
