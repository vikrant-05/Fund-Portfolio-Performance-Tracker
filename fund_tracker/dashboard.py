"""
dashboard.py (optional)
------------------------
A lightweight Streamlit front-end over the same pipeline used by main.py.

Adding a fund
--------------
A firm isn't limited to one Weightage/NAV file - the sidebar's "Add a fund"
uploader lets the user pick another fund's Weightage + Daily NAV Excel files
(same column headers as every other file) straight from the browser. They're
saved into Inputs/ and registered via config.add_fund_files(), so the new
fund's code shows up in the "Fund" dropdown below immediately after.

Removing a fund
----------------
The sidebar's "Remove a fund" expander is the inverse: it lists every
configured Weightage/Daily NAV file pair with a "Remove" button, which un-
registers that pair via config.remove_fund_files() (see file_manager.py).
The underlying files are left untouched on disk - this only stops the app
from loading them, so any fund code that lived only inside them stops
appearing (fund dropdown, reports, etc.) from the next load onward.

Rebalancing
------------
There's no manager-entered target weight anymore. The drift threshold
slider instead flags any holding whose Current Weight has moved by more
than that many percentage points versus its Previous month-end Weight -
see rebalance.compute_weight_drift().

File management note
----------------------
Streamlit runs as a local web app, but it's still a process on the user's
own machine when launched with `streamlit run dashboard.py`, so the same
file_manager.py / config.py used by main.py works here too - Security
Master files load silently unless updated below.

Run:
    streamlit run dashboard.py
"""

from pathlib import Path

import altair as alt
import pandas as pd
import streamlit as st

import attribution
import config
import data_loader
import performance
import rebalance
import report_generator
import yahoo_fetch

st.set_page_config(page_title="Fund/Portfolio Performance Tracker", layout="wide")
st.title("Fund / Portfolio Performance Tracker")


def _nav_vs_benchmark_chart(daily: pd.DataFrame):
    """
    Dual-axis NAV vs Benchmark line chart. Portfolio NAV (per-unit, usually
    tens-to-hundreds) and Benchmark NAV (an index level, often thousands-
    to-tens-of-thousands) live on very different scales - Streamlit's
    st.line_chart shares one y-axis across columns, which makes Portfolio
    NAV look like a flat line at zero next to a much larger benchmark
    index. Altair's independent-scale layering keeps both series' actual
    shapes visible, each on its own axis.
    """
    df = (
        daily[["Date", "Portfolio NAV", "Benchmark NAV"]]
        .dropna(subset=["Portfolio NAV", "Benchmark NAV"])
        .drop_duplicates(subset=["Date"])
        .sort_values("Date")
    )
    base = alt.Chart(df).encode(x=alt.X("Date:T", title=None))
    port_line = base.mark_line(color="#1F4E78").encode(
        y=alt.Y("Portfolio NAV:Q", axis=alt.Axis(title="Portfolio NAV", titleColor="#1F4E78")),
        tooltip=["Date:T", "Portfolio NAV:Q"],
    )
    bench_line = base.mark_line(color="#C0392B", strokeDash=[4, 2]).encode(
        y=alt.Y("Benchmark NAV:Q", axis=alt.Axis(title="Benchmark NAV", titleColor="#C0392B")),
        tooltip=["Date:T", "Benchmark NAV:Q"],
    )
    return alt.layer(port_line, bench_line).resolve_scale(y="independent")


def _growth_chart(daily: pd.DataFrame):
    """
    Cumulative-return growth chart (Portfolio vs Benchmark, both in %,
    rebased to 0% at the start of whatever window `daily` covers). Unlike
    the raw NAV chart above, both series are already on a comparable scale
    here, so a single shared axis is correct and preferred.
    """
    df = (
        daily[["Date", "Cumulative Portfolio Return", "Cumulative Benchmark Return"]]
        .dropna(subset=["Cumulative Portfolio Return", "Cumulative Benchmark Return"])
        .drop_duplicates(subset=["Date"])
        .sort_values("Date")
        .rename(columns={
            "Cumulative Portfolio Return": "Portfolio",
            "Cumulative Benchmark Return": "Benchmark",
        })
        .set_index("Date")
    )
    return df

