#!/usr/bin/env python3
"""Reuse an already-open browser tab instead of stacking up new ones.

macOS `open <url>` always makes a NEW tab, so re-serving a site left the user
with a pile of tabs, most of them pointing at dead ports. This module asks the
browsers that are already running whether one of their tabs is showing the site
(its current URL, or an earlier URL from a previous server run on another port)
and, if so, points that tab at the new URL and focuses it.

Everything here is best-effort and time-boxed: a browser that is not running is
never launched, osascript is run with a timeout, and any error maps to a status
the caller can fall back on. Nothing raises.
"""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit


# Seconds any single osascript call may take before it is killed. AppleScript to
# a busy browser is normally milliseconds; the cap is there so a wedged or
# permission-prompting browser can never stall a server start.
OSASCRIPT_TIMEOUT = 4.0

# Scriptable everyday browsers, by the process name pgrep sees, mapped to the
# AppleScript application name and dialect. Chromium and Safari differ in how a
# tab is made current; everything else about the two scripts is the same. Testing
# builds are deliberately absent — see TESTING_BUNDLE_IDS.
BROWSERS: list[tuple[str, str, str, str]] = [
    # (bundle id, process name, AppleScript app name, dialect)
    ("com.google.chrome", "Google Chrome", "Google Chrome", "chromium"),
    ("com.brave.browser", "Brave Browser", "Brave Browser", "chromium"),
    ("com.microsoft.edgemac", "Microsoft Edge", "Microsoft Edge", "chromium"),
    ("com.vivaldi.vivaldi", "Vivaldi", "Vivaldi", "chromium"),
    ("com.apple.safari", "Safari", "Safari", "safari"),
]

LAUNCH_SERVICES_PLIST = (
    "~/Library/Preferences/com.apple.LaunchServices/com.apple.launchservices.secure.plist"
)

# Browser builds that exist to be driven by test harnesses. A person's tabs are
# never in one of these, so a webdoc preview must never land there — even if one
# of them has somehow become the registered URL handler.
TESTING_BUNDLE_IDS = {"com.google.chrome.for.testing", "org.chromium.chromium"}

# The everyday browser to fall back on when the registered handler is a testing
# build or cannot be read. Checked for existence before it is used.
FALLBACK_BROWSER = ("com.google.Chrome", "/Applications/Google Chrome.app")

# Command-line flags that mark a browser process as one a test harness launched.
# An instance carrying these is driving a throwaway profile, not the user's.
AUTOMATION_MARKERS = (
    "--remote-debugging-pipe",
    "--remote-debugging-port",
    "--enable-automation",
    "--headless",
    "--test-type",
    "ms-playwright",
    "puppeteer",
    "selenium",
)

# "reused"      - an existing tab now shows the URL and is focused
# "none"        - the browsers we could talk to have no tab for this site
# "unavailable" - nobody could answer (no scriptable browser running, the
#                 addressed instance had no windows, automation denied, timeout)
REUSED, NONE, UNAVAILABLE = "reused", "none", "unavailable"


def origins(url: str) -> list[str]:
    """URL prefixes that identify this site's pages in a tab list.

    A tab may sit on a sub-page of the site, and 127.0.0.1 and localhost are the
    same server with different spellings, so match on origin rather than on the
    exact URL.
    """
    parts = urlsplit(url)
    if not parts.scheme or not parts.netloc:
        return []
    hosts = [parts.hostname or ""]
    if parts.hostname in ("127.0.0.1", "localhost"):
        hosts = ["127.0.0.1", "localhost"]
    port = f":{parts.port}" if parts.port else ""
    out: list[str] = []
    for host in hosts:
        if not host:
            continue
        prefix = urlunsplit((parts.scheme, f"{host}{port}", "/", "", ""))
        if prefix not in out:
            out.append(prefix)
    return out


def match_prefixes(url: str, aliases: "list[str] | tuple[str, ...]" = ()) -> list[str]:
    """Origins for the current URL plus every earlier URL the site was served on,
    so a tab left over from a previous port is recognised and retargeted."""
    out = origins(url)
    for alias in aliases:
        for prefix in origins(alias):
            if prefix not in out:
                out.append(prefix)
    return out


