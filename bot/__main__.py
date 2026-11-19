"""CLI: python -m bot <command>"""
from __future__ import annotations

import argparse
import datetime as dt
import sys

from .config import load_config
from .journal import Journal
from .reports import graduation_report


def main(argv=None) -> int:
    p = argparse.ArgumentParser(prog="bot")
    sub = p.add_subparsers(dest="cmd", required=True)

    mc = sub.add_parser("montecarlo", help="run the challenge Monte Carlo")
    mc.add_argument("--runs", type=int, default=5000)
    mc.add_argument("--risk", type=float, nargs="+", default=[10, 25, 50])
    mc.add_argument("--models", nargs="+", default=["regime", "random_walk"])
    mc.add_argument("--seed", type=int, default=7)
    mc.add_argument("--workers", type=int, default=None)
    mc.add_argument("--out", default="reports/montecarlo.md")
    mc.add_argument("--set", nargs="*", default=[], metavar="KEY=VALUE",
                    help="config overrides, e.g. strategy.vol_filter.enabled=true")

    g = sub.add_parser("graduation", help="write reports/graduation.md from the paper journal")
    g.add_argument("--journal", default="state/paper")
    g.add_argument("--out", default="reports/graduation.md")

    sub.add_parser("run-day", help="run today's paper session on live Webull data (needs approved_to_start)")
    sub.add_parser("webull-login", help="store Webull app key/secret in Windows Credential Manager")
    sub.add_parser("webull-probe", help="save raw Webull responses to state/webull_probe (read-only)")
    fh = sub.add_parser("fetch-history", help="save up to ~4.7 years of Webull daily closes to state/history/")
    fh.add_argument("--days", type=int, default=1200)
    tu = sub.add_parser("tune", help="walk-forward parameter tuning on state/history/ (proposes, never applies)")
    tu.add_argument("--trials", type=int, default=40)
    tu.add_argument("--history", default="state/history")
    sub.add_parser("calibrate-vol", help="fit GARCH(1,1) per symbol on Webull daily history -> config/vol_params.yaml")

    args = p.parse_args(argv)
    cfg = load_config()

    if args.cmd == "montecarlo":
        from sim.montecarlo import apply_overrides, run, summarize, write_report
        cfg = apply_overrides(cfg, args.set)
        res = run(cfg, args.runs, args.risk, args.models, seed=args.seed, workers=args.workers)
        summ = {k: summarize(v, cfg.sim["horizon_trading_days"]) for k, v in res["results"].items()}
        out = cfg.path(args.out)
        out.parent.mkdir(parents=True, exist_ok=True)
        write_report(summ, cfg, args.runs, out, res["elapsed_s"])
        print(out.read_text())
        return 0

    if args.cmd == "graduation":
        J = Journal(cfg.path(args.journal))
        md, _ = graduation_report(J, cfg)
        out = cfg.path(args.out)
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(md, encoding="utf-8")
        print(md)
        return 0

    if args.cmd == "run-day":
        from .live import run_day
        print(run_day(cfg))
        return 0

    if args.cmd == "webull-login":
        import getpass

        import keyring

        from .webull_data import KEYRING_SERVICE
        keyring.set_password(KEYRING_SERVICE, "app_key", getpass.getpass("Webull app key: ").strip())
        keyring.set_password(KEYRING_SERVICE, "app_secret", getpass.getpass("Webull app secret: ").strip())
        print("Saved to Windows Credential Manager.")
        return 0

    if args.cmd == "webull-probe":
        from .webull_data import WebullData
        out = cfg.path("state/webull_probe")
        for line in WebullData(cfg.root).probe(out):
            print(line)
        print(f"Raw responses saved in {out}. They contain market data only, no keys.")
        return 0
    if args.cmd == "fetch-history":
        from .tuner import save_history
        from .webull_data import WebullData
        bars = WebullData(cfg.root).daily_bars(list(cfg.strategy["universe"]), count=args.days)
        save_history(cfg.path("state/history"), bars)
        for sym, rows in bars.items():
            print(f"{sym}: {len(rows)} days, {rows[0][0] if rows else '-'} to {rows[-1][0] if rows else '-'}")
        return 0

    if args.cmd == "tune":
        from .tuner import load_history, tune, verdict, write_tuning_report
        dates, closes = load_history(cfg.path(args.history), list(cfg.strategy["universe"]))
        out = tune(cfg, dates, closes, trials=args.trials)
        v = verdict(cfg, out, dates, closes, 110)
        path = cfg.path("reports/tuning.md")
        write_tuning_report(cfg, out, v, path,
                            f"Real Webull daily closes, {dates[0]} to {dates[-1]} ({len(dates)} days). "
                            "Option prices are modeled (Black-Scholes at GARCH vol x premium).")
        print(path.read_text(encoding="utf-8"))
        return 0

    if args.cmd == "calibrate-vol":
        import yaml

        from .volatility import calibrate
        from .webull_data import WebullData
        wb = WebullData(cfg.root)
        bars = wb.daily_bars(list(cfg.strategy["universe"]), count=1200)
        out = {}
        for sym, series in bars.items():
            closes = [c for _, c in series]
            try:
                out[sym] = calibrate(closes)
                print(f"{sym}: alpha {out[sym]['alpha']:.3f} beta {out[sym]['beta']:.3f} "
                      f"long-run vol {(out[sym]['long_run_var'] * 252) ** 0.5:.1%} ({len(closes)} days)")
            except Exception as e:  # noqa: BLE001
                print(f"{sym}: kept defaults ({e})")
        cfg.path("config/vol_params.yaml").write_text(yaml.safe_dump(out), encoding="utf-8")
        return 0
    return 1


if __name__ == "__main__":
    sys.exit(main())
