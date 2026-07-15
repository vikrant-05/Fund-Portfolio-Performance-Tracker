"""
data_loader.py
--------------
Loads and validates the pipeline's inputs:
    - Weightage file(s)  (portfolio composition, per fund - one row per
      holding per MONTH-END since the fund's inception, not just a single
      snapshot)
    - Daily NAV file(s)  (daily portfolio & benchmark NAV, per fund)

A firm isn't limited to one Weightage/NAV file: config.get_weightage_files()
/ config.get_nav_files() each return a *list* of files (see file_manager.py),
and every one of them is loaded and concatenated here, so adding a new fund
is just a matter of supplying its own Weightage + Daily NAV Excel pair (same
column headers) via config.add_fund_files() / the dashboard's "Add a fund"
uploader - the new fund's code then shows up automatically everywhere
(fund dropdown, reports, etc.) without anything else changing.

The ISIN -> Yahoo ticker mapping is resolved via security_master.py, which
looks the ISIN up in the NSE/BSE Security Master reference files (also
obtained through config.py, never hardcoded) - nothing here needs to know
how that resolution works.

Every function here raises a ValueError with a clear message on bad data
instead of silently continuing, since a silent NaN in NAV or weight data
quietly corrupts every downstream calculation - the one deliberate
exception being Daily NAV rows that are legitimately not yet available
(see load_nav()) and Cash lines in Weightage (see CASH_ISIN below), both
of which are expected gaps rather than data errors.
"""

import re
from dataclasses import dataclass

import pandas as pd

import config
import security_master

# Weightage carries a "Cash" line per fund per month (un-invested cash /
# net receivables) alongside real securities. Cash has no ISIN, no ticker,
# and no sector - it's exempt from Security Master resolution and
# sector/return lookups, but its weight still counts toward the fund's
# total allocation.
CASH_ISIN = "CASH"
_CASH_NAME_RE = re.compile(r"\bcash\b", re.IGNORECASE)

WEIGHTAGE_REQUIRED_COLUMNS = ["Date", "Fund Code", "Fund Name", "ISIN", "Stock Name", "Current Weight"]
NAV_REQUIRED_COLUMNS = ["Date", "Fund Code", "Portfolio NAV", "Benchmark NAV"]


@dataclass
class FundData:
    """Container for everything the pipeline needs, already validated."""
    weightage: pd.DataFrame
    nav: pd.DataFrame
    mapping: pd.DataFrame


def _read_excel(path, required_columns):
    if not path.exists():
        raise FileNotFoundError(
            f"Required input file not found: {path}\n"
            f"It may have been moved since it was last configured - re-run the "
            f"tool and you'll be prompted to select it again."
        )
    df = pd.read_excel(path)
    missing = set(required_columns) - set(df.columns)
    if missing:
        raise ValueError(f"{path.name} is missing required column(s): {sorted(missing)}")
    return df


def _validate_headers(path, required_columns) -> list:
    """
    Lightweight header-only check (no full read) for a candidate fund file,
    used before it's added to the configuration - e.g. by the dashboard's
    "Add a fund" uploader - so a badly-formatted file is rejected with a
    clear message instead of silently breaking every subsequent run.
    Returns a list of problems (empty list = file looks fine).
    """
    try:
        preview = pd.read_excel(path, nrows=0)
    except Exception as exc:  # noqa: BLE001 - any read/parse failure
        return [f"Could not read '{path}' as an Excel file: {exc}"]
    missing = set(required_columns) - set(preview.columns)
    if missing:
        return [f"Missing required column(s): {sorted(missing)}"]
    return []


def validate_weightage_file(path) -> list:
    """Header-only validation for a candidate Weightage file. Empty list = OK."""
    return _validate_headers(path, WEIGHTAGE_REQUIRED_COLUMNS)


def validate_nav_file(path) -> list:
    """Header-only validation for a candidate Daily NAV file. Empty list = OK."""
    return _validate_headers(path, NAV_REQUIRED_COLUMNS)


