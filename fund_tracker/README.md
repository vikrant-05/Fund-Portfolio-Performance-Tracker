# Fund / Portfolio Performance Tracker

A modular Python tool that turns a firm's existing NAV series and portfolio
weightage snapshots into a full performance, attribution, and rebalancing
report per fund — with no fund hardcoded, so it scales to any number of
funds automatically.

It's built to be **hosted as a Streamlit web app**: a firm deploys it once,
and every fund manager uses it from a browser — uploading data files
through the sidebar rather than needing local Python, a file system, or a
CLI. A local/scripted CLI (`main.py`) is also available for batch runs and
automation.

## What it does

1. **Loads & validates** the Weightage and Daily NAV files for every fund
   (files are uploaded through the dashboard — see [Data files](#data-files)
   below — and stay configured until you change them)
2. **Resolves ISIN → Yahoo Ticker → Sector/Industry**, using the NSE/BSE
   Security Master reference files to find the ticker and Yahoo Finance to
   fetch sector/industry (cached to disk)
3. Calculates **performance**: daily/cumulative/absolute return, CAGR,
   benchmark comparison (active return, alpha, tracking error, information
   ratio), drawdown — both since inception and broken out by period (Last
   5Y / 3Y / 1Y / Current Financial Year)
4. Calculates **attribution**: stock-level and sector-level contribution
   (`Weight × Return`), plus a best/worst monthly contributors view
5. Runs **rebalancing checks** automatically: flags any holding whose
   Current Weight has drifted from its previous month-end Weight by more
   than a configurable threshold — no manager-entered target weight needed
6. Generates a formatted **Excel report with charts**
   (`Outputs/Fund_Report_<FundCode>.xlsx`) for every fund, downloadable
   straight from the dashboard

## Project layout

```
config.py               fixed directories + get_*()/has_*() accessors for the 4 managed files
file_manager.py          config.json read/write, path validation, upload/file-picker handling
security_master.py       ISIN -> Yahoo ticker via NSE/BSE Security Master files
data_loader.py           load + validate Weightage/NAV, build the ISIN mapping
yahoo_fetch.py           ticker -> sector/industry (cached)
performance.py           returns, CAGR, benchmark comparison, drawdown
attribution.py           stock & sector contribution
rebalance.py             current vs previous month weight, drift, flags
report_generator.py      builds the Excel report + matplotlib charts
dashboard.py             the Streamlit app — primary, hosted way to use this tool
main.py                  CLI orchestrator for every fund (local/scripted use)
generate_sample_data.py  creates demo files (incl. sample Security Masters) to try the tool with
warm_cache.py            offline pre-fetch of Sector/Industry for every ISIN a fund has ever held
Inputs/                  where uploaded/selected files are stored (persisted — see below)
Outputs/                 Fund_Report_<code>.xlsx + Performance_Charts/ (generated)
config.json              auto-created/updated — remembers which files are configured
```

## Setup

```bash
pip install -r requirements.txt
```

`tkinter` is only needed if you run `main.py` locally (its file-picker
dialog). It's **not** required to run or host `dashboard.py`, which uses
browser uploads instead. It ships with the standard Python installer on
Windows/macOS; on Linux you may need `sudo apt install python3-tk`.

## Running the dashboard

Locally:

```bash
streamlit run dashboard.py
```

Hosted (e.g. Streamlit Community Cloud, or any platform that runs
`streamlit run dashboard.py` from this repo): deploy as normal, pointing
at `dashboard.py` as the entry point and `requirements.txt` for
dependencies. No file paths, environment variables, or secrets need to be
configured up front — the app starts empty and every file is supplied
through the browser the first time someone uses it. See
[Persistence & hosting](#persistence--hosting) for the one caveat worth
knowing about before you rely on this for production data.

### First run / getting started

A freshly deployed (or freshly cloned) app has nothing configured yet. The
main page will show a **"Get started"** prompt instead of a fund dashboard
until all four required files are in place:

1. In the sidebar, open **"Security Master files"** and upload the NSE and
   BSE Security Master files (downloaded from nseindia.com / bseindia.com —
   see [Security Master files](#security-master-files-nseindiacom--bseindiacom)
   below for the exact format).
2. In the sidebar, open **"Add a fund"** and upload a Weightage file and a
   Daily NAV file for at least one fund (see
   [Weightage file](#weightage-file) and
   [Daily NAV file](#daily-nav-file) below for the exact format).

Once all four are uploaded, the dashboard renders normally: pick a fund and
a month-end snapshot from the sidebar, and everything else — performance,
attribution, rebalancing, contributors, the Excel report — follows from
those two selections.

Don't have real files handy yet? Run this once, locally, before you deploy
(or in a separate terminal against the same folder), then upload the four
files it writes into `Inputs/`:

```bash
python generate_sample_data.py
```

## Data files

Everything the tool needs falls into two categories, handled differently
because they change at very different rates.

| File type | How often it changes | Dashboard behaviour |
|---|---|---|
| **Weightage / Daily NAV** (per fund) | daily/weekly | Uploaded per fund via **"Add a fund"**; every fund uploaded stays in the **Fund** dropdown across sessions until removed via **"Remove a fund"**. Adding more funds never removes ones already there. |
| **NSE / BSE Security Master** | effectively static reference data | Uploaded once via **"Security Master files"**; loaded silently on every run after that — no re-prompt — until you upload a fresh copy to replace it (e.g. after downloading an updated file). |

### Weightage file

One row per holding **per month-end since the fund's inception**, not just
a single "as of today" snapshot, plus a Cash line per fund per month for
un-invested cash. Required columns:

| Column | Notes |
|---|---|
| `Date` | month-end snapshot date |
| `Fund Code` | short fund identifier — drives the dashboard's Fund dropdown and one Excel report per code |
| `Fund Name` | display name |
| `ISIN` | blank/`CASH`/anything with "cash" in the Stock Name is treated as a Cash line (exempt from ticker/sector lookup, still counted in weight totals) |
| `Stock Name` | |
| `Current Weight` | percentage, e.g. `6.25` for 6.25% |

Weights per fund/date are expected to sum to roughly 100 — a total outside
95–105 is flagged (not failed) at load time, since a small deliberate cash
buffer is normal.

### Daily NAV file

One row per fund per trading day. Required columns: `Date`, `Fund Code`,
`Portfolio NAV`, `Benchmark NAV`. A NAV of `0` or blank is treated as "not
yet available" (e.g. right after a fund's inception) and silently dropped
from that fund's series rather than failing the whole load.

### Security Master files (nseindia.com / bseindia.com)

Used to resolve each holding's ISIN to a Yahoo Finance ticker (Yahoo
can't be queried by ISIN directly):

1. Look up the ISIN in the **NSE Security Master**.
   Found → ticker = `<NSE symbol>` + **`.NS`**
2. Not found on NSE? Look it up in the **BSE Security Master**.
   Found → ticker = `<BSE scrip code>` + **`.BO`**
3. Not found on either → the ISIN is logged (`.cache/missing_isins.log`)
   and skipped — the rest of the pipeline keeps running with
   `Sector = "Unknown"` and `Stock Return = 0%` for that holding, exactly
   as it already does for any Yahoo Finance fetch failure.

Column headers are detected via an alias list rather than one exact
hardcoded name (e.g. `ISIN NUMBER` / `ISIN_NUMBER` / `ISIN CODE` / `ISIN`
for NSE), so minor formatting differences between downloads don't break
the lookup. The BSE file can be either the static scrip master
(`SC_CODE`/`ISIN_NO`/`SC_GROUP`) or a daily Bhavcopy/trade file
(`TckrSymb`/`ISIN`/`Sgmt`) — both shapes are supported, and a Bhavcopy file
is automatically filtered to the Cash Market segment so an ISIN doesn't
accidentally resolve to a derivatives contract's symbol instead of the
equity symbol.

If NSE/BSE ever rename a header this tool doesn't recognise, add the new
spelling to the alias lists at the top of `security_master.py`
(`NSE_ISIN_ALIASES`, `BSE_SYMBOL_ALIASES`, etc.) — no other code needs to
change. The dashboard's uploader validates the file's columns immediately
(via `security_master.validate_nse_master_file()` /
`validate_bse_master_file()`) and rejects a bad upload with a clear message
rather than silently registering something the pipeline can't use.

The Yahoo ticker is only ever used *internally* to query Yahoo Finance
(sector/industry, and historical prices for attribution). Everywhere else
in the app — holdings tables, the Excel report, rebalancing — the **ISIN
remains the primary identifier**.

## Persistence & hosting

Every uploaded file is written to disk under `Inputs/` and its path is
recorded in `config.json`. That means, for as long as the app's underlying
process/disk keeps running:

- A fund you add stays in the **Fund** dropdown — and its files stay on
  disk — across every dashboard reload and every visitor's session, until
  someone removes it via **"Remove a fund"** (which only un-registers it;
  the file itself isn't deleted).
- A Security Master file you set stays in use, with no re-prompt, until
  someone uploads a replacement via **"Security Master files"**.
- This is shared, process-level state, not per-browser state: one person
  uploading a fund makes it visible to every other visitor to the same
  deployment. There's no per-user login/isolation built in — treat a
  deployment as scoped to one firm/team that shares its data, not as
  multi-tenant.

**Caveat on ephemeral hosting:** some platforms (e.g. Streamlit Community
Cloud) rebuild an app's container — wiping local disk, including
`Inputs/` and `config.json` — on a redeploy, or after a long period of
inactivity. Nothing in this app deletes your files; if they disappear, it's
the platform resetting the container, and you'll need to re-upload once
after that happens. If you need uploads to survive that, host on a
platform that offers a persistent volume/disk mounted at the app's working
directory (most VM/container hosts do), or adapt `file_manager.py` to write
into that mounted path instead of the project-relative `Inputs/`.

## Using the dashboard

- **Fund / snapshot** — pick a fund and a month-end Weightage snapshot from
  the sidebar. Every table/chart below reflects that selection.
- **Rebalance drift threshold** — a slider (percentage points); any
  holding whose Current Weight has moved by more than this versus the
  *previous* month-end snapshot is flagged. There's no manager-entered
  target weight — drift is always measured against the fund's own prior
  snapshot (a brand-new holding shows Previous Weight = 0%; a fully-exited
  one shows Current Weight = 0%).
- **Performance tabs** — Since Inception / Last 5Y / Last 3Y / Last 1Y /
  Current FY (Apr–Mar), each with its own Absolute Return, CAGR, Alpha, Max
  Drawdown, and NAV/growth charts computed fresh over just that window. A
  window under about a year shows **CAGR/Alpha as "N/A"** rather than an
  annualised figure — annualising a partial year (e.g. treating a genuine
  +10% over 3 months as if it repeated all year) would wildly overstate it;
  use Absolute Return for those windows instead.
- **Portfolio Weightage** — current holdings with automatic drift flags
  (highlighted rows) vs the previous month-end.
- **Monthly Best & Worst Contributors** — pick a month, then fetch: pulls
  that month's holdings' price history from Yahoo Finance (only for that
  month, not the fund's whole history) and shows the top/bottom 5
  contributors by `Weight × Return`. Watch for the **Return Status**
  column: a 0.00% contribution can mean a stock genuinely didn't move, *or*
  that its return fetch failed — this column (and an on-screen warning)
  tells you which. Rows flagged "Verify" have a Stock Return that looks
  unusual against that stock's own nearby trading days (a possible bad/thin
  tick from Yahoo, common on illiquid small-caps) and are worth a manual
  price check before trusting them — the exact date/price used at each
  boundary is shown alongside the flag.
- **Generate Excel Report** — builds and offers a download of
  `Fund_Report_<FundCode>.xlsx`: Summary (incl. performance by period),
  Holdings, Attribution (stock- and sector-level), Rebalancing, and a
  Charts sheet (NAV vs Benchmark, Growth, Drawdown, Sector Allocation,
  Current vs Previous Month Allocation, Contribution by Stock).

## Running the CLI (`main.py`)

For local batch runs, scripting, or a scheduled/automated job (as opposed
to interactive browser use):

```bash
python generate_sample_data.py   # writes demo Inputs/*.xlsx and Inputs/*Security_Master_sample.csv
python main.py                   # first run: a native file picker opens for each of the 4 files below
```

On that first `python main.py`, you'll be prompted (via a native file
dialog — requires a local display, so this only works when run on a
desktop, not a headless server) to select, in order: the Weightage file,
the Daily NAV file, the NSE Security Master file, and the BSE Security
Master file. Every choice is saved to `config.json`, so subsequent runs
skip straight to processing.

Useful flags:

```bash
python main.py --threshold 5              # 5-point drift threshold instead of the 3-point default
python main.py --as-of 2026-06            # use the June-2026 month-end snapshot instead of the latest
python main.py --no-sector-fetch          # skip live Yahoo sector/industry calls; cache-only, "Unknown" for anything uncached
python main.py --no-cache                 # ignore the local sector cache; re-fetch from Yahoo Finance

# file management overrides (skip the file picker, e.g. for a server/cron job)
python main.py --add-fund path/to/NewFund_Weightage.xlsx path/to/NewFund_NAV.xlsx
python main.py --remove-fund path/to/NewFund_Weightage.xlsx path/to/NewFund_NAV.xlsx
python main.py --nse-master path/to/NSE_Security_Master.csv
python main.py --bse-master path/to/BSE_Security_Master.csv
python main.py --update-security-masters  # force re-selection of both master files (local file picker)
```

`main.py` and `dashboard.py` share the same `config.json` / `Inputs/`
folder, so a fund added through one is immediately visible to the other —
useful if you want to seed a hosted deployment's initial data by running
`main.py --add-fund ...` once against the same working directory before
sharing the dashboard URL.

### Speed note (both entry points)

Sector/Industry lookup and stock-return lookup for attribution are
independent Yahoo Finance calls — `main.py` kicks off the sector lookup for
every ISIN across every fund in the background right after inputs load,
and each fund's attribution price-fetch runs concurrently with it rather
than waiting. `warm_cache.py` takes this further for either entry point:

```bash
python warm_cache.py
```

Pre-fetches and caches Sector/Industry for **every** ISIN a fund has ever
held (its full history, not just the latest snapshot), so interactive runs
— `main.py` or the dashboard — read entirely from `.cache/sector_cache.json`
afterward, with no live Yahoo Finance wait. Good to run once after adding a
new fund, or on a schedule (cron / Task Scheduler) if new stocks get added
often.

## Yahoo Finance rate limits & offline behaviour

Yahoo's unofficial API returns "Too Many Requests" if hit too fast/often.
This tool throttles every call to a configurable minimum spacing, retries
with backoff (extra backoff specifically for rate-limit responses), and
parallelises uncached lookups across a small, bounded thread pool — see the
`YAHOO_*` settings in `config.py` if you're still seeing rate-limit errors
(turn the delay up, the worker count down) or want faster runs once Yahoo's
comfortable.

If Yahoo Finance is unreachable at all (offline sandbox, firewall, outbound
network disabled), the tool degrades gracefully rather than crashing:
sector shows as `"Unknown"` and stock return defaults to `0%` for
attribution, with a warning printed/shown. Once network access is
available, real sector/industry and stock returns populate automatically
(cached from then on).

## ISIN → Yahoo Ticker mapping — troubleshooting

- ISINs that can't be resolved against either Security Master file are
  logged to `.cache/missing_isins.log` for later review.
- Sector lookups are cached in `.cache/sector_cache.json` so re-runs on the
  same tickers don't keep re-hitting Yahoo Finance.
- If a Security Master upload is rejected, the dashboard shows exactly
  which required column(s) it couldn't find — check the file against the
  alias lists described in [Security Master files](#security-master-files-nseindiacom--bseindiacom)
  above.

## Version 2 (documented, not built — needs data not yet available)

- **XIRR** from actual dated cash flows (`pyxirr`), once transaction
  history exists
- NAV computed from holdings × live prices instead of a precomputed NAV
  series
- Live intraday valuation
- Historical rebalancing simulation / what-if analysis
- Multi-fund comparison dashboard
- Scheduled automatic data refresh
- Per-firm/multi-tenant isolation for the hosted dashboard (today, one
  deployment's uploaded data is shared/visible to everyone who uses it —
  see [Persistence & hosting](#persistence--hosting))
- Additional exchanges beyond NSE/BSE in `security_master.py` (the
  alias-list design already supports this — add a new master-file loader
  following the `_build_nse_lookup`/`_build_bse_lookup` pattern and a new
  suffix, e.g. `.L`)

None of this requires touching the current architecture —
`performance.py`, `attribution.py`, and `rebalance.py` are already
independent modules that a new data source can plug straight into.
