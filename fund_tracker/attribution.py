"""
attribution.py
---------------
Stock-level and sector-level contribution to return.

    Contribution = Weight x Return

Weight is the holding's Current Weight (as-of the Weightage snapshot date),
and Return is the stock's return over the fund's analysis window. Because
the input data available today is limited to firm-level NAV (not per-stock
daily prices), stock return is approximated using each stock's own price
history pulled from Yahoo Finance over the same window as the NAV series -
this keeps the contribution numbers on a like-for-like time horizon with the
fund's Absolute Return in the performance summary.

Rate-limit handling
--------------------
Earlier versions of this module called Yahoo Finance once per holding in a
loop, which is exactly the pattern that triggers "Too Many Requests" (429)
errors on any fund with more than a handful of holdings. Instead:
  - all tickers needed for a fund are fetched in a single batched
    yf.download() call (Yahoo Finance supports multiple tickers per
    request), rather than one request per stock;
  - results are cached to disk (config.RETURN_CACHE_FILE), keyed by
    ticker + start/end date, so re-running the same fund/window (e.g. while
    iterating on target weights) doesn't re-download anything at all;
  - a failed batch never crashes the run - any ticker with no usable data
    falls back to 0% return (flagged), exactly as before.

Return Status (why a holding is 0%)
--------------------------------------
A 0% Stock Return can mean two very different things: the stock genuinely
didn't move, or its return simply couldn't be fetched. Those used to be
indistinguishable - the only trace of a failure was a print() to whatever
console happened to be running the process, which is invisible if the
dashboard/report is run without a visible terminal. Every row returned by
fetch_stock_returns() now also carries a "Return Status" column so the
dashboard and the Excel report can show, per holding, whether its return is
"Fetched (live)", "Cached", "Cash (exempt)", "No Yahoo Ticker" (ISIN never
resolved against a Security Master file), or "Fetch Failed - <reason>" -
the last one carrying the actual yfinance/network error message, not just
a silent zero.
"""

import json
import logging

import pandas as pd
import yfinance as yf

import config

UNKNOWN = "Unknown"

# --- Telemetry Logger Setup ---
logger = logging.getLogger(__name__)
# Basic config ensures these actually print to your console even if your main app doesn't configure logging
logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")


def _valid_ticker(ticker) -> bool:
    """
    True only for a real, resolved Yahoo ticker string.

    Guards against a subtle bug: after `holdings.merge(mapping[...], how="left")`,
    an ISIN with no matching row in `mapping` gets a 'Yahoo Ticker' of NaN
    (a float), not an empty string. `if ticker and ticker != UNKNOWN:` treats
    that NaN as truthy - bool(float('nan')) is True in Python - so the
    holding was silently sent to Yahoo Finance as the literal ticker string
    "nan", failed, and came back as a 0% return indistinguishable from a
    real flat return. This helper is the single place that decides "is this
    a usable ticker", so every truthiness check on a ticker goes through it
    instead of re-deriving the same (buggy) condition in multiple places.
    """
    if ticker is None:
        return False
    if isinstance(ticker, float) and pd.isna(ticker):
        return False
    ticker = str(ticker).strip()
    if not ticker or ticker.lower() == "nan":
        return False
    return ticker.upper() != UNKNOWN.upper()