def _is_cash_row(df: pd.DataFrame) -> pd.Series:
    """
    True for rows representing cash/cash-equivalents rather than a real
    security: either the ISIN is already the "CASH" placeholder / blank, or
    the Stock Name mentions "cash" (e.g. "Cash & Cash Equivalents", "Net
    Cash", "Cash Balance").
    """
    isin_upper = df["ISIN"].astype(str).str.strip().str.upper()
    name = df["Stock Name"].astype(str).str.strip()
    isin_blank_or_cash = isin_upper.isin(["", "NAN", CASH_ISIN])
    name_says_cash = name.str.contains(_CASH_NAME_RE, na=False)
    return isin_blank_or_cash | name_says_cash


def load_weightage() -> pd.DataFrame:
    paths = config.get_weightage_files()
    frames = []
    for path in paths:
        frame = _read_excel(path, WEIGHTAGE_REQUIRED_COLUMNS)
        frames.append(frame)
    if not frames:
        raise FileNotFoundError("No Weightage file is configured.")

    print(f"  Loading {len(frames)} Weightage file(s): "
          f"{', '.join(p.name for p in paths)}")
    df = pd.concat(frames, ignore_index=True)

    df["Date"] = pd.to_datetime(df["Date"])
    df["ISIN"] = df["ISIN"].astype(str).str.strip().str.upper()
    df["Fund Code"] = df["Fund Code"].astype(str).str.strip()

    # --- cash exemption -----------------------------------------------------
    # Normalise every cash-like row onto one placeholder ISIN so downstream
    # merges (mapping/sector/attribution) all key off the same value, and so
    # a missing ISIN on a cash line doesn't get flagged as bad data below.
    df["Is Cash"] = _is_cash_row(df)
    df.loc[df["Is Cash"], "ISIN"] = CASH_ISIN

    # --- validation -------------------------------------------------------
    missing_isin = (~df["Is Cash"]) & (
        df["ISIN"].isna() | (df["ISIN"] == "") | (df["ISIN"] == "NAN")
    )
    if missing_isin.any():
        raise ValueError(
            f"Weightage file(s) have {missing_isin.sum()} row(s) with a missing ISIN "
            f"that aren't recognisable as Cash: "
            f"{df.loc[missing_isin, ['Fund Code', 'Date', 'Stock Name']].to_dict('records')}"
        )

    missing_weight = df["Current Weight"].isna()
    if missing_weight.any():
        raise ValueError(
            f"Weightage file(s) have {missing_weight.sum()} row(s) with a missing weight."
        )

    # Duplicate-ISIN check only applies to real securities within the same
    # month: a fund can legitimately carry more than one cash-labelled line
    # in a given month (e.g. "Cash" and "Net Receivables"), and the same
    # security naturally recurs across different months, which is expected,
    # not a duplicate. This also catches the same fund/month accidentally
    # appearing in two different uploaded files.
    securities = df[~df["Is Cash"]]
    dupes = securities.duplicated(subset=["Fund Code", "Date", "ISIN"], keep=False)
    if dupes.any():
        raise ValueError(
            "Weightage file(s) have duplicate (Fund Code, Date, ISIN) rows for the same "
            "security within the same month (check whether the same fund/month appears "
            "in more than one uploaded file):\n"
            f"{securities.loc[dupes, ['Fund Code', 'Date', 'ISIN']].to_string(index=False)}"
        )

    if df["Is Cash"].any():
        n_cash_rows = int(df["Is Cash"].sum())
        print(f"  Note: {n_cash_rows} Cash/Cash-equivalent row(s) found - exempt from "
              f"Security Master resolution and sector lookup, but included in weight totals.")

    # Weights per fund/date should sum to roughly 100. Flag, don't fail hard,
    # since a manager may intentionally hold a small uninvested buffer.
    totals = df.groupby(["Fund Code", "Date"])["Current Weight"].sum()
    off = totals[(totals < 95) | (totals > 105)]
    if not off.empty:
        print(
            "  Warning: the following fund/date weight totals are outside "
            f"95-105%, please double check:\n{off.to_string()}"
        )

    return df


