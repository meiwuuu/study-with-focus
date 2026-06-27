#!/usr/bin/env python3
"""Launch focus tool with user's preferred browser.

Reads config.json for the browser setting, scans installed browsers,
and opens index.html with the selected browser.
Falls back to system default if the chosen browser is not found.
"""
import json
import os
import subprocess
import sys
from pathlib import Path

DATA_DIR = Path(__file__).parent
CONFIG_FILE = DATA_DIR / "config.json"
INDEX_URL = "file:///" + str((DATA_DIR / "index.html").as_posix())

# Browser executable paths (common Windows install locations)
BROWSER_PATHS = {
    "chrome": [
        r"C:\Program Files\Google\Chrome\Application\chrome.exe",
        r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
    ],
    "edge": [
        r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
        r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
    ],
    "firefox": [
        r"C:\Program Files\Mozilla Firefox\firefox.exe",
        r"C:\Program Files (x86)\Mozilla Firefox\firefox.exe",
    ],
    "brave": [
        r"C:\Program Files\BraveSoftware\Brave-Browser\Application\brave.exe",
    ],
    "vivaldi": [
        os.path.expandvars(r"%LOCALAPPDATA%\Vivaldi\Application\vivaldi.exe"),
    ],
    "opera": [
        os.path.expandvars(r"%LOCALAPPDATA%\Programs\Opera\opera.exe"),
    ],
}


def find_browser(browser_name):
    """Find executable path for browser, or None."""
    paths = BROWSER_PATHS.get(browser_name, [])
    for p in paths:
        if os.path.isfile(p):
            return p
    return None


def get_configured_browser():
    """Read config.json and return the browser setting."""
    if CONFIG_FILE.exists():
        try:
            with open(CONFIG_FILE, "r", encoding="utf-8") as f:
                config = json.load(f)
            return config.get("browser", "system")
        except (json.JSONDecodeError, ValueError):
            pass
    return "system"


def main():
    browser_name = get_configured_browser()

    if browser_name == "system":
        # Use system default
        os.startfile(str(DATA_DIR / "index.html"))
        print(f"Opened with system default browser")
        return 0

    exe = find_browser(browser_name)

    if exe:
        # Use --app mode for app-like experience on Chrome/Edge/Brave
        if browser_name in ("chrome", "edge", "brave"):
            subprocess.Popen(
                [exe, "--app=" + INDEX_URL],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
        else:
            subprocess.Popen(
                [exe, INDEX_URL],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
        print(f"Opened with {browser_name}: {exe}")
        return 0

    # Browser not found, fall back to system default
    print(f"WARNING: {browser_name} not found. Falling back to system default.")
    os.startfile(str(DATA_DIR / "index.html"))
    return 1


if __name__ == "__main__":
    sys.exit(main())