def robust_yf_download(tickers, start, end, group_by='column', **kwargs):
    """
    Downloads historical data with strict per-ticker validation and explicit 
    telemetry for empty or missing data series.
    """
    if isinstance(tickers, str):
        tickers = [tickers]
        
    logger.info(f"Initiating batch yfinance download for {len(tickers)} tickers from {start.date()} to {end.date()}...")
    
    try:
        df = yf.download(tickers, start=start, end=end, group_by=group_by, **kwargs)
        
        if df.empty:
            logger.error("CRITICAL: The entire downloaded DataFrame is completely empty. Yahoo Finance returned no data.")
            return df
            
        for ticker in tickers:
            if len(tickers) > 1:
                # Handle MultiIndex extraction based on how we grouped it
                if group_by == 'ticker':
                    if ticker not in df.columns.get_level_values(0):
                        logger.warning(f" [MISSING] Ticker '{ticker}' is completely missing from the returned columns.")
                        continue
                    ticker_df = df[ticker]
                else:
                    if ticker not in df.columns.get_level_values(1):
                        logger.warning(f" [MISSING] Ticker '{ticker}' is completely missing from the returned columns.")
                        continue
                    ticker_df = df.xs(ticker, axis=1, level=1)
            else:
                ticker_df = df
                
            if ticker_df.empty:
                logger.warning(f" [EMPTY] Ticker '{ticker}' returned a valid structure but zero rows.")
            elif 'Close' not in ticker_df.columns:
                logger.warning(f" [NO CLOSE] Ticker '{ticker}' returned rows, but has no 'Close' column.")
            elif ticker_df['Close'].isna().all() or (ticker_df['Close'] == 0).all():
                logger.warning(f" [ZERO/NaN] Ticker '{ticker}' returned rows, but 'Close' contains only zeros or NaNs.")
            else:
                non_zero_count = (ticker_df['Close'] > 0).sum()
                logger.info(f" [SUCCESS] Ticker '{ticker}': Loaded {len(ticker_df)} rows ({non_zero_count} non-zero closes).")
                
        return df

    except Exception as e:
        logger.exception(f"CRITICAL failure during yf.download execution layer: {str(e)}")
        raise


def _load_return_cache() -> dict:
    if config.RETURN_CACHE_FILE.exists():
        try:
            return json.loads(config.RETURN_CACHE_FILE.read_text())
        except (json.JSONDecodeError, OSError):
            return {}
    return {}


def _save_return_cache(cache: dict) -> None:
    try:
        config.RETURN_CACHE_FILE.write_text(json.dumps(cache, indent=2))
    except OSError:
        pass  # cache is a nice-to-have, never let it break the run


def _cache_key(ticker: str, start, end) -> str:
    # "v3" marks the outlier-sanity-check fix below (fetch_stock_returns) -
    # bumping this means any return cached under an OLDER version - either
    # the pre-"as-of" buggy date-alignment logic (v1), or the v2 logic that
    # trusted a single boundary Close price with no cross-validation at all -
    # is treated as a cache miss and recomputed with the new checks, rather
    # than a bad cached number (e.g. from one stray tick on a thinly-traded
    # small-cap) being served forever just because that ticker/window
    # combination already has an entry on disk.
    return f"v3|{ticker}|{pd.Timestamp(start).date()}|{pd.Timestamp(end).date()}"


def _asof_close(closes: pd.Series, target_ts: pd.Timestamp,
                 window: int = None, outlier_ratio: float = None):
    """
    The last available Close on/before `target_ts` (the "as-of" price used
    for a return boundary), PLUS a sanity check against that same day's
    local neighbourhood.

    Why this exists: fetch_stock_returns() used to trust this single day's
    raw Close outright. A one-off bad/thin print - not rare for illiquid
    NSE/BSE small- and mid-caps on Yahoo's feed - then silently became "the"
    reported return for that stock, indistinguishable from a genuine move:
    every other holding in the same batch fetch would still be correct,
    nothing would crash, and nothing would flag it. That is exactly the
    failure mode where a report shows one holding with a wildly wrong
    Stock Return (e.g. -33% against an actual ~-2.7%) while every other row
    in the same table is fine.

    This does NOT silently substitute a "corrected" price - a real one-day
    move should never be quietly overwritten, and a genuine outlier is
    exactly the kind of thing a fund manager needs to see, not have hidden.
    It only flags: if the boundary Close differs from the median of the
    `window` trading days on either side of it (from data already fetched -
    no extra network call) by more than `outlier_ratio`, `is_outlier=True`
    is returned alongside the (unmodified) value, so the caller can surface
    a "verify this" note in Return Status.

    Returns (value, date_used, is_outlier) or (None, None, False) if there's
    no trading day on/before target_ts at all.
    """
    if window is None:
        window = config.ATTRIBUTION_OUTLIER_WINDOW_DAYS
    if outlier_ratio is None:
        outlier_ratio = config.ATTRIBUTION_OUTLIER_RATIO

    asof = closes.loc[:target_ts]
    if asof.empty:
        return None, None, False

    date_used = asof.index[-1]
    value = float(asof.iloc[-1])

    pos = closes.index.get_loc(date_used)
    lo = max(0, pos - window)
    hi = min(len(closes), pos + window + 1)
    neighbourhood = closes.iloc[lo:hi].drop(index=date_used, errors="ignore")

    is_outlier = False
    if len(neighbourhood) >= 2:
        local_median = float(neighbourhood.median())
        if local_median > 0 and abs(value / local_median - 1) > outlier_ratio:
            is_outlier = True

    return value, date_used, is_outlier