def load_nav() -> pd.DataFrame:
    """
    A NAV value of zero (or blank) means the data simply isn't available yet
    for that date (e.g. a fund's inception month, a data-entry gap) rather
    than a real NAV of zero, so those rows are dropped with a warning instead
    of failing the whole load.
    """
    paths = config.get_nav_files()
    frames = []
    for path in paths:
        frame = _read_excel(path, NAV_REQUIRED_COLUMNS)
        frames.append(frame)
    if not frames:
        raise FileNotFoundError("No Daily NAV file is configured.")

    print(f"  Loading {len(frames)} Daily NAV file(s): "
          f"{', '.join(p.name for p in paths)}")
    df = pd.concat(frames, ignore_index=True)

    df["Date"] = pd.to_datetime(df["Date"])
    df["Fund Code"] = df["Fund Code"].astype(str).str.strip()

    # NAV columns sometimes come in from Excel as text (thousands-separator
    # commas, stray whitespace, a "-" placeholder, cells formatted as text,
    # etc). Left as object dtype, those values compare/plot unpredictably
    # downstream - e.g. a whole NAV series silently rendering as a flat 0
    # line in the dashboard chart instead of raising anything. Coerce to
    # numeric explicitly and surface exactly which rows didn't survive that
    # conversion, rather than let them pass through as an ambiguous falsy
    # value.
    for nav_col in ("Portfolio NAV", "Benchmark NAV"):
        numeric = pd.to_numeric(
            df[nav_col].astype(str).str.strip().str.replace(",", "", regex=False),
            errors="coerce",
        )
        bad = numeric.isna() & df[nav_col].notna() & (df[nav_col].astype(str).str.strip() != "")
        if bad.any():
            print(
                f"  Warning: {bad.sum()} row(s) in the Daily NAV file(s) have a non-numeric "
                f"'{nav_col}' value (e.g. text, stray characters) - treating as unavailable, "
                f"same as a missing/zero NAV:\n"
                f"{df.loc[bad, ['Fund Code', 'Date', nav_col]].to_string(index=False)}"
            )
        df[nav_col] = numeric

    portfolio_bad = df["Portfolio NAV"].isna() | (df["Portfolio NAV"].fillna(0) <= 0)
    benchmark_bad = df["Benchmark NAV"].isna() | (df["Benchmark NAV"].fillna(0) <= 0)
    unavailable = portfolio_bad | benchmark_bad
    if unavailable.any():
        # Report Portfolio vs Benchmark separately - a whole row is dropped
        # either way (see docstring), but conflating the two counts makes a
        # benchmark-only problem (e.g. a broken external-workbook formula
        # link in the Benchmark NAV column) look like a Portfolio NAV
        # problem, when the Portfolio NAV for those same dates may be
        # perfectly fine.
        print(
            f"  Note: ignoring {unavailable.sum()} row(s) in the Daily NAV file(s) with a "
            "missing/zero Portfolio and/or Benchmark NAV (treated as data not yet "
            "available, not an error) - "
            f"{portfolio_bad.sum()} due to Portfolio NAV, {benchmark_bad.sum()} due to "
            "Benchmark NAV:\n"
            f"{df.loc[unavailable, ['Fund Code', 'Date']].to_string(index=False)}"
        )
        df = df.loc[~unavailable].copy()

    if df.empty:
        raise ValueError("Daily NAV file(s) have no valid (non-zero) NAV rows after filtering.")

    df = df.sort_values(["Fund Code", "Date"]).reset_index(drop=True)

    dupes = df.duplicated(subset=["Fund Code", "Date"], keep=False)
    if dupes.any():
        raise ValueError(
            "Daily NAV file(s) have duplicate (Fund Code, Date) rows (check whether the "
            "same fund/date appears in more than one uploaded file)."
        )

    return df


