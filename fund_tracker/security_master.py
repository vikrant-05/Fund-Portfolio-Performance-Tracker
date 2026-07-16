"""
security_master.py
-------------------
Dedicated, self-contained module for resolving ISIN -> Yahoo Finance ticker
using the NSE and BSE Security Master reference files, and for handing back
Yahoo Finance market data (sector/industry) for a given ISIN. Nothing else
in the application needs to know these files exist, what columns they have,
or that a "ticker" is involved at all - the rest of the pipeline supplies an
ISIN and gets data back; the ISIN remains the primary identifier everywhere
else.

Resolution order (per the firm's convention)
----------------------------------------------
    1. Look up the ISIN in the NSE Security Master.
       Found  -> Yahoo ticker = <NSE symbol> + ".NS"
    2. If not found on NSE, look up the ISIN in the BSE Security Master.
       Found  -> Yahoo ticker = <BSE scrip code> + ".BO"
    3. If not found on either -> log it and skip. The rest of the pipeline
       already treats a missing/blank ticker as "Unknown" sector / 0% return
       and keeps running (see yahoo_fetch.py, attribution.py) - this module
       never raises or terminates the application over a single bad ISIN.

Security Master file formats
-------------------------------
NSE and BSE both publish these as CSV (occasionally Excel) with slightly
different, sometimes-changing column headers. Rather than hardcode one exact
header name, each column we need is found by matching against a short list
of known aliases (case/whitespace-insensitive), falling back to "any column
whose name contains the keyword". To support a header NSE/BSE renames in a
future release, just add the new spelling to the alias lists below - no
other code needs to change, which is the "minimal modifications" scalability
the rest of the app expects from this module.

Note: some BSE downloads are a daily Bhavcopy/trade file (e.g.
BhavCopy_BSE_CM_*.CSV) rather than the static scrip master. Those use
ISIN + TckrSymb (+ Sgmt for segment) instead of ISIN_NO + SC_CODE + SC_GROUP.
Both shapes are supported below.
"""

from pathlib import Path

import pandas as pd

import config

UNKNOWN = "Unknown"

# --- column aliases -----------------------------------------------------
# Add new header spellings here if NSE/BSE change their file format.
NSE_ISIN_ALIASES = ["ISIN NUMBER", "ISIN_NUMBER", "ISIN CODE", "ISIN"]
NSE_SYMBOL_ALIASES = ["SYMBOL", "TRADING SYMBOL", "SECURITY ID"]
NSE_SERIES_ALIASES = ["SERIES"]
NSE_PREFERRED_SERIES = "EQ"  # when an ISIN has multiple series rows, prefer this one

BSE_ISIN_ALIASES = ["ISIN_NO", "ISIN NO", "ISIN NUMBER", "ISIN CODE", "ISIN"]
BSE_SYMBOL_ALIASES = ["SC_CODE", "SECURITY CODE", "SCRIP CODE", "SCRIP_CD", "SC CODE",
                       "TCKRSYMB", "TICKER SYMBOL", "TRADING SYMBOL", "SYMBOL"]
BSE_GROUP_ALIASES = ["SC_GROUP", "SECURITY GROUP", "SC GROUP"]
BSE_PREFERRED_GROUP = "A"  # when an ISIN has multiple listings, prefer group A

# Some BSE downloads are actually daily Bhavcopy/trade files rather than the
# static scrip master (e.g. BhavCopy_BSE_CM_*.CSV). Those mix equity (Cash
# Market) rows in with F&O contract rows for the *same* underlying ISIN, so
# if a segment column is present we filter down to the equity segment first
# - otherwise the same ISIN could resolve to a derivatives contract symbol
# instead of the actual traded equity symbol.
BSE_SEGMENT_ALIASES = ["SGMT", "SEGMENT", "SEGMENT TYPE"]
BSE_PREFERRED_SEGMENT = "CM"  # Cash Market


# =============================================================================
# Column detection helpers
# =============================================================================
def _normalise(name: str) -> str:
    return "".join(ch for ch in str(name).upper() if ch.isalnum())


def _find_column(df: pd.DataFrame, aliases: list) -> str | None:
    """Match a column by exact alias first, then by substring, both
    whitespace/case-insensitive, so header formatting quirks (extra spaces,
    underscores vs. spaces) don't break the lookup."""
    normalised_cols = {_normalise(c): c for c in df.columns}

    for alias in aliases:
        hit = normalised_cols.get(_normalise(alias))
        if hit:
            return hit

    # fall back: any column containing the first alias's core keyword
    keyword = _normalise(aliases[0])[:4]  # e.g. "ISIN" or "SYMB"
    for norm, original in normalised_cols.items():
        if keyword in norm:
            return original
    return None


def _read_master_file(path: Path) -> pd.DataFrame:
    suffix = path.suffix.lower()
    if suffix in (".xlsx", ".xls"):
        return pd.read_excel(path, dtype=str)
    # CSV: NSE/BSE downloads are sometimes latin-1 rather than utf-8
    try:
        return pd.read_csv(path, dtype=str, encoding="utf-8")
    except UnicodeDecodeError:
        return pd.read_csv(path, dtype=str, encoding="latin-1")