def fetch_stock_returns(holdings: pd.DataFrame, start, end) -> pd.DataFrame:
    """
    Pull each holding's own return over [start, end] from Yahoo Finance,
    keyed by ISIN via the 'Yahoo Ticker' column that should already be
    merged onto `holdings`.

    Return = (Close as-of `end` / Close as-of `start`) - 1, using the raw
    (unadjusted) Close price - see the auto_adjust=False below, which
    matches a manual look-up of the day's closing price rather than a
    dividend-adjusted one.

    "As-of" matters here: `start`/`end` are calendar dates (e.g. a
    Weightage month-end like "2026-05-31"), which often fall on a weekend
    or market holiday - NOT a trading day. yfinance's `start` is a
    forward-only lower bound, so requesting data starting exactly on a
    non-trading `start` date silently rolls forward to the NEXT trading
    day's close (e.g. 1 June) instead of the correct LAST trading day
    on/before it (e.g. 29 May) - using a price from after the intended
    boundary instead of on/before it. This under/overstates the return by
    however much the stock moved on the skipped days, in either direction
    depending on the stock - exactly the kind of stock-specific mismatch
    that showed up against a manual calculation. To fix this, a buffered
    window is fetched and the actual start/end closes are looked up
    explicitly as "last available close on or before that date", rather
    than assumed to be the first/last row of whatever yfinance returns.

    All not-yet-cached tickers are fetched together in one batched request
    rather than one request per ticker. Falls back to 0% return (flagged)
    for any ticker that fails to fetch, so one bad ticker never crashes the
    attribution step.

    Returns: ISIN | Stock Return | Return Status. "Return Status" says WHY
    a holding got the return it did - "Cash (exempt)", "No Yahoo Ticker"
    (ISIN never resolved against a Security Master file), "Cached",
    "Fetched (live)", or "Fetch Failed - <reason>" (the actual
    yfinance/network error, when the whole batch call raised). This is
    what lets a 0% that's a genuine flat return be told apart from a 0%
    that's really "the fetch didn't work" - both dashboard.py and
    report_generator.py surface this column directly.
    """
    cache = _load_return_cache()

    # Map each ticker to the ISIN(s) that use it (normally one-to-one, but
    # this stays correct even if two holdings somehow share a ticker).
    ticker_to_isins = {}
    for _, row in holdings.iterrows():
        if row["ISIN"] == "CASH":
            continue
        ticker = row.get("Yahoo Ticker", None)
        if _valid_ticker(ticker):
            ticker_to_isins.setdefault(ticker, []).append(row["ISIN"])

    # A cached value of None means an EARLIER run tried this ticker/window
    # and failed (bad batch, rate limit, delisted/renamed ticker, etc.) - it
    # is NOT a genuinely resolved result. Treating "key present in cache" as
    # "done" (the old check below) locks that failure in forever, since
    # nothing ever overwrites a key that's already there - the batch that
    # failed for e.g. an older month's window keeps reporting 0% return for
    # every one of its tickers on every future run too. A cached None is
    # therefore retried, exactly like a ticker that was never attempted.
    tickers_to_fetch = [
        t for t in ticker_to_isins if cache.get(_cache_key(t, start, end)) is None
    ]

    # Per-ticker outcome for THIS call, used to build the "Return Status"
    # column below. Tickers that were already in the cache (i.e. never
    # entered tickers_to_fetch) are reported as "Cached"; anything that
    # actually went through a live yfinance call this run is reported as
    # either "Fetched (live)" or "Fetch Failed - <reason>", so the reason a
    # holding is 0% is always visible, not just logged to a console no one
    # is watching.
    ticker_outcome: dict = {}
    batch_failure_reason = None

    if tickers_to_fetch:
        print(f"    Fetching price history for {len(tickers_to_fetch)} ticker(s) in a single "
              f"batched request (instead of one request per stock)...")
        start_ts = pd.Timestamp(start)
        end_ts = pd.Timestamp(end)
        try:
            # Buffer well before `start` (long weekends/festival holiday
            # clusters can run several trading days) so there's always an
            # actual trading day on/before `start_ts` inside the fetched
            # range to look up - see the as-of explanation above. `end` is
            # still pushed out by a day since yfinance's `end` is exclusive
            # (like a Python slice), so the requested end date's own close
            # isn't silently dropped. The buffer also has to be wide enough
            # either side of both boundaries for the outlier sanity check
            # below (_asof_close) to have a real local neighbourhood to
            # compare against - see config.ATTRIBUTION_LOOKBACK_BUFFER_DAYS.
            data = robust_yf_download(
                tickers=tickers_to_fetch,
                start=start_ts - pd.Timedelta(days=config.ATTRIBUTION_LOOKBACK_BUFFER_DAYS),
                end=end_ts + pd.Timedelta(days=config.ATTRIBUTION_LOOKBACK_BUFFER_DAYS),
                group_by="ticker",
                threads=True,
                progress=False,
                auto_adjust=False,
            )
        except Exception as exc:  # noqa: BLE001 - any network/parse failure
            batch_failure_reason = str(exc) or exc.__class__.__name__
            print(f"    Warning: batched price fetch failed ({batch_failure_reason}); all "
                  f"{len(tickers_to_fetch)} ticker(s) in this batch will default to 0% return.")
            data = None

        for ticker in tickers_to_fetch:
            stock_return = None
            per_ticker_reason = None
            outlier_flags = []
            detail = {}
            try:
                if data is not None and not data.empty:
                    # yf.download(..., group_by="ticker") ALWAYS returns a
                    # (Ticker, Price) MultiIndex DataFrame - regardless of
                    # how many tickers were requested. Older yfinance
                    # releases only flattened this down to plain columns
                    # when `multi_level_index=False` was passed AND exactly
                    # one ticker was requested; this code never passes
                    # multi_level_index, so it defaults to True and the
                    # MultiIndex is kept even for a batch of one. Guessing
                    # "single ticker -> flat columns" from len(tickers_to_
                    # fetch) was therefore wrong: a batch of exactly one
                    # ticker (very common for a fund's earliest, most
                    # concentrated months) hit `data["Close"]` directly,
                    # raised a KeyError that this function's except clause
                    # quietly swallowed, and silently returned Stock Return
                    # = 0% - the source of "every stock shows zero
                    # contribution" for those months. Detect the actual
                    # column shape instead of assuming it from the count.
                    if isinstance(data.columns, pd.MultiIndex):
                        if ticker not in data.columns.get_level_values(0):
                            per_ticker_reason = "ticker missing from Yahoo response"
                            raise KeyError(ticker)
                        hist = data[ticker]
                    else:
                        hist = data
                    closes = hist["Close"].dropna().sort_index()

                    # As-of lookups: the last available close ON OR BEFORE
                    # each boundary date (NOT the first/last row of the
                    # whole fetched range - see docstring above), each
                    # cross-checked against its own local neighbourhood so a
                    # single bad/thin print doesn't get reported with the
                    # same confidence as a genuine, corroborated price - see
                    # _asof_close().
                    start_val, start_date, start_outlier = _asof_close(closes, start_ts)
                    end_val, end_date, end_outlier = _asof_close(closes, end_ts)

                    if start_val is not None and end_val is not None:
                        stock_return = float(end_val / start_val - 1)
                        detail = {
                            "start_date": str(start_date.date()), "start_close": start_val,
                            "end_date": str(end_date.date()), "end_close": end_val,
                        }
                        if start_outlier:
                            outlier_flags.append(f"start price on {start_date.date()}")
                        if end_outlier:
                            outlier_flags.append(f"end price on {end_date.date()}")
                    else:
                        per_ticker_reason = "no trading day on/before the start or end date"
                elif data is None:
                    per_ticker_reason = batch_failure_reason or "batched fetch failed"
                else:
                    per_ticker_reason = "Yahoo Finance returned an empty dataset for this batch"
            except Exception as exc:  # noqa: BLE001 - deliberately broad: this loop
                # runs once per ticker in the batch, and a shape/type problem
                # specific to ONE ticker's data (e.g. a stray tz-aware index
                # from Yahoo colliding with the naive start_ts/end_ts here)
                # must not propagate out and abort every other ticker in the
                # same batch - it should fall back to "Fetch Failed" for just
                # that ticker, same as any other per-ticker data problem.
                stock_return = None
                per_ticker_reason = per_ticker_reason or f"{exc.__class__.__name__}: {exc}"

            # Only cache an actual resolved value. Writing None here would
            # be exactly the bug this fix removes: a transient failure
            # would get baked into the cache file and never get a chance to
            # succeed on a later run (see the tickers_to_fetch comment
            # above) - so a failure is simply left out of the cache instead,
            # which naturally makes it "not yet fetched" next time.
            if stock_return is not None:
                cache[_cache_key(ticker, start, end)] = {"return": stock_return, **detail}
                if outlier_flags:
                    ticker_outcome[ticker] = (
                        "Fetched (live) - \u26a0 Verify: unusual "
                        + " and ".join(outlier_flags)
                        + f" (>{config.ATTRIBUTION_OUTLIER_RATIO:.0%} off its own nearby days - "
                          "check for a corporate action, thin trading, or a bad print before "
                          "trusting this number)"
                    )
                else:
                    ticker_outcome[ticker] = "Fetched (live)"
            else:
                ticker_outcome[ticker] = f"Fetch Failed - {per_ticker_reason}"

        _save_return_cache(cache)

    rows = []
    for _, row in holdings.iterrows():
        # Cash has no ticker and no market return by definition - it's exempt
        # from the fetch entirely and simply contributes 0% (a simplifying
        # assumption; it ignores any interest/dividend the cash may earn).
        if row["ISIN"] == "CASH":
            rows.append({"ISIN": row["ISIN"], "Stock Return": 0.0, "Return Status": "Cash (exempt)",
                         "Return Start Date": None, "Return Start Close": None,
                         "Return End Date": None, "Return End Close": None})
            continue

        ticker = row.get("Yahoo Ticker", None)
        stock_return = None
        start_date = start_close = end_date = end_close = None
        status = "No Yahoo Ticker (ISIN not resolved against a Security Master file)"
        if _valid_ticker(ticker):
            cached = cache.get(_cache_key(ticker, start, end))
            if isinstance(cached, dict):
                stock_return = cached.get("return")
                start_date, start_close = cached.get("start_date"), cached.get("start_close")
                end_date, end_close = cached.get("end_date"), cached.get("end_close")
            elif isinstance(cached, (int, float)):
                # Defensive: a v2-or-earlier cache entry that somehow still
                # matched (shouldn't happen given the "v3|" key bump, but
                # cheap to handle gracefully rather than raise on it).
                stock_return = cached
            status = ticker_outcome.get(ticker, "Cached" if stock_return is not None else "No data")
        rows.append({
            "ISIN": row["ISIN"], "Stock Return": stock_return, "Return Status": status,
            "Return Start Date": start_date, "Return Start Close": start_close,
            "Return End Date": end_date, "Return End Close": end_close,
        })

    returns_df = pd.DataFrame(rows)
    missing = returns_df["Stock Return"].isna()
    if missing.any():
        print(
            f"    Warning: could not fetch a return for {missing.sum()} holding(s) "
            "(excluding Cash, which is exempt by design); treating as 0% for "
            "attribution purposes."
        )
    returns_df["Stock Return"] = returns_df["Stock Return"].fillna(0.0)
    return returns_df


