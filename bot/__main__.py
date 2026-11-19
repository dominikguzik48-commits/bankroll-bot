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

    g = sub.add_parser("graduation", help="write reports/graduation.md from the paper journal")
    g.add_argument("--journal", default="state/paper")
    g.add_argument("--out", default="reports/graduation.md")

    sub.add_parser("run-day", help="run today's paper session on live Webull data (needs approved_to_start)")
    sub.add_parser("webull-login", help="store Webull app key/secret in Windows Credential Manager")
    sub.add_parser("webull-probe", help="save raw Webull responses to state/webull_probe (read-only)")

    args = p.parse_args(argv)
    cfg = load_config()

    if args.cmd == "montecarlo":
        from sim.montecarlo import run, summarize, write_report
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
    return 1


if __name__ == "__main__":
    sys.exit(main())