# =============================================================================
# Lazily-built, in-memory lookup tables (rebuilt only when a Security Master
# file changes, e.g. via config.update_security_masters()). These files are
# static reference data and cheap to parse, so no disk cache is needed - the
# in-memory dict is rebuilt at most once per process.
# =============================================================================
_nse_lookup: dict = {}
_bse_lookup: dict = {}
_nse_source: Path = None
_bse_source: Path = None


def _build_nse_lookup(path: Path) -> dict:
    df = _read_master_file(path)
    isin_col = _find_column(df, NSE_ISIN_ALIASES)
    symbol_col = _find_column(df, NSE_SYMBOL_ALIASES)
    series_col = _find_column(df, NSE_SERIES_ALIASES)

    if not isin_col or not symbol_col:
        raise ValueError(
            f"Could not find ISIN/Symbol columns in NSE Security Master "
            f"({path}). Found columns: {list(df.columns)}. Add the correct "
            f"header spelling to NSE_ISIN_ALIASES/NSE_SYMBOL_ALIASES in "
            f"security_master.py."
        )

    df = df[[isin_col, symbol_col] + ([series_col] if series_col else [])].copy()
    df[isin_col] = df[isin_col].astype(str).str.strip().str.upper()
    df[symbol_col] = df[symbol_col].astype(str).str.strip()

    lookup = {}
    for isin, group in df.groupby(isin_col):
        if len(group) > 1 and series_col:
            preferred = group[group[series_col].astype(str).str.upper() == NSE_PREFERRED_SERIES]
            row = preferred.iloc[0] if not preferred.empty else group.iloc[0]
        else:
            row = group.iloc[0]
        lookup[isin] = row[symbol_col]
    return lookup


def _build_bse_lookup(path: Path) -> dict:
    df = _read_master_file(path)
    isin_col = _find_column(df, BSE_ISIN_ALIASES)
    symbol_col = _find_column(df, BSE_SYMBOL_ALIASES)
    group_col = _find_column(df, BSE_GROUP_ALIASES)
    segment_col = _find_column(df, BSE_SEGMENT_ALIASES)

    if not isin_col or not symbol_col:
        raise ValueError(
            f"Could not find ISIN/Scrip-code columns in BSE Security Master "
            f"({path}). Found columns: {list(df.columns)}. Add the correct "
            f"header spelling to BSE_ISIN_ALIASES/BSE_SYMBOL_ALIASES in "
            f"security_master.py."
        )

    keep_cols = [isin_col, symbol_col]
    if group_col:
        keep_cols.append(group_col)
    if segment_col:
        keep_cols.append(segment_col)
    df = df[keep_cols].copy()
    df[isin_col] = df[isin_col].astype(str).str.strip().str.upper()
    df[symbol_col] = df[symbol_col].astype(str).str.strip()

    # If this is really a Bhavcopy/trade file rather than a static scrip
    # master, it may carry multiple segments (equity, F&O, ...) for the same
    # underlying ISIN. Restrict to the equity/Cash Market segment first so we
    # don't pick up a derivatives contract's symbol by accident.
    if segment_col:
        equity_only = df[df[segment_col].astype(str).str.strip().str.upper() == BSE_PREFERRED_SEGMENT]
        if not equity_only.empty:
            df = equity_only

    lookup = {}
    for isin, group in df.groupby(isin_col):
        if len(group) > 1 and group_col:
            preferred = group[group[group_col].astype(str).str.upper() == BSE_PREFERRED_GROUP]
            row = preferred.iloc[0] if not preferred.empty else group.iloc[0]
        else:
            row = group.iloc[0]
        lookup[isin] = row[symbol_col]
    return lookup


def _ensure_loaded(force: bool = False) -> None:
    """(Re)load both Security Master files into memory if they haven't been
    loaded yet, or if a different file is now configured (e.g. after
    config.update_security_masters())."""
    global _nse_lookup, _bse_lookup, _nse_source, _bse_source

    nse_path = config.get_nse_security_master()
    if force or nse_path != _nse_source:
        _nse_lookup = _build_nse_lookup(nse_path)
        _nse_source = nse_path
        print(f"  Loaded {len(_nse_lookup)} ISIN(s) from NSE Security Master ({nse_path.name}).")

    bse_path = config.get_bse_security_master()
    if force or bse_path != _bse_source:
        _bse_lookup = _build_bse_lookup(bse_path)
        _bse_source = bse_path
        print(f"  Loaded {len(_bse_lookup)} ISIN(s) from BSE Security Master ({bse_path.name}).")


def reload_masters() -> None:
    """Force a fresh re-read of both Security Master files (e.g. right after
    the user has picked new ones via config.update_security_masters())."""
    _ensure_loaded(force=True)