def stock_contribution(holdings: pd.DataFrame) -> pd.DataFrame:
    """
    holdings must contain: Stock Name, ISIN, Current Weight (%), Sector, Stock Return.
    Contribution = Weight (as a fraction) x Return.

    "Return Status" (see fetch_stock_returns) is carried through if present,
    so the report/dashboard can show WHY a 0% contribution is 0% - a
    genuine flat return, an unresolved ISIN, or a failed fetch - rather
    than a bare, unexplained zero.
    """
    df = holdings.copy()
    df["Contribution"] = (df["Current Weight"] / 100.0) * df["Stock Return"]
    cols = ["Stock Name", "ISIN", "Current Weight", "Stock Return", "Sector", "Contribution"]
    if "Return Status" in df.columns:
        cols.append("Return Status")
    for audit_col in ("Return Start Date", "Return Start Close", "Return End Date", "Return End Close"):
        if audit_col in df.columns:
            cols.append(audit_col)
    return df[cols].sort_values("Contribution", ascending=False).reset_index(drop=True)


def sector_contribution(stock_contrib_df: pd.DataFrame) -> pd.DataFrame:
    """Group stock-level contribution by sector, e.g. Technology = TCS + Infosys + HCL Tech."""
    grouped = (
        stock_contrib_df.groupby("Sector", as_index=False)
        .agg(**{
            "Contribution": ("Contribution", "sum"),
            "Weight": ("Current Weight", "sum"),
            "Holdings": ("Stock Name", "count"),
        })
        .sort_values("Contribution", ascending=False)
        .reset_index(drop=True)
    )
    return grouped


