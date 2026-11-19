# Bankroll Challenge bot

An options bot with a virtual challenge bankroll, account-profile permissions that mirror
Webull's option levels, a risk manager that approves every opening order, a synthetic-market
Monte Carlo and a weekly graduation report.

**Status:** the simulator, tests and Webull market-data adapter are in place. Webull is
used for market data only; paper trades fill inside the bot at live Webull bid/ask
(`bot/live.py`). The challenge does not start until `approved_to_start: true` is set in
`config/challenge.yaml`. Nothing here can place a live order.

## Layout

| Path | What |
|---|---|
| `config/challenge.yaml` | $1,000 start, $5,000 target, $200 ruin floor, 25% risk/trade, 1 position, milestones |
| `config/account.yaml` | `profile: CASH_L2` or `MARGIN_L3`. Only you change it. |
| `config/risk.yaml` | risk manager limits: liquidity, stale data, blackout dates, kill switch, fees |
| `config/strategy.yaml` | 20/50 SMA + RSI signal, 7–30 DTE, delta 0.30–0.50, +50% / −40% / 3 DTE exits |
| `config/sim.yaml` | synthetic market, fill model, horizon |
| `bot/risk.py` | the risk manager |
| `bot/challenge.py` | target / ruin floor / milestones / risk step-down |
| `bot/profile.py` | CASH_L2 vs MARGIN_L3 and the $2,000 alert |
| `bot/ledger.py` | virtual bankroll with settled and unsettled cash (T+1) |
| `bot/engine.py` | the daily loop (same code for the simulator and paper trading) |
| `bot/reports.py` | `challenge_complete.md`, `challenge_failed.md`, `graduation.md` |
| `bot/calendar.py` | NYSE calendar: holidays, 1 PM early closes, T+1 settlement days |
| `bot/volatility.py` | GARCH(1,1) volatility forecast and the IV filter |
| `bot/tuner.py` | walk-forward tuning with guardrails; proposes, never applies |
| `sim/` | synthetic market, history replay and Monte Carlo |

## Commands

```bash
python3 -m pytest -q
python3 -m bot montecarlo --runs 5000 --risk 10 25 50
python3 -m bot graduation          # reads state/paper/, writes reports/graduation.md
python3 -m bot montecarlo --runs 2000 --risk 25 --set strategy.vol_filter.enabled=true
python3 -m bot fetch-history       # Webull daily closes -> state/history/
python3 -m bot calibrate-vol       # GARCH(1,1) per symbol (arch) -> config/vol_params.yaml
python3 -m bot tune                # walk-forward tuning (optuna) -> reports/tuning.md, proposal only
```

The daily run writes the graduation report every Friday. Windows scheduling is in `WINDOWS.md`.

## Open-source pieces

| Package | License | Used for |
|---|---|---|
| `pandas_market_calendars` | MIT | NYSE holidays and early closes |
| `arch` | NCSA | GARCH(1,1) fits for the volatility forecast |
| `optuna` | MIT | parameter search inside the walk-forward tuner |
| `webull-openapi-python-sdk` | Apache-2.0 | Webull market data (read-only) |

## Rules as implemented

- **Bankroll** = challenge cash + liquidation value of the bot's open positions (at the bid,
  after exit fees). It never looks at the broker account balance.
- **Risk manager** checks every opening order for: halt state, kill switch
  (`state/KILL` exists → flatten and stop), reconciliation, blackout date, valid structure,
  profile permission, **no naked shorts (always)**, spread width rules, per-leg liquidity
  and quote age, max open positions, max loss ≤ risk % of bankroll (fees included),
  settled cash. Closing orders are never blocked.
- **CASH_L2**: long calls/puts only, paid from settled cash; sale proceeds settle T+1.
- **MARGIN_L3**: only if you set it *and* the start-of-day bankroll is ≥ $2,000. Crossing
  $2,000 triggers an alert. The bot never switches profiles itself.
- **Challenge**: at $5,000 close all, halt, write `reports/challenge_complete.md`, wait.
  Below $200 close all, halt, write `reports/challenge_failed.md`. Milestones at
  $1,500 / $2,000 / $2,500 alert and step risk 25% → 20% → 15% → 10% (ratchet).
- **No fit → no trade.** If no contract in the DTE/delta window fits the budget, nothing is bought.
- Reconciliation mismatch or a risk-rule violation halts trading until you clear it.

## Not done yet

- Confirm Webull's response fields from `webull-probe` output.
- A strategy for MARGIN_L3 spreads. The risk rules for verticals and condors exist; nothing generates them.
- Real blackout dates in `config/risk.yaml` (empty now); the Webull economic calendar can fill these once its format is confirmed.
- The volatility filter is off by default until results show it helps.
- Push alerts: `Alerts.send` is a no-op; alerts go to `logs/alerts.log`.
