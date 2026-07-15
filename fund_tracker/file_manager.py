"""
file_manager.py
----------------
Dedicated configuration/file manager for the whole pipeline. Nothing else
in the application should hardcode a path to a Weightage/Daily NAV file, or
to the NSE/BSE Security Master files - every module asks *this* module for
the path(s) it needs, and this module decides whether that means reading
config.json, validating stored paths, or opening a file-picker dialog.

Multiple Weightage / Daily NAV files
--------------------------------------
A firm's funds don't all have to live in one Weightage.xlsx / Daily_NAV.xlsx.
To add another fund, the user supplies its own Weightage + Daily NAV Excel
file pair (same column headers as every other file - see data_loader.py's
required columns), and it's simply added to the list already configured.
Every file in the list is loaded and concatenated by data_loader.py, so a
newly-added fund's code shows up everywhere (fund picker, reports, etc.)
without anything else changing. config.json therefore stores these two as
lists:

    {
      "weightage_files": ["C:/.../Weightage.xlsx", "C:/.../NewFund_Weightage.xlsx"],
      "nav_files": ["C:/.../Daily_NAV.xlsx", "C:/.../NewFund_NAV.xlsx"],
      "nse_security_master_file": "C:/.../NSE_Security_Master.csv",
      "bse_security_master_file": "C:/.../BSE_Security_Master.csv"
    }

Security Master files (NSE, BSE) are unaffected by this - they remain a
single near-static reference file each, selected once and loaded silently
on every run after that (only re-prompted if the stored path goes missing,
or the user explicitly asks to update it - see update_security_masters()).

Any config.json written by an older version of this tool (single
"weightage_file" / "nav_file" strings) is transparently migrated to the new
list form the first time it's read - see _migrate_legacy_single_file_keys().
"""

import json
from pathlib import Path

BASE_DIR = Path(__file__).parent
CONFIG_PATH = BASE_DIR / "config.json"

# --- keys used inside config.json ------------------------------------------
KEY_WEIGHTAGE_FILES = "weightage_files"
KEY_NAV_FILES = "nav_files"
KEY_NSE_MASTER = "nse_security_master_file"
KEY_BSE_MASTER = "bse_security_master_file"

# Legacy single-file keys, only used for one-time migration of an old
# config.json into the new list-based keys above.
_LEGACY_KEY_WEIGHTAGE = "weightage_file"
_LEGACY_KEY_NAV = "nav_file"

FRIENDLY_NAMES = {
    KEY_WEIGHTAGE_FILES: "Weightage file",
    KEY_NAV_FILES: "Daily NAV file",
    KEY_NSE_MASTER: "NSE Security Master file",
    KEY_BSE_MASTER: "BSE Security Master file",
}

FILE_DIALOG_FILTERS = {
    KEY_WEIGHTAGE_FILES: [("Excel files", "*.xlsx *.xls"), ("All files", "*.*")],
    KEY_NAV_FILES: [("Excel files", "*.xlsx *.xls"), ("All files", "*.*")],
    KEY_NSE_MASTER: [("CSV/Excel files", "*.csv *.xlsx *.xls"), ("All files", "*.*")],
    KEY_BSE_MASTER: [("CSV/Excel files", "*.csv *.xlsx *.xls"), ("All files", "*.*")],
}

# Which of the list-based keys a given "kind" of add maps to, for
# add_fund_files() below.
_MULTI_KEYS = {KEY_WEIGHTAGE_FILES, KEY_NAV_FILES}