# ---------------------------------------------------------------------------
# Month-by-month best/worst contributors
# ---------------------------------------------------------------------------
# Separate from stock_contribution()/sector_contribution() above, which score
# a fund's holdings against ITS OWN "since [window start]" return (e.g. the
# whole "Since Inception" or "Last 1Y" window). This section instead scores
# every MONTH-END snapshot against just that one month's own return, so a
# fund manager can see which holdings drove (or dragged) performance in any
# given month, not only over the whole analysis window.
def compute_stock_contributions_for_month(
    fund_weightage: pd.DataFrame,
    mapping: pd.DataFrame,
    month_end,
    previous_month_end=None,
) -> pd.DataFrame:
    """
    Best/worst-contributor view for a SINGLE month-end snapshot: each
    holding's contribution to the fund's return over just that one month,
    using that month-end's Current Weight and the stock's own price return
    from the previous month-end to this one (reuses fetch_stock_returns()
    above on a one-month window, instead of the fund's full analysis
    window, so results and caching behave exactly like the existing
    attribution fetch - just with a narrower [start, end]).

    fund_weightage: this ONE fund's full Weightage history (every month-end
        since inception - e.g. `weightage[weightage["Fund Code"] == code]`).
    mapping: ISIN -> Yahoo Ticker (data_loader.resolve_mapping()'s output).
    month_end: the month-end snapshot date to score.
    previous_month_end: the prior month-end to measure each stock's monthly
        return from. Defaults to the month-end immediately before
        `month_end` in `fund_weightage`. If there isn't one (this is the
        fund's very first snapshot), an empty frame is returned - a monthly
        return needs an earlier point to measure from.

    Returns: Stock Name | ISIN | Current Weight | Stock Return | Contribution
    | Return Status, sorted by Contribution descending. Cash is excluded -
    it contributes 0% by definition (see fetch_stock_returns) and isn't a
    meaningful "best/worst contributor" to rank alongside real holdings.

    Contribution = Current Weight (already in %, e.g. 6.5 meaning 6.5%) x
    Stock Return (decimal fraction, e.g. 0.10 meaning 10%) - NOT divided by
    100 first. The result is therefore already in percentage-point units
    (e.g. 0.65 means "contributed 0.65% to the fund's return"), matching a
    manual weight% x return calculation - it should be displayed with a
    plain "+.2f" + literal "%" format, not a percent-conversion format that
    would multiply it by 100 again.
    """
    month_end = pd.Timestamp(month_end)

    if previous_month_end is None:
        earlier_dates = fund_weightage.loc[fund_weightage["Date"] < month_end, "Date"]
        if earlier_dates.empty:
            return pd.DataFrame(
                columns=["Stock Name", "ISIN", "Current Weight", "Stock Return",
                         "Contribution", "Return Status", "Return Start Date",
                         "Return Start Close", "Return End Date", "Return End Close"]
            )
        previous_month_end = earlier_dates.max()
    else:
        previous_month_end = pd.Timestamp(previous_month_end)

    snapshot = fund_weightage[fund_weightage["Date"] == month_end].copy()
    holdings = snapshot.merge(mapping[["ISIN", "Yahoo Ticker"]], on="ISIN", how="left")

    stock_returns = fetch_stock_returns(holdings, start=previous_month_end, end=month_end)
    holdings = holdings.merge(stock_returns, on="ISIN", how="left")
    holdings = holdings[holdings["ISIN"] != "CASH"].copy()

    holdings["Contribution"] = holdings["Current Weight"] * holdings["Stock Return"]
    return holdings[["Stock Name", "ISIN", "Current Weight", "Stock Return",
                      "Contribution", "Return Status", "Return Start Date",
                      "Return Start Close", "Return End Date", "Return End Close"]] \
        .sort_values("Contribution", ascending=False) \
        .reset_index(drop=True)


