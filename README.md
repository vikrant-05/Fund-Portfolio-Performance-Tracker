# Fund / Portfolio Performance Tracker

A modular Python tool that turns a firm's existing NAV series and portfolio
weightage snapshot into a full performance, attribution, and rebalancing
report per fund — with no fund hardcoded, so it scales to any number of
funds automatically.

## What it does

1. **Loads & validates** the Weightage and Daily NAV files (locations
   managed by `file_manager.py` - see below, not hardcoded)
2. **Resolves ISIN → Yahoo Ticker → Sector/Industry**, using the NSE/BSE
   Security Master reference files to find the ticker and Yahoo Finance to
   fetch sector/industry (cached to disk)
3. Lets the **fund manager enter target weights inside the app** (CLI prompt, JSON file, or the Streamlit dashboard) — never written back to the source files
4. Calculates **performance**: daily/cumulative/absolute return, CAGR, benchmark comparison (active return, alpha, tracking error, information ratio), drawdown
5. Calculates **attribution**: stock-level and sector-level contribution (`Weight × Return`)
6. Runs **rebalancing checks**: flags any holding whose drift from target exceeds a configurable threshold
7. Generates a formatted **Excel report with charts** (`Outputs/Fund_Report_<FundCode>.xlsx`) for every fund found

## Project layout

```
config.py               fixed directories + get_*() accessors for the 4 managed files
file_manager.py          config.json read/write, path validation, file-picker dialogs
security_master.py       ISIN -> Yahoo ticker via NSE/BSE Security Master files
data_loader.py           load + validate Weightage/NAV, build the ISIN mapping
yahoo_fetch.py           ticker -> sector/industry (cached)
performance.py           returns, CAGR, benchmark comparison, drawdown
attribution.py           stock & sector contribution
rebalance.py             current vs target weight, drift, flags
report_generator.py      builds the Excel report + matplotlib charts
main.py                  orchestrates the pipeline for every fund
dashboard.py (optional)  Streamlit UI for interactive use
generate_sample_data.py  creates demo Inputs/ files (incl. sample Security Masters)
Inputs/                  suggested starting folder for the file picker (not required)
Outputs/                 Fund_Report_<code>.xlsx + Performance_Charts/ (generated)
config.json              auto-created/updated - remembers your 4 file locations
```

## Setup

From the project root, in a terminal / command prompt:

```bash
# (recommended) create an isolated environment first
python -m venv venv
venv\Scripts\activate        # Windows
source venv/bin/activate     # macOS/Linux

# then install dependencies
pip install -r requirements.txt
```

`tkinter` (used for the file-picker dialogs) ships with the standard Python
installer on Windows/macOS. On Linux you may need to install it separately,
e.g. `sudo apt install python3-tk` — this is an OS package, not something
`pip install -r requirements.txt` can install for you.

## Quick start (with demo data)

```bash
python generate_sample_data.py   # writes demo Inputs/*.xlsx and Inputs/*Security_Master_sample.csv
python main.py                   # first run: a file picker opens for each of the 4 files below
```

On that first `python main.py`, you'll be prompted (via a native file
dialog) to select, in order: the Weightage file, the Daily NAV file, the NSE
Security Master file, and the BSE Security Master file. For the demo, just
pick the four files `generate_sample_data.py` wrote into `Inputs/`. Every
choice is saved to `config.json`, so subsequent runs skip straight to
processing — no code changes, no re-prompting, unless a file is later moved.

To use your **real** data, run `python main.py` again and point it at your
firm's actual Weightage and Daily NAV files (same column headers as the
demo files) — the app will notice the file changed from what's remembered
in `config.json`... actually it won't notice on its own (see below), so
either delete `config.json` to be re-prompted for everything, or pass the
new file explicitly:

```bash
python main.py --weightage /path/to/real/Weightage.xlsx --nav /path/to/real/Daily_NAV.xlsx
```

## Managing file locations (`file_manager.py` / `config.json`)

Nothing in the application hardcodes a path to the Weightage file, the
Daily NAV file, or either Security Master file. Instead, every module asks
`config.py` for the path it needs, and `config.py` delegates to
`file_manager.ConfigManager`, which is responsible for:

- reading/writing `config.json`
- validating that a remembered path still exists on disk
- opening a file-picker dialog only when it actually has to
- handing back a `pathlib.Path` to the caller

The two file types are treated differently, matching how often they change:

| File type | Behaviour |
|---|---|
| **Weightage / Daily NAV** (change daily/weekly) | Re-validated (existence check only) every run. If the file's been moved/deleted/renamed, you're prompted to browse for *just that file*; `config.json` is updated automatically. |
| **NSE / BSE Security Master** (near-static reference data) | Selected once, on first run. Loaded silently on every run after that — no prompt — unless the stored path is missing, or you explicitly ask to update it. |

To force an update of the Security Master files at any time (e.g. after
downloading fresher ones):

```bash
python main.py --update-security-masters
```

or, from the Streamlit dashboard, click **"Update Security Master Files"**
in the sidebar.

For headless/server use, all four files can also be supplied directly on
the command line (this both bypasses the dialog for that run *and* updates
`config.json` for next time):