# --- file management sidebar ------------------------------------------------
with st.sidebar.expander("Configured files", expanded=False):
    st.caption("Portfolio files are re-checked (not re-prompted) every run; "
               "Security Master files load silently unless updated below.")
    st.markdown("**Weightage file(s):**")
    for p in config.get_weightage_files():
        st.text(f"  {p}")
    st.markdown("**Daily NAV file(s):**")
    for p in config.get_nav_files():
        st.text(f"  {p}")
    st.text(f"NSE Master: {config.get_nse_security_master()}")
    st.text(f"BSE Master: {config.get_bse_security_master()}")

if st.sidebar.button("Update Security Master Files"):
    config.update_security_masters()
    st.cache_data.clear()
    st.sidebar.success("Security Master files updated.")
    st.rerun()

st.sidebar.markdown("---")

# --- add a fund --------------------------------------------------------------
with st.sidebar.expander("Add a fund", expanded=False):
    st.caption(
        "Upload a new fund's Weightage and Daily NAV Excel files (same column "
        "headers as the existing ones). Once added, its fund code(s) appear "
        "in the dropdown below alongside every fund already configured."
    )
    new_weightage_upload = st.file_uploader("Weightage file (.xlsx)", type=["xlsx", "xls"],
                                             key="new_weightage_upload")
    new_nav_upload = st.file_uploader("Daily NAV file (.xlsx)", type=["xlsx", "xls"],
                                       key="new_nav_upload")

    if st.button("Add fund"):
        if not new_weightage_upload or not new_nav_upload:
            st.sidebar.error("Please choose both a Weightage file and a Daily NAV file.")
        else:
            dest_weightage = config.INPUT_DIR / new_weightage_upload.name
            dest_nav = config.INPUT_DIR / new_nav_upload.name
            dest_weightage.write_bytes(new_weightage_upload.getvalue())
            dest_nav.write_bytes(new_nav_upload.getvalue())

            problems = (
                data_loader.validate_weightage_file(dest_weightage)
                + data_loader.validate_nav_file(dest_nav)
            )
            if problems:
                st.sidebar.error(
                    "Couldn't add this fund - the uploaded file(s) don't match the "
                    "expected format:\n" + "\n".join(f"- {p}" for p in problems)
                )
            else:
                config.add_fund_files(dest_weightage, dest_nav)
                st.cache_data.clear()
                st.sidebar.success(
                    f"Added {new_weightage_upload.name} / {new_nav_upload.name}."
                )
                st.rerun()

st.sidebar.markdown("---")

# --- remove a fund -------------------------------------------------------
with st.sidebar.expander("Remove a fund", expanded=False):
    st.caption(
        "Un-registers a Weightage/Daily NAV file pair from this tool. The "
        "files themselves are NOT deleted from disk - this only stops the "
        "app from loading them, so any fund code that lived only inside "
        "them stops appearing below from the next load onward."
    )
    weightage_paths = config.get_weightage_files()
    nav_paths = config.get_nav_files()

    if len(weightage_paths) <= 1 and len(nav_paths) <= 1:
        st.caption("Only one fund file pair is configured - nothing to remove "
                   "(the app needs at least one Weightage and Daily NAV file).")
    elif len(weightage_paths) == len(nav_paths):
        # Normal case: each Weightage/NAV pair was added together via
        # add_fund_files(), so list position i pairs them up.
        for i, (w, n) in enumerate(zip(weightage_paths, nav_paths)):
            col1, col2 = st.columns([4, 1])
            col1.text(f"{w.name}\n{n.name}")
            if col2.button("Remove", key=f"remove_pair_{i}"):
                config.remove_fund_files(w, n)
                st.cache_data.clear()
                st.sidebar.success(f"Removed {w.name} / {n.name}.")
                st.rerun()
    else:
        # Weightage/NAV counts don't line up 1:1 (e.g. config.json was hand-
        # edited) - fall back to removing each file individually.
        st.caption("Weightage/NAV file counts don't match up 1:1 - remove "
                   "each file individually below.")
        st.markdown("**Weightage file(s):**")
        for w in weightage_paths:
            col1, col2 = st.columns([4, 1])
            col1.text(w.name)
            if col2.button("Remove", key=f"remove_w_{w}"):
                config.remove_weightage_file(w)
                st.cache_data.clear()
                st.sidebar.success(f"Removed {w.name}.")
                st.rerun()
        st.markdown("**Daily NAV file(s):**")
        for n in nav_paths:
            col1, col2 = st.columns([4, 1])
            col1.text(n.name)
            if col2.button("Remove", key=f"remove_n_{n}"):
                config.remove_nav_file(n)
                st.cache_data.clear()
                st.sidebar.success(f"Removed {n.name}.")
                st.rerun()

