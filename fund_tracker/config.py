"""
config.py
---------
Single place for tunables and fixed project directories.

Input file locations are deliberately NOT hardcoded here. They're resolved
on demand through file_manager.ConfigManager (config.json), which validates
stored path(s) and only opens a file-picker dialog when nothing valid is
configured yet, or the user explicitly asks to update the Security Master
files. Every module that needs one of these files should call the matching
get_*() function below - nothing else in the application should build its
own path to them.

When run as the Streamlit dashboard (the primary, hosted way this tool is
used), there's no display for that file-picker dialog to appear on. Files
are instead uploaded through the browser (see dashboard.py's sidebar),
written into INPUT_DIR below, and registered via add_fund_files() /
set_nse_security_master() / set_bse_security_master() - the same
config.json ends up holding the same kind of path either way. Whatever's
registered stays configured - and the underlying file stays in INPUT_DIR -
across dashboard sessions/reruns until it's explicitly replaced (Security
Master) or removed (a fund's Weightage/NAV pair), for as long as the
app's disk survives (see README "Persistence" section for the caveat on
platforms with ephemeral storage, e.g. Streamlit Community Cloud across a
redeploy).

Weightage / Daily NAV are a *list* of files, not a single one - a firm adds
another fund by supplying its own Weightage + Daily NAV Excel pair (same
column headers as every other file) via config.add_fund_files(); every file
already configured keeps being loaded too. See file_manager.py for details.
"""

from pathlib import Path

import file_manager

BASE_DIR = Path(__file__).parent

# Output/cache locations are fixed, project-relative folders rather than
# something a user selects per run, so these stay as plain constants.
OUTPUT_DIR = BASE_DIR / "Outputs"
CHARTS_DIR = OUTPUT_DIR / "Performance_Charts"
CACHE_DIR = BASE_DIR / ".cache"

# Not read from directly anymore - used as (a) the desktop file-picker's
# initial folder the first time a portfolio/master file is selected, and
# (b) the on-disk save location for every file uploaded through the
# Streamlit dashboard (Weightage, Daily NAV, NSE/BSE Security Master). A
# file written here stays here - and stays configured - until it's
# explicitly replaced/removed, so this doubles as the app's persistent
# storage for uploads across dashboard reruns/sessions.
INPUT_DIR = BASE_DIR / "Inputs"

SECTOR_CACHE_FILE = CACHE_DIR / "sector_cache.json"

# Caches attribution.py's per-ticker price-history lookups (keyed by
# ticker + date window), so re-running the same fund/window doesn't
# re-download data that's already on disk. See attribution.py.
RETURN_CACHE_FILE = CACHE_DIR / "stock_return_cache.json"

# Trading days per year, used for annualising returns / CAGR / tracking error.
TRADING_DAYS_PER_YEAR = 252

# Default rebalancing drift threshold (in percentage points). A holding's
# Current Weight vs its Previous month-end Weight beyond this is flagged -
# see rebalance.py. Configurable per run/dashboard session.
DEFAULT_REBALANCE_THRESHOLD = 3.0

# Risk-free rate assumption for simple alpha calc (annualised, as a fraction).
RISK_FREE_RATE = 0.0

# ---------------------------------------------------------------------------
# Yahoo Finance request pacing / retry behaviour.
# Yahoo's unofficial API will return "Too Many Requests" (HTTP 429) if it's
# hit too fast or too often. These knobs keep the pipeline under that limit:
#   - a minimum delay between any two Yahoo Finance calls (even across
#     threads - see yahoo_fetch._throttle())
#   - a small thread pool (not per-ticker unbounded threads) for sector
#     lookups, so requests are parallelised a little without hammering Yahoo
#   - retries with exponential backoff, with extra backoff specifically for
#     429/rate-limit responses
# Tune these down (fewer workers, longer delay) if you're still seeing
# rate-limit errors; tune them up if Yahoo is comfortable and you want
# faster runs.
# ---------------------------------------------------------------------------
YAHOO_REQUEST_DELAY = 0.35   # seconds, minimum spacing between Yahoo Finance calls
YAHOO_MAX_RETRIES = 3        # retries per ticker before giving up and marking Unknown/0%
YAHOO_BACKOFF_BASE = 2.0     # seconds, base for exponential backoff (doubles each retry)
YAHOO_MAX_WORKERS = 4        # max concurrent threads for sector/industry lookups