```bash
python main.py --weightage Inputs/Weightage.xlsx \
                --nav Inputs/Daily_NAV.xlsx \
                --nse-master Inputs/NSE_Security_Master_sample.csv \
                --bse-master Inputs/BSE_Security_Master_sample.csv
```

## ISIN → Yahoo Ticker mapping (`security_master.py`)

Yahoo Finance can't be queried by ISIN directly, so `security_master.py`
resolves each ISIN to a Yahoo ticker using the NSE and BSE Security Master
files you selected above:

1. Look up the ISIN in the **NSE Security Master**.
   Found → ticker = `<NSE symbol>` + **`.NS`**
2. Not found on NSE? Look it up in the **BSE Security Master**.
   Found → ticker = `<BSE scrip code>` + **`.BO`**
3. Not found on either → the ISIN is **logged** (printed, and appended to
   `.cache/missing_isins.log`) and **skipped** — the rest of the pipeline
   keeps running with `Sector = "Unknown"` and `Stock Return = 0%` for that
   holding, exactly as it already does for any Yahoo Finance fetch failure.

The Yahoo ticker is only ever used *internally*, to query Yahoo Finance for
sector/industry (and, in `attribution.py`, historical prices). Everywhere
else in the app — holdings tables, the Excel report, rebalancing — the
**ISIN remains the primary identifier**.

Other modules never touch a Security Master file, a column name, or a
ticker directly; they call `security_master.resolve_tickers(isins)` /
`security_master.get_ticker_map(isins)` (batch) or
`security_master.get_market_data(isin)` (single ISIN, used for ad-hoc
lookups) and get back Yahoo data keyed by ISIN.

### If NSE/BSE change their file format

`security_master.py` detects the ISIN/Symbol/Series columns by matching
against a short alias list at the top of the file (`NSE_ISIN_ALIASES`,
`NSE_SYMBOL_ALIASES`, `BSE_ISIN_ALIASES`, `BSE_SYMBOL_ALIASES`, ...) rather
than one exact hardcoded header name. If a future NSE/BSE download uses a
header spelling that isn't recognised, add it to the relevant alias list —
no other code needs to change.

## Rebalancing / drift threshold

There's no manager-entered target weight - rebalancing is fully automatic.
A holding is flagged when its Current Weight (this run's snapshot) has
drifted from its Previous Weight (the same fund's prior month-end
Weightage snapshot, same ISIN) by more than a configurable threshold - see
`rebalance.compute_weight_drift()`.

```bash
python main.py                   # use the default drift threshold (config.DEFAULT_REBALANCE_THRESHOLD)
python main.py --threshold 5     # flag any holding that's moved more than 5 percentage points
python main.py --as-of 2026-06   # run against the June-2026 month-end snapshot instead of the latest one
```

A holding with no earlier snapshot to compare against (a brand-new
position, or a fund's very first month-end) is treated as Previous Weight
= 0%, so it's naturally flagged rather than silently skipped.

## Running the dashboard (Streamlit)

1. Open a command prompt / terminal in the project folder.
2. (If you haven't already) install dependencies:
   ```bash
   pip install -r requirements.txt
   ```
3. Launch the dashboard:
   ```bash
   streamlit run dashboard.py
   ```
4. Streamlit starts a local web server and should open your browser
   automatically to something like `http://localhost:8501`. If it doesn't
   open automatically, copy that URL from the terminal into your browser.
5. Use the sidebar to add a fund (upload its Weightage + Daily NAV Excel
   pair), remove a fund, update the Security Master files, or adjust the
   drift threshold - the same underlying `config.json` used by `main.py`
   is shared here, so choices made in either place are remembered for next
   time.
6. To stop the dashboard, go back to the terminal and press `Ctrl+C`.

## Notes on this environment vs. production

- Yahoo Finance calls require outbound internet access to
  `query1/query2.finance.yahoo.com`. Wherever that's unavailable (as in the
  sandbox this was built in), the tool **degrades gracefully**: sector shows
  as `"Unknown"` and stock return defaults to `0%` for attribution, with a
  warning printed — it never crashes the run. Once you run this in an
  environment with normal internet access, real sector/industry names and
  stock returns will populate automatically.
- Sector lookups are cached in `.cache/sector_cache.json` so re-runs on the
  same tickers don't keep re-hitting Yahoo Finance.
- ISINs that can't be resolved against either Security Master file are
  logged to `.cache/missing_isins.log` for later review.

## Version 2 (documented, not built — needs data not yet available)

- **XIRR** from actual dated cash flows (`pyxirr`), once transaction history exists
- NAV computed from holdings × live prices instead of a precomputed NAV series
- Live intraday valuation
- Historical rebalancing simulation / what-if analysis
- Multi-fund comparison dashboard
- Scheduled automatic data refresh
- Additional exchanges beyond NSE/BSE in `security_master.py` (the alias-list
  design already supports this — add a new master-file loader following the
  `_build_nse_lookup`/`_build_bse_lookup` pattern and a new suffix, e.g. `.L`)

None of this requires touching the current architecture — `performance.py`,
`attribution.py`, and `rebalance.py` are already independent modules that a
new data source can plug straight into.