def compute_monthly_contributions(fund_weightage: pd.DataFrame, mapping: pd.DataFrame) -> dict:
    """
    Runs compute_stock_contributions_for_month() for every month-end in
    `fund_weightage` that has an earlier snapshot to measure a monthly
    return against (i.e. every month except the fund's very first).

    Returns {month_end_timestamp: {"data": contribution_df, "window_start":
    previous_month_end, "window_end": month_end}}, sorted chronologically -
    the window dates are carried alongside the data so a caller (e.g. the
    dashboard) can show exactly which period each stock's return was
    measured over, without having to re-derive the previous month-end
    itself. Each network fetch inside the loop is for just that one month's
    tickers over a one-month window, and goes through the same disk cache
    (config.RETURN_CACHE_FILE) as every other attribution.py call, so
    re-running this for a fund already reviewed is effectively free.
    """
    month_ends = sorted(fund_weightage["Date"].unique())
    results = {}
    for i in range(1, len(month_ends)):
        month_end = pd.Timestamp(month_ends[i])
        previous_month_end = pd.Timestamp(month_ends[i - 1])
        data = compute_stock_contributions_for_month(
            fund_weightage, mapping, month_end=month_end, previous_month_end=previous_month_end,
        )
        results[month_end] = {
            "data": data, "window_start": previous_month_end, "window_end": month_end,
        }
    return results