class ConfigManager:
    """Owns config.json end to end: read, write, validate, prompt."""

    def __init__(self, config_path: Path = CONFIG_PATH):
        self.config_path = Path(config_path)
        self._config = self._load()
        self._migrate_legacy_single_file_keys()

    # ------------------------------------------------------------------
    # low-level config.json IO
    # ------------------------------------------------------------------
    def _load(self) -> dict:
        if self.config_path.exists():
            try:
                data = json.loads(self.config_path.read_text())
                return data if isinstance(data, dict) else {}
            except (json.JSONDecodeError, OSError):
                print(f"  Warning: {self.config_path} is unreadable/corrupt; "
                      f"starting with an empty configuration.")
                return {}
        return {}

    def _save(self) -> None:
        try:
            self.config_path.write_text(json.dumps(self._config, indent=2, sort_keys=True))
        except OSError as exc:
            print(f"  Warning: could not write {self.config_path} ({exc}); "
                  f"the selected path will only be used for this run.")

    def _migrate_legacy_single_file_keys(self) -> None:
        """A config.json written before multi-fund support used a single
        'weightage_file' / 'nav_file' string. Fold that into the new list
        keys once, so existing users don't lose their configured file(s)."""
        changed = False
        legacy_pairs = [
            (_LEGACY_KEY_WEIGHTAGE, KEY_WEIGHTAGE_FILES),
            (_LEGACY_KEY_NAV, KEY_NAV_FILES),
        ]
        for legacy_key, list_key in legacy_pairs:
            legacy_value = self._config.pop(legacy_key, None)
            if legacy_value:
                existing = self._config.setdefault(list_key, [])
                if legacy_value not in existing:
                    existing.append(legacy_value)
                changed = True
        if changed:
            self._save()

    # ------------------------------------------------------------------
    # validation
    # ------------------------------------------------------------------
    @staticmethod
    def _is_valid(path_str) -> bool:
        return bool(path_str) and Path(path_str).exists()

    # ------------------------------------------------------------------
    # file picker
    # ------------------------------------------------------------------
    def _browse(self, key: str, initial_dir: Path = None) -> str:
        try:
            import tkinter as tk
            from tkinter import filedialog
        except ImportError as exc:
            raise RuntimeError(
                f"Cannot open a file picker for the {FRIENDLY_NAMES.get(key, key)} - "
                f"tkinter is not available in this environment (on Linux you may need "
                f"to install the 'python3-tk' package). As a workaround, either edit "
                f"'{key}' directly in {self.config_path}, or pass the path on the "
                f"command line (see main.py --help)."
            ) from exc

        root = tk.Tk()
        root.withdraw()
        root.attributes("-topmost", True)
        try:
            selected = filedialog.askopenfilename(
                title=f"Select {FRIENDLY_NAMES.get(key, key)}",
                filetypes=FILE_DIALOG_FILTERS.get(key, [("All files", "*.*")]),
                initialdir=str(initial_dir) if initial_dir and initial_dir.exists() else None,
            )
        finally:
            root.destroy()

        if not selected:
            raise FileNotFoundError(
                f"No {FRIENDLY_NAMES.get(key, key)} was selected - cannot continue "
                f"without it."
            )
        return selected

    # ------------------------------------------------------------------
    # generic getter for the two single-file keys (NSE/BSE Security Master)
    # ------------------------------------------------------------------
    def _get_single_path(self, key: str, force_prompt: bool = False) -> Path:
        current = self._config.get(key)

        if not force_prompt and self._is_valid(current):
            return Path(current)

        if force_prompt:
            print(f"  Please select the {FRIENDLY_NAMES.get(key, key)}.")
        elif current and not self._is_valid(current):
            print(f"  Security Master file moved/deleted (expected at: {current}). "
                  f"Please select the {FRIENDLY_NAMES.get(key, key)} again.")
        else:
            print(f"  {FRIENDLY_NAMES.get(key, key)} is not configured yet. "
                  f"Please select it.")

        initial_dir = Path(current).parent if current else BASE_DIR
        selected = self._browse(key, initial_dir=initial_dir)
        self._config[key] = selected
        self._save()
        print(f"  -> {FRIENDLY_NAMES.get(key, key)} set to: {selected}")
        return Path(selected)

    # ------------------------------------------------------------------
    # generic getter for the two list-based keys (Weightage/NAV files)
    # ------------------------------------------------------------------
    def _get_multi_paths(self, key: str) -> list:
        """
        Returns every valid, existing Path currently configured for `key`.
        Any stored path that no longer exists is dropped (with a warning)
        and the cleaned list is persisted, rather than failing the whole
        run over one moved/deleted fund file. If the list is empty after
        that (first run, or every file went missing), the user is prompted
        to select at least one file via the file picker.
        """
        stored = self._config.get(key, [])
        valid, missing = [], []
        for p in stored:
            (valid if self._is_valid(p) else missing).append(p)

        if missing:
            print(f"  Warning: {len(missing)} configured {FRIENDLY_NAMES.get(key, key)}(s) "
                  f"could no longer be found and will be skipped: {missing}")
            self._config[key] = valid
            self._save()

        if not valid:
            print(f"  No {FRIENDLY_NAMES.get(key, key)} configured yet. Please select one "
                  f"(you can add more funds later).")
            selected = self._browse(key, initial_dir=BASE_DIR)
            valid = [selected]
            self._config[key] = valid
            self._save()
            print(f"  -> {FRIENDLY_NAMES.get(key, key)} set to: {selected}")

        return [Path(p) for p in valid]

    # ------------------------------------------------------------------
    # public API - portfolio files (Weightage/NAV - multi-file, one pair
    # per fund or per group of funds)
    # ------------------------------------------------------------------
    def get_weightage_files(self) -> list:
        return self._get_multi_paths(KEY_WEIGHTAGE_FILES)

    def get_nav_files(self) -> list:
        return self._get_multi_paths(KEY_NAV_FILES)

    def add_fund_files(self, weightage_path, nav_path) -> tuple:
        """
        Register another fund's Weightage + Daily NAV Excel files (same
        column format as the existing ones). Both files are added to their
        respective lists in config.json so every subsequent run - and every
        other part of the app (fund dropdown, reports, etc.) - picks up the
        new fund(s) inside them automatically, alongside whatever was
        already configured. Never overwrites/removes anything already
        configured.
        """
        weightage_path = Path(weightage_path)
        nav_path = Path(nav_path)
        if not weightage_path.exists():
            raise FileNotFoundError(f"Weightage file not found: {weightage_path}")
        if not nav_path.exists():
            raise FileNotFoundError(f"Daily NAV file not found: {nav_path}")

        weightage_list = self._config.setdefault(KEY_WEIGHTAGE_FILES, [])
        nav_list = self._config.setdefault(KEY_NAV_FILES, [])

        weightage_str, nav_str = str(weightage_path), str(nav_path)
        if weightage_str not in weightage_list:
            weightage_list.append(weightage_str)
        if nav_str not in nav_list:
            nav_list.append(nav_str)

        self._save()
        print(f"  Added fund files -> Weightage: {weightage_path.name}, "
              f"NAV: {nav_path.name}")
        return weightage_path, nav_path

    # ------------------------------------------------------------------
    # removing a previously-added fund (inverse of add_fund_files) -----
    # ------------------------------------------------------------------
    def _remove_from_multi(self, key: str, path) -> bool:
        """Remove a single path from a list-based config key by comparing
        resolved-ish string form (so 'C:/x/y.xlsx' and 'C:\\x\\y.xlsx' match).
        The file itself is never touched on disk - this only stops the app
        from loading it. Returns True if something was actually removed."""
        target = str(Path(path))
        existing = self._config.get(key, [])
        remaining = [p for p in existing if str(Path(p)) != target]
        removed = len(remaining) != len(existing)
        self._config[key] = remaining
        self._save()
        return removed

    def remove_weightage_file(self, path) -> bool:
        """Remove a single Weightage file from the configuration. Returns
        True if it was found and removed, False if it wasn't configured."""
        removed = self._remove_from_multi(KEY_WEIGHTAGE_FILES, path)
        print(f"  {'Removed' if removed else 'Not configured, nothing to remove:'} "
              f"Weightage file: {Path(path).name}")
        return removed

    def remove_nav_file(self, path) -> bool:
        """Remove a single Daily NAV file from the configuration. Returns
        True if it was found and removed, False if it wasn't configured."""
        removed = self._remove_from_multi(KEY_NAV_FILES, path)
        print(f"  {'Removed' if removed else 'Not configured, nothing to remove:'} "
              f"Daily NAV file: {Path(path).name}")
        return removed

    def remove_fund_files(self, weightage_path, nav_path) -> tuple:
        """
        Un-register a fund by removing its Weightage + Daily NAV file pair
        from the configuration - the inverse of add_fund_files(). Neither
        file is deleted from disk; this only stops the app from loading it
        on future runs. Every fund code that lived only inside the removed
        files simply stops appearing (fund dropdown, reports, etc.) from
        the next load onward. Whatever else is configured is untouched.
        """
        w_removed = self.remove_weightage_file(weightage_path)
        n_removed = self.remove_nav_file(nav_path)
        return w_removed, n_removed

    # ------------------------------------------------------------------
    # public API - Security Master files (prompted once, then silent)
    # ------------------------------------------------------------------
    def get_nse_security_master(self) -> Path:
        return self._get_single_path(KEY_NSE_MASTER)

    def get_bse_security_master(self) -> Path:
        return self._get_single_path(KEY_BSE_MASTER)

    def update_security_masters(self) -> tuple:
        """Explicit user-triggered re-selection of both Security Master
        files (the "Update Security Master Files" action)."""
        nse = self._get_single_path(KEY_NSE_MASTER, force_prompt=True)
        bse = self._get_single_path(KEY_BSE_MASTER, force_prompt=True)
        return nse, bse

    # ------------------------------------------------------------------
    # scripting/automation escape hatch - set a single-file path without a
    # dialog (used by main.py's --nse-master/--bse-master CLI overrides)
    # ------------------------------------------------------------------
    def set_path(self, key: str, path) -> Path:
        if key in _MULTI_KEYS:
            raise KeyError(
                f"'{key}' now holds a list of files - use add_fund_files() instead "
                f"of set_path()."
            )
        if key not in FRIENDLY_NAMES:
            raise KeyError(f"Unknown config key '{key}'")
        resolved = Path(path)
        if not resolved.exists():
            raise FileNotFoundError(f"{FRIENDLY_NAMES[key]} not found at: {resolved}")
        self._config[key] = str(resolved)
        self._save()
        return resolved

    def as_dict(self) -> dict:
        """Read-only snapshot of the current configuration, for display/debugging."""
        return dict(self._config)


# Module-level singleton so every part of the app shares one config/session
# (and only ever opens one dialog per missing file per run).
_manager = None


def get_manager() -> ConfigManager:
    global _manager
    if _manager is None:
        _manager = ConfigManager()
    return _manager
