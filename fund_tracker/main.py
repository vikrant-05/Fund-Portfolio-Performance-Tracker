"""
main.py
-------
Orchestrates the full pipeline:

    Read inputs -> Validate -> ISIN->Ticker->Sector -> Performance ->
    Attribution -> Rebalancing (auto, vs previous month) -> Fund_Report_<code>.xlsx

Funds are never hardcoded: every unique Fund Code found across all
configured Weightage files is processed automatically and gets its own
report.

File locations (Weightage, Daily NAV, NSE/BSE Security Master) are never
hardcoded either - they come from config.py / file_manager.py. A firm can
add another fund at any time with --add-fund (see below) instead of
maintaining one giant Weightage/NAV file; every fund across every
configured file is picked up automatically.

Rebalancing
------------
There's no manager-entered target weight. A holding is flagged when its
Current Weight has drifted from its Previous month-end Weight (same ISIN,
prior Weightage snapshot) by more than --threshold percentage points - see
rebalance.compute_weight_drift().

Speed note
----------
Sector/Industry lookup (yahoo_fetch) and stock-return lookup for
attribution (attribution.py) are independent Yahoo Finance calls - the
second doesn't need the first to finish. So the sector lookup for every
ISIN across every fund is kicked off once, in the background, right after
input files are loaded, and each fund's attribution price-fetch runs
concurrently with it rather than waiting for it - the two network-bound
stages overlap instead of running one after the other. Sector data is
only actually awaited right before it's merged into that fund's holdings,
by which point the background fetch has usually already finished.

For fast, iterative runs where fresh sector data doesn't matter (e.g. while
tweaking the rebalance threshold), pass --no-sector-fetch to skip the
network call entirely and read whatever's already cached (anything not
cached shows as "Unknown" - see warm_cache.py for a way to pre-populate the
cache offline so this is rarely a tradeoff).

Usage
-----
    python main.py                          # process every configured fund
    python main.py --threshold 5             # use a 5-point drift threshold
    python main.py --as-of 2026-06           # use the June-2026 month-end Weightage snapshot
                                              #   instead of the latest one (each fund carries
                                              #   one row per holding per month since inception)
    python main.py --no-sector-fetch         # skip live Yahoo sector/industry calls entirely;
                                              #   use cache only, "Unknown" for anything uncached

Adding a fund (all optional - omit and you'll be prompted with a file
picker the first time a file is needed):
    python main.py --add-fund path/to/NewFund_Weightage.xlsx path/to/NewFund_NAV.xlsx
    python main.py --nse-master path/to/NSE_Security_Master.csv
    python main.py --bse-master path/to/BSE_Security_Master.csv
    python main.py --update-security-masters   # force re-selection of both master files
"""

import argparse
from concurrent.futures import Future, ThreadPoolExecutor

import attribution
import config
import data_loader
import file_manager
import performance
import rebalance
import report_generator
import yahoo_fetch


def parse_args():
    p = argparse.ArgumentParser(description="Fund/Portfolio Performance Tracker")
    p.add_argument("--threshold", type=float, default=config.DEFAULT_REBALANCE_THRESHOLD,
                    help="Absolute drift (percentage points), vs the previous month-end "
                         "snapshot, above which a holding is flagged.")
    p.add_argument("--no-cache", action="store_true",
                    help="Ignore the local sector cache and re-fetch from Yahoo Finance.")
    p.add_argument("--no-sector-fetch", action="store_true",
                    help="Skip live Yahoo Finance sector/industry calls entirely; use only "
                         "what's already cached (anything uncached shows as 'Unknown'). "
                         "For fast iteration - see warm_cache.py to pre-populate the cache.")
    p.add_argument("--as-of", type=str, default=None,
                    help="Weightage file month-end snapshot to use (e.g. '2026-06'). "
                         "Defaults to the most recent month-end available per fund.")

    # --- file management overrides (skip the file picker, e.g. for a server) ---
    p.add_argument("--add-fund", nargs=2, metavar=("WEIGHTAGE_PATH", "NAV_PATH"), default=None,
                    help="Register another fund by supplying its Weightage and Daily NAV "
                         "Excel files (same column headers as data_loader.py expects). Added "
                         "alongside whatever's already configured, and saved to config.json "
                         "for future runs.")
    p.add_argument("--remove-fund", nargs=2, metavar=("WEIGHTAGE_PATH", "NAV_PATH"), default=None,
                    help="Un-register a previously-added fund by its Weightage and Daily NAV "
                         "file paths (the inverse of --add-fund). Neither file is deleted from "
                         "disk - they're only removed from config.json, so the app stops "
                         "loading them on future runs.")
    p.add_argument("--nse-master", type=str, default=None,
                    help="Explicit path to the NSE Security Master file (also saved to config.json).")
    p.add_argument("--bse-master", type=str, default=None,
                    help="Explicit path to the BSE Security Master file (also saved to config.json).")
    p.add_argument("--update-security-masters", action="store_true",
                    help="Force re-selection of the NSE and BSE Security Master files via the "
                         "file picker (normally only prompted for once, on first run).")
    return p.parse_args()