def resolve_mapping(weightage: pd.DataFrame) -> pd.DataFrame:
    """
    Resolve every unique real-security ISIN in the Weightage file(s) to a
    Yahoo ticker via the NSE/BSE Security Master files (security_master.py).
    Cash is excluded from that lookup and given a synthetic mapping row
    instead, so it flows cleanly through downstream merges without ever
    being sent through the ISIN resolution or Yahoo Finance calls.

    Returns ISIN | Yahoo Ticker | Exchange | Status.
    """
    real_isins = sorted(weightage.loc[weightage["ISIN"] != CASH_ISIN, "ISIN"].unique().tolist())
    print(f"  Resolving {len(real_isins)} unique ISIN(s) via NSE/BSE Security Master files...")
    mapping = security_master.resolve_tickers(real_isins)

    if (weightage["ISIN"] == CASH_ISIN).any():
        cash_row = pd.DataFrame([{
            "ISIN": CASH_ISIN, "Yahoo Ticker": "", "Exchange": "", "Status": "Cash (exempt)",
        }])
        mapping = pd.concat([mapping, cash_row], ignore_index=True)

    unresolved = mapping.loc[
        (mapping["Yahoo Ticker"] == "") & (mapping["ISIN"] != CASH_ISIN), "ISIN"
    ].tolist()
    if unresolved:
        print(
            f"  Warning: {len(unresolved)} ISIN(s) could not be resolved in either "
            f"Security Master file and will show as 'Unknown' sector: {sorted(unresolved)}"
        )

    return mapping


def latest_snapshot(weightage: pd.DataFrame, fund_code: str, as_of: str = None) -> pd.DataFrame:
    """
    The Weightage file holds one row per holding per MONTH-END since
    inception, so the same security recurs every month a fund keeps holding
    it. Any holdings-based calculation (attribution, rebalancing) needs a
    single point-in-time snapshot, not the full history - merging multiple
    months' worth of rows for the same ISIN against another multi-month
    table is what produces "duplicate ISIN" blow-ups downstream.

    Returns just one month's rows for `fund_code`:
      - the most recent month-end on or before `as_of` if given (accepts
        anything pandas can parse, e.g. "2026-06-30" or "2026-06")
      - otherwise the most recent month-end available for that fund
    """
    fund_df = weightage[weightage["Fund Code"] == fund_code]
    if fund_df.empty:
        raise ValueError(f"No Weightage rows found for fund code '{fund_code}'.")

    if as_of:
        as_of_ts = pd.to_datetime(as_of)
        eligible = fund_df[fund_df["Date"] <= as_of_ts]
        if eligible.empty:
            raise ValueError(
                f"No Weightage snapshot on or before {as_of_ts.date()} for fund "
                f"{fund_code} (earliest available is {fund_df['Date'].min().date()})."
            )
        target_date = eligible["Date"].max()
    else:
        target_date = fund_df["Date"].max()

    return fund_df[fund_df["Date"] == target_date].copy()


def previous_snapshot(weightage: pd.DataFrame, fund_code: str, before_date) -> pd.DataFrame:
    """
    Return the month-end snapshot immediately before `before_date` for
    `fund_code` (used to measure month-over-month weight drift for
    rebalancing - see rebalance.compute_weight_drift()).

    Returns None if `before_date` is the fund's earliest available
    snapshot (i.e. there's nothing earlier to compare against yet).
    """
    fund_df = weightage[weightage["Fund Code"] == fund_code]
    before_ts = pd.to_datetime(before_date)
    earlier = fund_df[fund_df["Date"] < before_ts]
    if earlier.empty:
        return None
    target_date = earlier["Date"].max()
    return fund_df[fund_df["Date"] == target_date].copy()


def load_all() -> FundData:
    """Load and cross-validate all inputs together, resolving tickers via
    the Security Master files."""
    weightage = load_weightage()
    nav = load_nav()
    mapping = resolve_mapping(weightage)

    # Every fund code in Weightage should have a matching NAV series.
    funds_no_nav = set(weightage["Fund Code"]) - set(nav["Fund Code"])
    if funds_no_nav:
        raise ValueError(
            f"Fund code(s) {sorted(funds_no_nav)} appear in the Weightage file(s) but have "
            f"no rows in the Daily NAV file(s) - performance cannot be calculated for them."
        )

    return FundData(weightage=weightage, nav=nav, mapping=mapping)


def get_fund_codes(fund_data: FundData) -> list:
    """Unique fund codes to loop over. Never hardcode a fund name in the pipeline."""
    return sorted(fund_data.weightage["Fund Code"].unique().tolist())