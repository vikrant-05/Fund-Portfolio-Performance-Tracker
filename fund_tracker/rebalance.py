"""
rebalance.py
------------
No manager-entered target weight anymore - rebalancing is fully automatic.
A holding is flagged when its Current Weight has drifted from its Previous
Weight (the fund's prior month-end Weightage snapshot, same ISIN) by more
than a configurable threshold:

    Drift = Current Weight - Previous Weight
    |Drift| > Threshold  ->  "Rebalance Required"

A holding that's brand new this month (wasn't held last month) has a
Previous Weight of 0%, so a large new position is naturally flagged; a
holding that was fully exited since last month shows a Current Weight of 0%
with whatever it used to be as its Previous Weight - both cases surface
correctly without any manual input.

If there's no earlier snapshot to compare against yet (e.g. a fund's very
first month-end), every holding's Previous Weight is treated as 0% - i.e.
the entire current allocation shows up as "new", which is the correct,
non-misleading behaviour for a fund with no prior snapshot.
"""

import pandas as pd

import config


def compute_weight_drift(
    holdings: pd.DataFrame,
    previous_holdings: pd.DataFrame = None,
    threshold: float = config.DEFAULT_REBALANCE_THRESHOLD,
) -> pd.DataFrame:
    """
    holdings: current month's snapshot - must contain Stock Name, ISIN, Current Weight.
    previous_holdings: prior month's snapshot (same columns), or None/empty
        if this is the fund's earliest available snapshot.
    threshold: absolute drift in percentage points above which a holding is flagged.

    Returns Stock Name | ISIN | Current Weight | Previous Weight | Drift |
    Action | Rebalance Required, sorted by |Drift| descending.
    """
    current = holdings[["Stock Name", "ISIN", "Current Weight"]].copy()

    if previous_holdings is None or previous_holdings.empty:
        merged = current.copy()
        merged["Previous Weight"] = 0.0
    else:
        prev = previous_holdings[["ISIN", "Stock Name", "Current Weight"]].rename(
            columns={"Current Weight": "Previous Weight", "Stock Name": "Prev Stock Name"}
        )
        merged = current.merge(prev, on="ISIN", how="outer")
        # A holding fully exited this month has no row in `current` (Stock
        # Name/Current Weight are NaN); a brand-new holding has no row in
        # `previous_holdings` (Previous Weight is NaN). Either way, keep the
        # name that IS available and default the missing weight to 0%.
        merged["Stock Name"] = merged["Stock Name"].fillna(merged["Prev Stock Name"])
        merged = merged.drop(columns=["Prev Stock Name"])
        merged["Current Weight"] = merged["Current Weight"].fillna(0.0)
        merged["Previous Weight"] = merged["Previous Weight"].fillna(0.0)

    merged["Drift"] = merged["Current Weight"] - merged["Previous Weight"]

    def action(drift):
        if drift > threshold:
            return "Weight Increased"
        if drift < -threshold:
            return "Weight Decreased"
        return "Stable"

    merged["Action"] = merged["Drift"].apply(action)
    merged["Rebalance Required"] = merged["Drift"].abs() > threshold

    return merged[["Stock Name", "ISIN", "Current Weight", "Previous Weight", "Drift",
                   "Action", "Rebalance Required"]].sort_values(
        "Drift", key=lambda s: s.abs(), ascending=False
    ).reset_index(drop=True)