def apply_file_overrides(args) -> None:
    """Push any --add-fund/--nse-master/--bse-master CLI args straight into
    config.json via file_manager, so the rest of the pipeline picks them up
    without ever seeing a file dialog."""
    manager = file_manager.get_manager()

    if args.add_fund:
        weightage_path, nav_path = args.add_fund
        problems = (
            data_loader.validate_weightage_file(weightage_path)
            + data_loader.validate_nav_file(nav_path)
        )
        if problems:
            raise SystemExit(
                "Cannot add fund - the supplied file(s) don't match the expected format:\n"
                + "\n".join(f"  - {msg}" for msg in problems)
            )
        w, n = config.add_fund_files(weightage_path, nav_path)
        print(f"  Fund files added -> Weightage: {w}, NAV: {n}")

    if args.remove_fund:
        weightage_path, nav_path = args.remove_fund
        config.remove_fund_files(weightage_path, nav_path)

    if args.nse_master:
        manager.set_path(file_manager.KEY_NSE_MASTER, args.nse_master)
        print(f"  NSE Security Master set from command line -> {args.nse_master}")
    if args.bse_master:
        manager.set_path(file_manager.KEY_BSE_MASTER, args.bse_master)
        print(f"  BSE Security Master set from command line -> {args.bse_master}")

    if args.update_security_masters:
        manager.update_security_masters()


def run_fund(fund_code: str, fund_data: data_loader.FundData, sector_future: Future, args):
    # The Weightage file has one row per holding per month-end since
    # inception, so pull just a single month's snapshot here - using the
    # full history would merge multiple months' rows for the same ISIN
    # together downstream.
    fund_weightage = data_loader.latest_snapshot(fund_data.weightage, fund_code, as_of=args.as_of)
    fund_nav = fund_data.nav[fund_data.nav["Fund Code"] == fund_code].copy()
    fund_name = fund_weightage["Fund Name"].iloc[0]
    snapshot_date = fund_weightage["Date"].iloc[0]

    print(f"\n=== {fund_name} ({fund_code}) ===")
    print(f"  Using Weightage snapshot as of {snapshot_date.date()}"
          + (" (most recent available)" if not args.as_of else " (--as-of requested)"))

    # --- merge in just the Yahoo Ticker for now (Sector/Industry comes later,
    # once the background fetch has finished - see below) ------------------
    holdings = fund_weightage.merge(
        fund_data.mapping[["ISIN", "Yahoo Ticker"]], on="ISIN", how="left"
    )

    # --- performance ---------------------------------------------------------
    print("  Calculating performance metrics...")
    perf = performance.compute_fund_performance(fund_nav)
    # Same metrics, broken out by period (Since Inception / 5Y / 3Y / 1Y /
    # Current FY) rather than only the fund's full history.
    period_perf = performance.compute_multi_period_performance(fund_nav)

    # --- attribution: stock returns ------------------------------------------
    # Deliberately runs before Sector/Industry is merged in - it doesn't need
    # it, only the Yahoo Ticker column above, so this overlaps with the
    # sector_future background fetch instead of waiting for it.
    print("  Fetching stock-level returns for attribution...")
    stock_returns = attribution.fetch_stock_returns(
        holdings, start=perf["summary"]["Start Date"], end=perf["summary"]["End Date"]
    )
    holdings = holdings.merge(stock_returns, on="ISIN", how="left")

    # --- now bring in Sector/Industry ----------------------------------------
    # By this point the background sector fetch launched in main() has had
    # the whole attribution fetch above to run concurrently, so this usually
    # returns immediately; .result() only blocks if it's somehow still going.
    sector_data = sector_future.result()
    holdings = yahoo_fetch.merge_sector_with_holdings(holdings, sector_data)

    stock_contrib = attribution.stock_contribution(holdings)
    sector_contrib = attribution.sector_contribution(stock_contrib)

    # --- automatic rebalancing: drift vs the previous month-end snapshot -----
    previous_holdings = data_loader.previous_snapshot(
        fund_data.weightage, fund_code, before_date=snapshot_date
    )
    if previous_holdings is None:
        print("  No earlier snapshot available for this fund - every holding treated as new "
              "(Previous Weight = 0%) for drift purposes.")
    rebalance_df = rebalance.compute_weight_drift(holdings, previous_holdings, threshold=args.threshold)

    # --- report ------------------------------------------------------------
    print("  Generating Excel report and charts...")
    out_path = report_generator.build_fund_report(
        fund_code=fund_code,
        fund_name=fund_name,
        perf=perf,
        period_perf=period_perf,
        holdings_enriched=holdings,
        stock_contrib=stock_contrib,
        sector_contrib=sector_contrib,
        rebalance_df=rebalance_df,
        threshold=args.threshold,
    )
    print(f"  Report saved -> {out_path}")
    return out_path


def main():
    args = parse_args()
    apply_file_overrides(args)

    print("Loading and validating input files...")
    fund_data = data_loader.load_all()

    # Kick off the (global, once-for-all-funds) sector/industry lookup in the
    # background now, so it overlaps with each fund's attribution price
    # fetch below instead of blocking in front of it - see module docstring.
    print("Resolving ISIN -> Yahoo Ticker -> Sector/Industry (in the background)...")
    sector_executor = ThreadPoolExecutor(max_workers=1)
    sector_future = sector_executor.submit(
        yahoo_fetch.fetch_sector_data,
        fund_data.mapping,
        not args.no_cache,
        args.no_sector_fetch,
    )

    fund_codes = data_loader.get_fund_codes(fund_data)
    print(f"Found {len(fund_codes)} fund(s): {fund_codes}")

    generated = []
    for fund_code in fund_codes:
        path = run_fund(fund_code, fund_data, sector_future, args)
        generated.append(path)

    sector_executor.shutdown()
    print(f"\nDone. {len(generated)} report(s) written to {config.OUTPUT_DIR}")


if __name__ == "__main__":
    main()