st.sidebar.markdown("---")


@st.cache_data(show_spinner="Loading and validating inputs...")
def _load_data():
    return data_loader.load_all()


@st.cache_data(show_spinner="Fetching sector data from Yahoo Finance...")
def _load_sectors(_mapping):
    return yahoo_fetch.fetch_sector_data(_mapping)


try:
    fund_data = _load_data()
except (FileNotFoundError, ValueError) as exc:
    st.error(str(exc))
    st.stop()

sector_data = _load_sectors(fund_data.mapping)
fund_codes = data_loader.get_fund_codes(fund_data)

fund_code = st.sidebar.selectbox("Fund", fund_codes)

# The Weightage file has one row per holding per month-end since inception,
# so let the user pick which month's snapshot to analyze (default: latest).
available_dates = sorted(
    fund_data.weightage.loc[fund_data.weightage["Fund Code"] == fund_code, "Date"].dt.date.unique(),
    reverse=True,
)
snapshot_date = st.sidebar.selectbox(
    "Weightage snapshot (month-end)", available_dates,
    format_func=lambda d: d.strftime("%b %Y"),
)

threshold = st.sidebar.slider("Rebalance drift threshold (percentage points)", 1.0, 10.0,
                               config.DEFAULT_REBALANCE_THRESHOLD, 0.5)
st.sidebar.caption(
    "Flags any holding whose weight has moved by more than this many "
    "percentage points versus the previous month-end snapshot."
)

fund_weightage = data_loader.latest_snapshot(fund_data.weightage, fund_code, as_of=str(snapshot_date))
fund_nav = fund_data.nav[fund_data.nav["Fund Code"] == fund_code].copy()
fund_name = fund_weightage["Fund Name"].iloc[0]
st.subheader(f"{fund_name} ({fund_code}) - {snapshot_date.strftime('%b %Y')} snapshot")

holdings = fund_weightage.merge(fund_data.mapping[["ISIN", "Yahoo Ticker"]], on="ISIN", how="left")
holdings = yahoo_fetch.merge_sector_with_holdings(holdings, sector_data)

perf = performance.compute_fund_performance(fund_nav)
s = perf["summary"]

