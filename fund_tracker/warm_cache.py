"""
warm_cache.py
-------------
Stand-alone pre-fetch script: resolves every ISIN that has EVER appeared in
the Weightage file (not just the current month's snapshot) to a Yahoo
ticker, and pre-fetches + caches its Sector/Industry.

Why this exists
-----------------
Sector/Industry is near-static reference data - a company's sector doesn't
change month to month - so there's no reason to fetch it live, in the
middle of an interactive run, over and over. The practical fix for "Yahoo
Finance is too slow" is to do this fetch once, offline, ahead of time:

    Run this script (e.g. overnight via cron / Windows Task Scheduler, or
    manually right after adding a new stock to a fund) and every
    subsequent `python main.py` / `streamlit run dashboard.py` reads
    entirely from the on-disk cache (config.SECTOR_CACHE_FILE) for any
    ticker it's already seen - no live Yahoo Finance calls, no waiting.

This intentionally covers a fund's FULL holding history (every month since
inception), not just the latest snapshot, so switching to an older
--as-of snapshot, or a fund manager reviewing a past month, never triggers
a live fetch either.

Run:
    python warm_cache.py
"""

import time

import data_loader
import security_master
import yahoo_fetch


def main():
    print("Loading full Weightage history (every month-end since inception, "
          "not just the latest snapshot)...")
    weightage = data_loader.load_weightage()

    all_isins = sorted(
        weightage.loc[weightage["ISIN"] != data_loader.CASH_ISIN, "ISIN"].unique().tolist()
    )
    print(f"Found {len(all_isins)} unique ISIN(s) across the fund's full history.")

    print("Resolving ISIN -> Yahoo Ticker via NSE/BSE Security Master files...")
    mapping = security_master.resolve_tickers(all_isins)

    print("Fetching (and caching) Sector/Industry for every resolved ticker "
          "not already cached - this is the one-time/off-hours cost this "
          "script exists to absorb...")
    start = time.monotonic()
    yahoo_fetch.fetch_sector_data(mapping, use_cache=True)
    elapsed = time.monotonic() - start

    print(f"\nDone in {elapsed:.1f}s. Sector/Industry cache is now warm for all "
          f"{len(all_isins)} ISIN(s) - main.py / dashboard.py will read this from "
          f"disk instead of hitting Yahoo Finance live for any of them.")


if __name__ == "__main__":
    main()
