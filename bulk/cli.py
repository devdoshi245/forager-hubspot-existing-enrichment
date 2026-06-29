"""
bulk.cli — command-line entrypoint for the backfill tool.

Usage (nothing spends credits unless you add --execute):

  # Free dry-run: count records + project cost. Run this first; show it to the client.
  python -m bulk.cli estimate                 # both companies and contacts
  python -m bulk.cli estimate companies
  python -m bulk.cli estimate contacts

  # Backfill companies (firmographics + Claude scoring + Tier). ~0 Forager credits.
  python -m bulk.cli run companies                         # dry-run (default)
  python -m bulk.cli run companies --execute --max-records 50

  # Backfill contacts: Forager reveal (WF2) then Deepline (WF3). ~25 Forager credits each.
  python -m bulk.cli run contacts                          # dry-run (default)
  python -m bulk.cli run contacts --execute --max-records 5     # recommended FIRST test
  python -m bulk.cli run contacts --execute --max-credits 5000
  python -m bulk.cli run contacts --execute --max-records 100 --sleep 0.5

Safety: --execute is required to spend anything. --max-records / --max-credits cap
the run. A Forager 402 (out of credits) stops the run automatically. Re-running
resumes where you left off (already-enriched records are skipped).
"""

import argparse
import json
import logging
import os
import sys

from dotenv import load_dotenv

load_dotenv()

# Importing the bulk package puts core/ on sys.path (see bulk/__init__.py).
from bulk import estimate as estimate_mod  # noqa: E402
from bulk import runner  # noqa: E402

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s - %(message)s")
logger = logging.getLogger("bulk")

_REQUIRED_ENV = ("FORAGER_API_KEY", "FORAGER_ACCOUNT_ID", "HUBSPOT_TOKEN")


def _check_env() -> None:
    missing = [n for n in _REQUIRED_ENV if not os.environ.get(n)]
    if missing:
        logger.error("MISSING REQUIRED ENV VARS: %s — set them before running.", ", ".join(missing))
        sys.exit(2)


def _cmd_estimate(args) -> None:
    _check_env()
    if args.object == "companies":
        result = {"companies": estimate_mod.estimate_companies()}
    elif args.object == "contacts":
        result = {"contacts": estimate_mod.estimate_contacts()}
    else:
        result = estimate_mod.estimate_all()
    # When estimating "all", also print the paste-ready client summary.
    if args.object == "all":
        print(estimate_mod.format_summary(result))
        print()
    print(json.dumps(result, indent=2))


def _cmd_run(args) -> None:
    _check_env()
    if not args.execute:
        logger.warning("DRY-RUN: no credits will be spent. Add --execute to actually enrich.")
    if args.object == "companies":
        result = runner.run_companies(
            execute=args.execute, max_records=args.max_records, sleep=args.sleep)
    else:
        result = runner.run_contacts(
            execute=args.execute, max_records=args.max_records,
            max_credits=args.max_credits, sleep=args.sleep)
    print(json.dumps(result, indent=2))


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="bulk", description="Backfill enrichment for existing HubSpot records.")
    sub = parser.add_subparsers(dest="command", required=True)

    est = sub.add_parser("estimate", help="DRY-RUN: count records + project cost. No credits spent.")
    est.add_argument("object", nargs="?", default="all", choices=["all", "companies", "contacts"])
    est.set_defaults(func=_cmd_estimate)

    run = sub.add_parser("run", help="Backfill records. Dry-run unless --execute.")
    run.add_argument("object", choices=["companies", "contacts"])
    run.add_argument("--execute", action="store_true", help="Actually enrich (spends credits). Default: dry-run.")
    run.add_argument("--max-records", type=int, default=None, help="Stop after this many records.")
    run.add_argument("--max-credits", type=int, default=None, help="Stop before estimated Forager credits exceed this (contacts).")
    run.add_argument("--sleep", type=float, default=0.0, help="Seconds to pause between records (throttle).")
    run.set_defaults(func=_cmd_run)
    return parser


def main(argv=None) -> None:
    args = build_parser().parse_args(argv)
    args.func(args)


if __name__ == "__main__":
    main()
