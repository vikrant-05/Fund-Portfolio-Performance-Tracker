"""
generate_sample_data.py
------------------------
Creates realistic DEMO input files in Inputs/ so the tracker can be run
end-to-end without real firm data. This is only a template/demo generator -
in production you would instead point the app (via its file picker, or the
--weightage/--nav/--nse-master/--bse-master CLI flags) at your real
Weightage file, Daily NAV file, and the NSE/BSE Security Master files you
downloaded from nseindia.com / bseindia.com.

The demo mirrors several things about the real data:
  - Weightage.xlsx carries one row per holding per MONTH-END since each
    fund's inception, not a single "as of today" snapshot, plus a "Cash"
    line each month for un-invested cash.
  - Daily_NAV.xlsx can have a short data gap (NAV recorded as 0) right
    after a fund's inception, before it had a NAV yet.
  - NSE_Security_Master_sample.csv / BSE_Security_Master_sample.csv give
    security_master.py something real to resolve ISIN -> Yahoo ticker
    against, in the same shape as the actual NSE/BSE reference files
    (SYMBOL/ISIN NUMBER for NSE, SC_CODE/ISIN_NO for BSE).

Run:
    python generate_sample_data.py
Then, the first time you run `python main.py` (or `streamlit run
dashboard.py`), select the four files it writes into Inputs/ when prompted -
after that they're remembered automatically.
"""

import numpy as np
import pandas as pd
from pathlib import Path

INPUT_DIR = Path(__file__).parent / "Inputs"
INPUT_DIR.mkdir(parents=True, exist_ok=True)

rng = np.random.default_rng(42)

# ---------------------------------------------------------------------------
# 1. Universe of stocks: ISIN, NSE symbol, BSE scrip code, company name.
#    (NSE/BSE codes below are the real, publicly-known identifiers for these
#    well-known Indian large-caps, used here purely for a realistic demo.)
# ---------------------------------------------------------------------------
UNIVERSE = [
    ("INE002A01018", "RELIANCE",   "500325", "Reliance Industries"),
    ("INE467B01029", "TCS",        "532540", "Tata Consultancy Services"),
    ("INE040A01034", "HDFCBANK",   "500180", "HDFC Bank"),
    ("INE009A01021", "INFY",       "500209", "Infosys"),
    ("INE030A01027", "HINDUNILVR", "500696", "Hindustan Unilever"),
    ("INE062A01020", "SBIN",       "500112", "State Bank of India"),
    ("INE154A01025", "ITC",        "500875", "ITC"),
    ("INE397D01024", "BHARTIARTL", "532454", "Bharti Airtel"),
    ("INE018A01030", "LT",         "500510", "Larsen & Toubro"),
    ("INE237A01028", "KOTAKBANK",  "500247", "Kotak Mahindra Bank"),
    ("INE860A01027", "HCLTECH",    "532281", "HCL Technologies"),
    ("INE059A01026", "CIPLA",      "500087", "Cipla"),
]

FUNDS = [
    ("CBTWRR", "Core Balanced Top-weight Regular Return Fund"),
    ("ABCD01", "Alpha Blue Chip Dynamic Fund"),
]

AS_OF_DATE = pd.Timestamp("2026-07-10")

# ---------------------------------------------------------------------------
# 2. Weightage sheet - one row per holding per month-end since inception,
#    plus a Cash line per fund per month.
# ---------------------------------------------------------------------------
month_ends = pd.date_range(end=AS_OF_DATE, periods=12, freq="ME")  # ~1 year of monthly snapshots

weight_rows = []
for fund_code, fund_name in FUNDS:
    n_holdings = rng.integers(6, len(UNIVERSE) + 1)
    picks = rng.choice(len(UNIVERSE), size=n_holdings, replace=False)
    base_weights = rng.dirichlet(np.ones(n_holdings)) * 100

    for month_idx, month_end in enumerate(month_ends):
        # weights drift a little every month rather than staying fixed
        drift = rng.normal(0, 1.5, size=n_holdings) * (month_idx + 1) ** 0.5
        raw = np.clip(base_weights + drift, 0.1, None)
        cash_weight = round(float(rng.uniform(2, 6)), 2)
        invested_weights = raw / raw.sum() * (100 - cash_weight)

        for idx, w in zip(picks, invested_weights):
            isin, _symbol, _bse_code, name = UNIVERSE[idx]
            weight_rows.append({
                "Date": month_end, "Fund Code": fund_code, "Fund Name": fund_name,
                "ISIN": isin, "Stock Name": name, "Current Weight": round(float(w), 2),
            })

        # Cash line: no ISIN, recognised by name - exempt from ticker/sector
        # lookup downstream but still counted in the fund's total weight.
        weight_rows.append({
            "Date": month_end, "Fund Code": fund_code, "Fund Name": fund_name,
            "ISIN": "", "Stock Name": "Cash & Cash Equivalents", "Current Weight": cash_weight,
        })