def validate_nse_master_file(path) -> list:
    """
    Validation for a candidate NSE Security Master file, used before it's
    registered (e.g. by the dashboard's "Security Master files" uploader)
    so a badly-formatted file is rejected with a clear message instead of
    silently breaking every subsequent ISIN lookup. Does a real (not just
    header) parse, since _build_nse_lookup() also needs to detect the
    ISIN/Symbol columns. Returns a list of problems (empty list = file
    looks fine).
    """
    try:
        _build_nse_lookup(Path(path))
    except ValueError as exc:
        return [str(exc)]
    except Exception as exc:  # noqa: BLE001 - any other read/parse failure
        return [f"Could not read '{path}' as an NSE Security Master file: {exc}"]
    return []


def validate_bse_master_file(path) -> list:
    """Same as validate_nse_master_file(), but for the BSE Security Master
    (also accepts the daily Bhavcopy/trade-file shape - see module
    docstring)."""
    try:
        _build_bse_lookup(Path(path))
    except ValueError as exc:
        return [str(exc)]
    except Exception as exc:  # noqa: BLE001
        return [f"Could not read '{path}' as a BSE Security Master file: {exc}"]
    return []


# =============================================================================
# Public API
# =============================================================================
def get_ticker(isin: str) -> tuple:
    """
    Resolve a single ISIN to a Yahoo Finance ticker.
    Returns (ticker, exchange) where exchange is "NSE", "BSE", or None.
    Tries NSE first, then BSE, per the firm's resolution order. Never
    raises for an unresolved ISIN - returns (None, None) instead.
    """
    _ensure_loaded()
    isin = str(isin).strip().upper()

    symbol = _nse_lookup.get(isin)
    if symbol:
        return f"{symbol}.NS", "NSE"

    symbol = _bse_lookup.get(isin)
    if symbol:
        return f"{symbol}.BO", "BSE"

    return None, None


def resolve_tickers(isins: list) -> pd.DataFrame:
    """
    Resolve a list of ISINs to Yahoo tickers via the Security Master files.
    Returns a DataFrame: ISIN | Yahoo Ticker | Exchange | Status
    'Status' is 'Resolved' or 'Not Found'. Any 'Not Found' ISIN is logged
    (printed, and appended to Outputs/.cache/missing_isins.log) rather than
    raising - the caller is expected to carry on without it.
    """
    rows, missing = [], []
    for isin in isins:
        isin = str(isin).strip().upper()
        ticker, exchange = get_ticker(isin)
        if ticker:
            rows.append({"ISIN": isin, "Yahoo Ticker": ticker, "Exchange": exchange,
                         "Status": "Resolved"})
        else:
            rows.append({"ISIN": isin, "Yahoo Ticker": "", "Exchange": "",
                         "Status": "Not Found"})
            missing.append(isin)

    if missing:
        print(f"  {len(missing)} ISIN(s) not found in either Security Master file "
              f"(Yahoo Finance lookup skipped for these): {missing}")
        _log_missing(missing)

    return pd.DataFrame(rows)


def get_ticker_map(isins: list) -> dict:
    """Convenience wrapper: {ISIN: Yahoo Ticker} for resolved ISINs only."""
    df = resolve_tickers(isins)
    resolved = df[df["Yahoo Ticker"] != ""]
    return dict(zip(resolved["ISIN"], resolved["Yahoo Ticker"]))


def get_market_data(isin: str) -> dict:
    """
    One-call convenience for callers that just want Yahoo Finance data for
    an ISIN, with no knowledge of tickers or Security Master files at all:
    resolves the ticker internally, then delegates to yahoo_fetch for
    Sector/Industry. Returns {'Yahoo Ticker', 'Exchange', 'Sector', 'Industry'}.
    (main.py's pipeline instead uses the batch fetch_sector_data() path in
    yahoo_fetch.py for efficiency across many holdings at once - this
    single-ISIN helper is for ad-hoc/standalone use, e.g. the dashboard.)
    """
    import yahoo_fetch  # local import: avoids a circular import at module load

    ticker, exchange = get_ticker(isin)
    if not ticker:
        return {"Yahoo Ticker": "", "Exchange": "", "Sector": UNKNOWN, "Industry": UNKNOWN}

    data = yahoo_fetch._fetch_one(ticker)
    return {"Yahoo Ticker": ticker, "Exchange": exchange, **data}


def _log_missing(isins: list) -> None:
    try:
        log_path = config.CACHE_DIR / "missing_isins.log"
        with open(log_path, "a") as f:
            for isin in isins:
                f.write(f"{isin}\n")
    except OSError:
        pass  # logging is best-effort, never let it break the run


# =============================================================================
# Standalone / debugging entry point
# =============================================================================
def main():
    import argparse

    p = argparse.ArgumentParser(description="Resolve ISIN -> Yahoo ticker via NSE/BSE Security Master files")
    p.add_argument("--isin", nargs="+", required=True, help="ISIN(s) to resolve")
    p.add_argument("--reload", action="store_true", help="Force a fresh re-read of both master files")
    args = p.parse_args()

    if args.reload:
        reload_masters()

    result = resolve_tickers([i.strip().upper() for i in args.isin])
    print(result.to_string(index=False))


if __name__ == "__main__":
    main()