def top_bottom_contributors(contrib_df: pd.DataFrame, n: int = 5) -> tuple:
    """
    Split a contribution DataFrame (as returned by
    compute_stock_contributions_for_month) into its best and worst `n`
    contributors for that month.

    Returns (top_df, bottom_df), each sorted so the most extreme
    contribution is first (top: highest positive first; bottom: most
    negative first). If the fund holds fewer than 2n securities that month,
    top and bottom may share rows - a natural consequence of a small or
    concentrated portfolio, not a bug.
    """
    ranked = contrib_df.sort_values("Contribution", ascending=False).reset_index(drop=True)
    top = ranked.head(n).reset_index(drop=True)
    bottom = ranked.tail(n).sort_values("Contribution").reset_index(drop=True)
    return top, bottom


def all_fetches_failed(contrib_df: pd.DataFrame) -> str | None:
    """
    If every non-cash holding in `contrib_df` has a "Fetch Failed - ..."
    Return Status (i.e. the whole batch call failed, not just an isolated
    bad ticker), returns the underlying reason string so the caller can
    surface it prominently (e.g. st.error in dashboard.py) instead of the
    user seeing an unexplained wall of 0.00% contributions. Returns None if
    at least one holding's return was genuinely fetched/cached, or if the
    DataFrame has no "Return Status" column at all.
    """
    if "Return Status" not in contrib_df.columns or contrib_df.empty:
        return None
    statuses = contrib_df["Return Status"].astype(str)
    failed = statuses.str.startswith("Fetch Failed")
    if not failed.all():
        return None
    # Reason text after "Fetch Failed - "
    first = statuses[failed].iloc[0]
    return first.split(" - ", 1)[1] if " - " in first else first