weightage_df = pd.DataFrame(weight_rows)
weightage_df.to_excel(INPUT_DIR / "Weightage.xlsx", index=False)

# ---------------------------------------------------------------------------
# 3. Daily NAV sheet - one series per fund, plus a shared benchmark. The
#    second fund gets a short NAV=0 gap right after inception, to exercise
#    the "ignore not-yet-available NAV" handling in data_loader.load_nav().
# ---------------------------------------------------------------------------
dates = pd.bdate_range(start=month_ends[0], end=AS_OF_DATE)

nav_frames = []
benchmark_nav = 100 * np.cumprod(1 + rng.normal(0.0004, 0.008, size=len(dates)))
for i, (fund_code, _) in enumerate(FUNDS):
    drift = rng.uniform(0.0003, 0.0007)
    vol = rng.uniform(0.006, 0.011)
    fund_nav = 100 * np.cumprod(1 + rng.normal(drift, vol, size=len(dates)))
    fund_nav = np.round(fund_nav, 4)
    bench = np.round(benchmark_nav, 4)

    if i == 1:
        gap = slice(0, 5)  # first 5 business days: NAV not yet available
        fund_nav[gap] = 0.0
        bench[gap] = 0.0

    nav_frames.append(pd.DataFrame({
        "Date": dates, "Fund Code": fund_code,
        "Portfolio NAV": fund_nav, "Benchmark NAV": bench,
    }))

nav_df = pd.concat(nav_frames, ignore_index=True)
nav_df.to_excel(INPUT_DIR / "Daily_NAV.xlsx", index=False)

# ---------------------------------------------------------------------------
# 4. Sample NSE & BSE Security Master files, in the same column shape
#    security_master.py expects from the real files. In production you'd
#    select the actual files downloaded from nseindia.com / bseindia.com
#    instead (once, via the file picker or --nse-master/--bse-master).
# ---------------------------------------------------------------------------
nse_master_df = pd.DataFrame([
    {"SYMBOL": symbol, "NAME OF COMPANY": name, "SERIES": "EQ",
     "ISIN NUMBER": isin, "FACE VALUE": 1}
    for isin, symbol, _bse_code, name in UNIVERSE
])
nse_master_df.to_csv(INPUT_DIR / "NSE_Security_Master_sample.csv", index=False)

bse_master_df = pd.DataFrame([
    {"SC_CODE": bse_code, "SC_NAME": name, "SC_GROUP": "A", "ISIN_NO": isin}
    for isin, _symbol, bse_code, name in UNIVERSE
])
bse_master_df.to_csv(INPUT_DIR / "BSE_Security_Master_sample.csv", index=False)

print(f"Sample input files written to: {INPUT_DIR}")
print(" - Weightage.xlsx                   (monthly snapshots since inception, incl. a Cash line per month)")
print(" - Daily_NAV.xlsx                   (includes a short NAV=0 gap for ABCD01 right after inception)")
print(" - NSE_Security_Master_sample.csv   (demo NSE reference file for ISIN -> ticker mapping)")
print(" - BSE_Security_Master_sample.csv   (demo BSE reference file for ISIN -> ticker mapping)")
print()
print("Next: run `python main.py` and select these four files when prompted "
      "(or pass them directly, e.g.:")
print("  python main.py --weightage Inputs/Weightage.xlsx --nav Inputs/Daily_NAV.xlsx "
      "\\\n                 --nse-master Inputs/NSE_Security_Master_sample.csv "
      "\\\n                 --bse-master Inputs/BSE_Security_Master_sample.csv )")