def default_browser_bundle_id() -> "str | None":
    """The bundle id handling http:, lowercased, or None if it cannot be read."""
    path = Path(LAUNCH_SERVICES_PLIST).expanduser()
    try:
        raw = subprocess.run(
            ["plutil", "-convert", "json", "-o", "-", str(path)],
            capture_output=True, text=True, timeout=OSASCRIPT_TIMEOUT, check=True,
        ).stdout
        handlers = json.loads(raw).get("LSHandlers", [])
    except (OSError, subprocess.SubprocessError, ValueError):
        return None
    for entry in handlers:
        if not isinstance(entry, dict):
            continue
        if entry.get("LSHandlerURLScheme") in ("http", "https"):
            role = entry.get("LSHandlerRoleAll") or entry.get("LSHandlerRoleViewer")
            if isinstance(role, str):
                return role.lower()
    return None


def is_automation_process(command: str) -> bool:
    return any(marker in command for marker in AUTOMATION_MARKERS)


def has_user_instance(proc_name: str) -> bool:
    """Whether this browser has at least one instance that a person is using.

    Playwright and friends launch the very same app bundle with a throwaway
    profile; those instances answer AppleScript and must never have a tab
    retargeted in them. When every running instance looks automated, the browser
    is treated as not running at all.
    """
    try:
        # -x matches the process name exactly, which already excludes the "…
        # Helper" renderer and GPU processes; -f would match argv instead and
        # never hit, since every instance carries arguments.
        pids = subprocess.run(
            ["pgrep", "-x", proc_name], capture_output=True, text=True, timeout=OSASCRIPT_TIMEOUT
        ).stdout.split()
    except (OSError, subprocess.SubprocessError):
        return False
    for pid in pids:
        if not pid.isdigit():
            continue
        try:
            command = subprocess.run(
                ["ps", "-o", "command=", "-p", pid],
                capture_output=True, text=True, timeout=OSASCRIPT_TIMEOUT,
            ).stdout.strip()
        except (OSError, subprocess.SubprocessError):
            continue
        if command and not is_automation_process(command):
            return True
    return False


def open_url(url: str) -> bool:
    """Open a URL in the user's everyday browser.

    Bare `open` hands the URL to whatever LaunchServices has registered, which is
    how a preview can end up in a testing build. Pin the bundle instead, and
    refuse to hand a URL to a testing browser.
    """
    bundle = default_browser_bundle_id()
    if bundle in TESTING_BUNDLE_IDS or not bundle:
        fallback_id, fallback_path = FALLBACK_BROWSER
        bundle = fallback_id if Path(fallback_path).exists() else None
    commands = []
    if bundle:
        commands.append(["open", "-b", bundle, url])
    commands.append(["open", url])
    commands.append(["xdg-open", url])
    for cmd in commands:
        try:
            proc = subprocess.run(cmd, capture_output=True, timeout=OSASCRIPT_TIMEOUT)
        except (OSError, subprocess.SubprocessError):
            continue
        if proc.returncode == 0:
            return True
    try:
        import webbrowser

        return webbrowser.open(url)
    except Exception:
        return False


def running_browsers() -> list[tuple[str, str, bool]]:
    """(AppleScript app name, dialect, is_default) for every supported browser
    already running, default browser first. pgrep, not AppleScript: addressing a
    browser that is not running would launch it."""
    found: list[tuple[str, str, str]] = []
    for bundle_id, proc_name, app_name, dialect in BROWSERS:
        if bundle_id in TESTING_BUNDLE_IDS:
            continue
        if has_user_instance(proc_name):
            found.append((bundle_id, app_name, dialect))
    default_id = default_browser_bundle_id()
    found.sort(key=lambda item: 0 if item[0] == default_id else 1)
    return [(app_name, dialect, bundle_id == default_id) for bundle_id, app_name, dialect in found]