# ---------------------------------------------------------------------------
# Attribution: as-of price lookback buffer + outlier sanity check.
#
# fetch_stock_returns() (attribution.py) computes Stock Return from a single
# day's raw Close price at each boundary date (last trading day on/before
# start, and on/before end). That single print is trusted with no
# cross-validation, which lets one bad/thin tick from Yahoo's feed - common
# for illiquid NSE/BSE small- and mid-caps - silently masquerade as a real,
# large monthly move (e.g. a stray -33% for a stock that actually moved
# -2.7%), with every other holding in the same batch fetching correctly.
# There is deliberately no automatic "correction": a real one-day move
# should never be silently overwritten. Instead, ATTRIBUTION_OUTLIER_RATIO
# flags (via Return Status) any boundary close that differs from the median
# of its own nearby trading days by more than this fraction, so it gets a
# second look before it's trusted in a report - see attribution.py's
# _asof_close() and the "Verify" suffix on Return Status.
ATTRIBUTION_LOOKBACK_BUFFER_DAYS = 15  # calendar days fetched before `start`,
                                       # so the as-of lookup always has a real
                                       # trading day on/before it even across
                                       # long weekend/festival holiday clusters
ATTRIBUTION_OUTLIER_WINDOW_DAYS = 5    # trading days on each side of a
                                       # boundary date used as its "local
                                       # neighbourhood" for the sanity check
ATTRIBUTION_OUTLIER_RATIO = 0.15       # flag a boundary close that differs
                                       # from its local median by more than 15%

for d in (INPUT_DIR, OUTPUT_DIR, CHARTS_DIR, CACHE_DIR):
    d.mkdir(parents=True, exist_ok=True)

_manager = file_manager.get_manager()


# ---------------------------------------------------------------------------
# Portfolio input files - change often (daily/weekly); every configured file
# is re-validated (not re-prompted) on every run unless moved. Multiple
# funds can be spread across multiple Weightage/NAV file pairs.
# ---------------------------------------------------------------------------
def get_weightage_files() -> list:
    """Every configured Weightage file (list of Path), prompting via file
    picker only if none is configured/valid yet."""
    return _manager.get_weightage_files()


def get_nav_files() -> list:
    """Every configured Daily NAV file (list of Path), same behaviour."""
    return _manager.get_nav_files()


def add_fund_files(weightage_path, nav_path) -> tuple:
    """
    Register a new fund by adding its Weightage + Daily NAV Excel file pair
    (same column headers as data_loader.py expects) to what's already
    configured. Nothing already configured is removed or overwritten - the
    new fund(s) inside these files simply show up alongside the existing
    ones (e.g. in the dashboard's fund dropdown) from the next load onward.
    """
    return _manager.add_fund_files(weightage_path, nav_path)


def remove_weightage_file(path) -> bool:
    """Remove a single Weightage file from the configuration (the file
    itself is left on disk - this only stops the app from loading it)."""
    return _manager.remove_weightage_file(path)


def remove_nav_file(path) -> bool:
    """Remove a single Daily NAV file from the configuration."""
    return _manager.remove_nav_file(path)


def remove_fund_files(weightage_path, nav_path) -> tuple:
    """
    Un-register a fund by removing its Weightage + Daily NAV file pair from
    the configuration - the inverse of add_fund_files(). Files are left on
    disk untouched; any fund code that lived only inside them simply stops
    showing up (fund dropdown, reports, etc.) from the next load onward.
    """
    return _manager.remove_fund_files(weightage_path, nav_path)


# ---------------------------------------------------------------------------
# Security Master reference files - effectively static; selected once, then
# loaded silently unless missing or explicitly updated.
# ---------------------------------------------------------------------------
def get_nse_security_master() -> Path:
    """Path to the NSE Security Master file."""
    return _manager.get_nse_security_master()


def get_bse_security_master() -> Path:
    """Path to the BSE Security Master file."""
    return _manager.get_bse_security_master()


def update_security_masters() -> tuple:
    """Explicit 'Update Security Master Files' action (desktop/CLI only) -
    force re-selection of both the NSE and BSE Security Master files via
    the native file picker. Not used by the Streamlit dashboard, which
    instead calls set_nse_security_master()/set_bse_security_master() with
    an uploaded file - see file_manager.py's module docstring."""
    return _manager.update_security_masters()


def set_nse_security_master(path) -> Path:
    """Register an NSE Security Master file directly (no file picker) -
    used by the dashboard's uploader and main.py's --nse-master flag."""
    return _manager.set_path(file_manager.KEY_NSE_MASTER, path)


def set_bse_security_master(path) -> Path:
    """Register a BSE Security Master file directly (no file picker) -
    used by the dashboard's uploader and main.py's --bse-master flag."""
    return _manager.set_path(file_manager.KEY_BSE_MASTER, path)


# ---------------------------------------------------------------------------
# Non-prompting existence checks - safe to call anywhere, including a hosted
# Streamlit app with no display. Callers should check these before calling
# any get_*() above, and render an upload prompt instead of letting a
# missing file fall through to the desktop file picker.
# ---------------------------------------------------------------------------
def has_weightage_files() -> bool:
    return _manager.has_weightage_files()


def has_nav_files() -> bool:
    return _manager.has_nav_files()


def has_nse_security_master() -> bool:
    return _manager.has_nse_security_master()


def has_bse_security_master() -> bool:
    return _manager.has_bse_security_master()