# Absolute Return / CAGR / Alpha / Max Drawdown, broken out by period rather
# than only "since inception" - each tab is computed over its own trailing
# window (see performance.compute_multi_period_performance), anchored on the
# fund's latest available NAV date. Each tab also shows ITS OWN NAV-vs-
# benchmark and growth chart, computed fresh over just that window (via
# ps["Daily"]) rather than always showing the same since-inception chart
# regardless of which period is selected.
period_perf = performance.compute_multi_period_performance(fund_nav)
period_tabs = st.tabs(performance.PERIOD_ORDER)
for label, tab in zip(performance.PERIOD_ORDER, period_tabs):
    with tab:
        ps = period_perf[label]
        if not ps["Available"]:
            st.info(f"{label}: {ps['Reason']}.")
            continue
        if ps["Truncated"]:
            st.caption(
                f"Fund history only goes back to {ps['Window Start'].date()} for this "
                f"snapshot, so this is not a full {label.replace('Last ', '').replace('Current FY (Apr-Mar)', 'financial year')} "
                f"window - shown as-is rather than padded."
            )
        pcol1, pcol2, pcol3, pcol4 = st.columns(4)
        pcol1.metric("Absolute Return", f"{ps['Absolute Return']:.2%}")
        # A window under ~1 year (almost always true for "Current FY" early
        # in the financial year, and possibly "Since Inception" for a young
        # fund) can't be annualised into a meaningful CAGR - compounding a
        # short-window return as if it repeated for a full year wildly
        # inflates it (e.g. a real +10% over 3 months would show as ~46%).
        # performance.py returns CAGR=NaN with "CAGR Annualised"=False for
        # exactly these windows - show the plain Absolute Return instead of
        # an annualised number in that case.
        if ps.get("CAGR Annualised", True):
            pcol2.metric("CAGR", f"{ps['CAGR']:.2%}")
        else:
            pcol2.metric("CAGR", "N/A", help="Window is under 1 year - an annualised "
                          "CAGR would be misleading here. See Absolute Return instead.")
        if ps.get("CAGR Annualised", True):
            pcol3.metric("Alpha", f"{ps['Alpha']:.2%}")
        else:
            pcol3.metric("Alpha", "N/A", help="Not meaningful over a window under 1 year "
                         "(Alpha is built from annualised CAGRs).")
        pcol4.metric("Max Drawdown", f"{ps['Maximum Drawdown']:.2%}")
        st.caption(f"Window: {ps['Window Start'].date()} to {ps['Window End'].date()}")

        period_daily = ps["Daily"]
        if period_daily.empty or (period_daily[["Portfolio NAV", "Benchmark NAV"]] == 0).all().all():
            st.warning(
                "No valid Portfolio/Benchmark NAV data to chart for this window - check "
                "the Daily NAV file for non-numeric or missing values."
            )
        else:
            chart_col1, chart_col2 = st.columns(2)
            with chart_col1:
                st.caption("NAV vs Benchmark (each on its own axis)")
                st.altair_chart(_nav_vs_benchmark_chart(period_daily), use_container_width=True)
            with chart_col2:
                st.caption(f"Growth (rebased to 0% at {ps['Window Start'].date()})")
                st.line_chart(_growth_chart(period_daily))

# --- weightage + automatic drift flagging (no manual target entry) ----------
st.markdown("### Portfolio Weightage")
st.caption(
    "Current holding weights for this snapshot. Rows highlighted in red have "
    "moved by more than the drift threshold (in the sidebar) versus the "
    "previous month-end snapshot."
)

previous_holdings = data_loader.previous_snapshot(fund_data.weightage, fund_code, before_date=snapshot_date)
rebalance_df = rebalance.compute_weight_drift(holdings, previous_holdings, threshold=threshold)

if previous_holdings is None:
    st.info("This is the earliest available snapshot for this fund - every holding is "
            "shown as new (no prior month to compare drift against).")

rebalance_display = rebalance_df.copy()
rebalance_display.index = range(1, len(rebalance_display) + 1)  # row numbers start at 1, not 0

st.dataframe(
    rebalance_display.style.apply(
        lambda r: ["background-color: #FCE4E4" if r["Rebalance Required"] else "" for _ in r],
        axis=1,
    ).format({"Current Weight": "{:.2f}%", "Previous Weight": "{:.2f}%", "Drift": "{:+.2f}pp"}),
    use_container_width=True,
)

st.markdown("---")
st.markdown("### Monthly Best & Worst Contributors")
st.caption(
    "Separate from the Portfolio Weightage table above, which shows weight "
    "drift, not return. Pick a month-end snapshot below, then fetch - only "
    "THAT month's holdings get their price history pulled from Yahoo "
    "Finance (that month's weight x the stock's own price return from the "
    "previous month-end to this one), not the fund's whole history, so "
    "there's no waiting on months you're not looking at. The earliest "
    "snapshot is excluded (no prior month-end to measure a monthly return "
    "against)."
)

# Same month-end dates as the Weightage snapshot picker in the sidebar,
# minus the fund's very first snapshot.
contributor_dates = [d for d in available_dates if d != min(available_dates)]

if not contributor_dates:
    st.info("This fund only has one month-end snapshot so far - nothing to compare yet.")