def _reuse_script(app_name: str, dialect: str) -> str:
    """AppleScript that retargets and focuses the first matching tab.

    Returns "reused", "none", or "empty" — "empty" meaning the instance that
    answered has no windows at all, which on a Mac running headless automation
    copies of the same browser means the answer came from the wrong instance and
    says nothing about the user's tabs.
    """
    if dialect == "safari":
        focus = "set current tab of w to t"
    else:
        focus = "set active tab index of w to idx"
    return f'''
on run argv
	set targetURL to item 1 of argv
	set prefixes to rest of argv
	tell application "{app_name}"
		if (count of windows) is 0 then return "empty"
		repeat with w in windows
			set idx to 0
			repeat with t in tabs of w
				set idx to idx + 1
				set u to (URL of t) as text
				repeat with p in prefixes
					if u starts with p then
						set URL of t to targetURL
						{focus}
						set index of w to 1
						activate
						return "reused"
					end if
				end repeat
			end repeat
		end repeat
	end tell
	return "none"
end run
'''


def _close_script(app_name: str) -> str:
    """AppleScript that closes every tab showing one of the prefixes. Tabs are
    collected before any is closed, because closing shifts the live indexes."""
    return f'''
on run argv
	set doomed to {{}}
	tell application "{app_name}"
		if (count of windows) is 0 then return "0"
		repeat with w in windows
			repeat with t in tabs of w
				set u to (URL of t) as text
				repeat with p in argv
					if u starts with p then
						set end of doomed to t
						exit repeat
					end if
				end repeat
			end repeat
		end repeat
		set n to 0
		repeat with t in doomed
			try
				close t
				set n to n + 1
			end try
		end repeat
		return n as text
	end tell
end run
'''


def _run_osascript(script: str, args: list[str]) -> "str | None":
    """Run an AppleScript, returning its trimmed output or None on any failure."""
    try:
        proc = subprocess.run(
            ["osascript", "-", *args],
            input=script, capture_output=True, text=True, timeout=OSASCRIPT_TIMEOUT,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if proc.returncode != 0:
        return None
    return proc.stdout.strip()


def reuse_tab(url: str, aliases: "list[str] | tuple[str, ...]" = ()) -> str:
    """Point an already-open tab for this site at url and focus it.

    Returns REUSED, NONE (no such tab in a browser that answered), or
    UNAVAILABLE (nothing could be determined — the caller decides what to do).
    """
    if os.uname().sysname != "Darwin":
        return UNAVAILABLE
    prefixes = match_prefixes(url, aliases)
    if not prefixes:
        return UNAVAILABLE
    answered = False
    default_running = False
    default_answered = False
    for app_name, dialect, is_default in running_browsers():
        default_running = default_running or is_default
        result = _run_osascript(_reuse_script(app_name, dialect), [url, *prefixes])
        if result == "reused":
            return REUSED
        if result == "none":
            answered = True
            default_answered = default_answered or is_default
    # A new tab would land in the default browser, so only that browser's answer
    # rules out a reusable tab. Another browser saying "no tab here" tells us
    # nothing about the one the URL is about to open in.
    if default_running:
        return NONE if default_answered else UNAVAILABLE
    if default_browser_bundle_id() in {b[0] for b in BROWSERS}:
        # We know the default browser and it is not running: it cannot be holding
        # a tab, so a fresh one is right.
        return NONE
    return NONE if answered else UNAVAILABLE


def close_tabs(urls: "list[str] | tuple[str, ...]") -> int:
    """Close tabs showing any of these URLs' origins. Best-effort; returns how
    many tabs were closed (0 when nothing could be reached)."""
    if os.uname().sysname != "Darwin":
        return 0
    prefixes: list[str] = []
    for url in urls:
        for prefix in origins(url):
            if prefix not in prefixes:
                prefixes.append(prefix)
    if not prefixes:
        return 0
    closed = 0
    for app_name, _dialect, _is_default in running_browsers():
        result = _run_osascript(_close_script(app_name), prefixes)
        try:
            closed += int(result or 0)
        except ValueError:
            pass
    return closed


if __name__ == "__main__":
    import sys

    if len(sys.argv) < 2:
        raise SystemExit("usage: browser_tabs.py <url> [alias-url ...]")
    print(json.dumps({
        "default_browser": default_browser_bundle_id(),
        "running": running_browsers(),
        "prefixes": match_prefixes(sys.argv[1], sys.argv[2:]),
        "result": reuse_tab(sys.argv[1], sys.argv[2:]),
    }, indent=2))