else:
    contrib_month = st.selectbox(
        "Month", contributor_dates, format_func=lambda d: d.strftime("%b %Y"),
        key="contrib_month_select",
    )
    contrib_month_ts = pd.Timestamp(contrib_month)

    @st.cache_data(show_spinner="Fetching this month's stock price history from Yahoo Finance...")
    def _load_month_contributions(_fund_weightage, _mapping, fund_code, month_end_ts):
        # fund_code/month_end_ts are the actual Streamlit cache key
        # (hashable); the leading-underscore DataFrame args are excluded
        # from hashing but are what the computation uses. Cached per
        # (fund, month) - switching back to an already-fetched month is
        # instant, and picking a new month only ever fetches that month's
        # tickers, never the whole fund history.
        fund_only = _fund_weightage[_fund_weightage["Fund Code"] == fund_code]
        previous_snap = data_loader.previous_snapshot(fund_only, fund_code, before_date=month_end_ts)
        previous_month_end = previous_snap["Date"].iloc[0] if previous_snap is not None else None
        contrib_df = attribution.compute_stock_contributions_for_month(
            fund_only, _mapping, month_end=month_end_ts, previous_month_end=previous_month_end,
        )
        return contrib_df, previous_month_end

    def _contributor_bar_chart(df: pd.DataFrame, color: str):
        # Contribution is already in percentage-point units (Weight% x
        # Return decimal, not divided by 100 - see attribution.py), so the
        # axis/tooltip use a plain numeric format here, NOT Vega-Lite's "%"
        # format (which would multiply the value by 100 again and show
        # e.g. "65%" instead of the correct "0.65%").
        chart_df = df[["Stock Name", "Contribution"]].copy()
        return (
            alt.Chart(chart_df)
            .mark_bar(color=color)
            .encode(
                x=alt.X("Contribution:Q", title="Contribution (%)", axis=alt.Axis(format=".2f")),
                y=alt.Y("Stock Name:N", sort="-x", title=None),
                tooltip=["Stock Name", alt.Tooltip("Contribution:Q", format="+.2f", title="Contribution (%)")],
            )
            .properties(height=32 * max(len(chart_df), 1))
        )

    if st.button("Fetch contributors for this month"):
        month_df, previous_month_end = _load_month_contributions(
            fund_data.weightage, fund_data.mapping, fund_code, contrib_month_ts,
        )
        st.session_state["monthly_contrib_result"] = (contrib_month_ts, month_df, previous_month_end)

    result = st.session_state.get("monthly_contrib_result")
    if result is not None and result[0] == contrib_month_ts:
        _, month_df, previous_month_end = result
        if month_df.empty:
            st.info("No monthly return could be computed for this snapshot.")
        else:
            st.caption(
                f"Stock returns calculated from **{previous_month_end.date()}** to "
                f"**{contrib_month_ts.date()}**."
            )

            # A 0.00% contribution can mean two very different things: the
            # stock genuinely didn't move, or its return simply couldn't be
            # fetched (Yahoo Finance blocked/rate-limited/unreachable, or an
            # unresolved ISIN). attribution.fetch_stock_returns() now tags
            # every holding with a "Return Status" for exactly this reason -
            # if EVERY holding this month failed the fetch (not just an
            # isolated bad ticker), say so loudly instead of quietly
            # rendering a wall of unexplained zeros.
            failure_reason = attribution.all_fetches_failed(month_df)
            if failure_reason:
                st.error(
                    f"Every holding's return fetch failed for this month "
                    f"(reason: {failure_reason}) - the 0.00% figures below are "
                    f"a fallback, NOT real data. Common causes: no internet "
                    f"access to Yahoo Finance from this machine, a firewall/"
                    f"proxy blocking it, or an outdated `yfinance` package "
                    f"being blocked by Yahoo (try `pip install -U yfinance`)."
                )
            elif "Return Status" in month_df.columns and \
                    month_df["Return Status"].astype(str).str.startswith("Fetch Failed").any():
                n_failed = month_df["Return Status"].astype(str).str.startswith("Fetch Failed").sum()
                st.warning(
                    f"{n_failed} of {len(month_df)} holding(s) this month show 0.00% because "
                    f"their return fetch failed (see the 'Return Status' column below), not "
                    f"because they were genuinely flat."
                )

            # A Stock Return built from a single day's raw Close, with no
            # cross-validation, can be silently wrong if that one day's
            # print was a bad/thin tick (common on illiquid small-caps) -
            # this is unrelated to "Fetch Failed" above (the fetch itself
            # succeeded; the price it fetched is just questionable). Flag it
            # loudly rather than let it sit in the table looking exactly as
            # trustworthy as every genuinely-fetched row - see
            # attribution._asof_close().
            if "Return Status" in month_df.columns and \
                    month_df["Return Status"].astype(str).str.contains("Verify", na=False).any():
                flagged = month_df[month_df["Return Status"].astype(str).str.contains("Verify", na=False)]
                st.warning(
                    f"\u26a0 {len(flagged)} holding(s) this month have a Stock Return that looks "
                    f"unusual against their own nearby trading days: "
                    f"{', '.join(flagged['Stock Name'])}. Double-check these against a manual "
                    f"price lookup (or a corporate-action calendar) before trusting them - see "
                    f"'Return Status' and the 'Return Start/End Date & Close' columns below for "
                    f"exactly which date/price was used."
                )

            # Contribution = Current Weight (%) x Stock Return (decimal) -
            # already in percentage-point units, so it's formatted with a
            # plain "+.2f" + literal "%" below, same as Current Weight,
            # rather than "+.2%" (which would multiply it by 100 again).
            top5, bottom5 = attribution.top_bottom_contributors(month_df, n=5)
            display_cols = ["Stock Name", "ISIN", "Current Weight", "Stock Return", "Contribution"]
            if "Return Status" in month_df.columns:
                display_cols.append("Return Status")
            # Exact date/price used at each boundary - lets a flagged (or
            # simply surprising) row be checked against a manual lookup
            # instead of trusting the percentage alone.
            for audit_col in ("Return Start Date", "Return Start Close",
                               "Return End Date", "Return End Close"):
                if audit_col in month_df.columns:
                    display_cols.append(audit_col)
            col_best, col_worst = st.columns(2)
            with col_best:
                st.markdown(f"**Top 5 - {contrib_month_ts.strftime('%b %Y')}**")
                st.altair_chart(_contributor_bar_chart(top5, "#1F4E78"), use_container_width=True)
                st.dataframe(
                    top5[display_cols].style.format(
                        {"Current Weight": "{:.2f}%", "Stock Return": "{:+.2%}",
                         "Contribution": "{:+.2f}%"}, na_rep="-"),
                    use_container_width=True, hide_index=True,
                )
            with col_worst:
                st.markdown(f"**Bottom 5 - {contrib_month_ts.strftime('%b %Y')}**")
                st.altair_chart(_contributor_bar_chart(bottom5, "#C0392B"), use_container_width=True)
                st.dataframe(
                    bottom5[display_cols].style.format(
                        {"Current Weight": "{:.2f}%", "Stock Return": "{:+.2%}",
                         "Contribution": "{:+.2f}%"}, na_rep="-"),
                    use_container_width=True, hide_index=True,
                )
    elif result is not None:
        st.caption("Selected month has changed - click **Fetch contributors for this month** to load it.")

st.markdown("---")

if st.button("Generate Excel Report"):
    with st.spinner("Fetching stock returns and building report..."):
        stock_returns = attribution.fetch_stock_returns(
            holdings, start=s["Start Date"], end=s["End Date"]
        )
        holdings_with_returns = holdings.merge(stock_returns, on="ISIN", how="left")
        stock_contrib = attribution.stock_contribution(holdings_with_returns)
        sector_contrib = attribution.sector_contribution(stock_contrib)

        out_path = report_generator.build_fund_report(
            fund_code=fund_code,
            fund_name=fund_name,
            perf=perf,
            period_perf=period_perf,
            holdings_enriched=holdings,
            stock_contrib=stock_contrib,
            sector_contrib=sector_contrib,
            rebalance_df=rebalance_df,
            threshold=threshold,
        )

    with open(out_path, "rb") as f:
        st.download_button("Download Fund_Report.xlsx", f, file_name=out_path.name)
    st.success(f"Report generated: {out_path.name}")
