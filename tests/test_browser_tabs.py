#!/usr/bin/env python3
"""Tests for one-tab-per-site presentation: origin matching for stale tabs, the
which-browser-is-authoritative decision, the bookkeeping fallback used when no
browser can be asked, and serve_site.present_site never opening a second tab.

No browser is launched and no AppleScript runs: the osascript layer is stubbed.
Pure stdlib. Run directly:

    python3 tests/test_browser_tabs.py
"""

from __future__ import annotations

import sys
import tempfile
import time
import traceback
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
sys.path.insert(0, str(SCRIPTS))

import browser_tabs  # noqa: E402
import serve_site  # noqa: E402

TESTS: list[tuple[str, "callable"]] = []


def test(name: str):
    def deco(fn):
        TESTS.append((name, fn))
        return fn
    return deco


def eq(got, want, what=""):
    assert got == want, f"{what}\n  got:  {got!r}\n  want: {want!r}"


class StubBrowsers:
    """Stands in for the running browsers and their AppleScript replies.

    replies maps an app name to what its script returns: "reused", "none",
    "empty" (an instance with no windows answered, so it knows nothing about the
    user's tabs), or None (timeout / denied / error).
    """

    def __init__(self, browsers, replies):
        self.browsers = browsers
        self.replies = replies
        self.asked: list[str] = []
        self.args: "list[str]" = []

    def __enter__(self):
        self._saved = (browser_tabs.running_browsers, browser_tabs._run_osascript,
                       browser_tabs.default_browser_bundle_id)
        browser_tabs.running_browsers = lambda: self.browsers
        browser_tabs.default_browser_bundle_id = lambda: "com.google.chrome"

        def fake(script, args):
            app = next(name for name, _d, _def in self.browsers if f'"{name}"' in script)
            self.asked.append(app)
            self.args = args
            return self.replies.get(app)

        browser_tabs._run_osascript = fake
        return self

    def __exit__(self, *exc):
        (browser_tabs.running_browsers, browser_tabs._run_osascript,
         browser_tabs.default_browser_bundle_id) = self._saved
        return False


CHROME = ("Google Chrome", "chromium", True)
SAFARI = ("Safari", "safari", False)


@test("origins: a loopback URL matches both spellings of the host")
def _origins():
    eq(browser_tabs.origins("http://127.0.0.1:5000/report.html"),
       ["http://127.0.0.1:5000/", "http://localhost:5000/"])


@test("match_prefixes: an earlier port is still recognised as this site")
def _aliases():
    got = browser_tabs.match_prefixes("http://127.0.0.1:5000/", ["http://127.0.0.1:4000/"])
    assert "http://127.0.0.1:4000/" in got, got
    assert "http://127.0.0.1:5000/" in got, got


@test("reuse: a matching tab is retargeted, and no further browser is asked")
def _reuse():
    with StubBrowsers([CHROME, SAFARI], {"Google Chrome": "reused"}) as stub:
        eq(browser_tabs.reuse_tab("http://127.0.0.1:5000/"), browser_tabs.REUSED)
        eq(stub.asked, ["Google Chrome"], "stopped at the first browser that reused a tab")
        eq(stub.args[0], "http://127.0.0.1:5000/", "the tab is pointed at the current URL")


@test("reuse: the default browser saying 'no tab' means open a new one")
def _none():
    with StubBrowsers([CHROME], {"Google Chrome": "none"}):
        eq(browser_tabs.reuse_tab("http://127.0.0.1:5000/"), browser_tabs.NONE)


@test("reuse: another browser's 'no tab' does not speak for the default browser")
def _not_authoritative():
    # Chrome is the default and answered from a windowless instance (a headless
    # automation copy), so its tabs are unknown even though Safari answered.
    with StubBrowsers([CHROME, SAFARI], {"Google Chrome": "empty", "Safari": "none"}):
        eq(browser_tabs.reuse_tab("http://127.0.0.1:5000/"), browser_tabs.UNAVAILABLE)


@test("reuse: a browser that fails outright leaves the answer unknown")
def _failure():
    with StubBrowsers([CHROME], {"Google Chrome": None}):
        eq(browser_tabs.reuse_tab("http://127.0.0.1:5000/"), browser_tabs.UNAVAILABLE)


@test("reuse: the default browser not running at all means there is no tab")
def _default_not_running():
    with StubBrowsers([SAFARI], {"Safari": "none"}):
        eq(browser_tabs.reuse_tab("http://127.0.0.1:5000/"), browser_tabs.NONE)


@test("assume_tab_open: only for the same URL, and only while it is recent")
def _assume():
    with tempfile.TemporaryDirectory() as tmp:
        site = Path(tmp)
        url = "http://127.0.0.1:5000/"
        eq(serve_site.assume_tab_open(site, url), False, "nothing opened yet")
        serve_site.record_tab_open(site, url, 4242)
        eq(serve_site.assume_tab_open(site, url), True)
        eq(serve_site.assume_tab_open(site, "http://127.0.0.1:6000/"), False, "different URL")
        stale = time.time() + serve_site.TAB_ASSUME_OPEN_SECONDS + 60
        eq(serve_site.assume_tab_open(site, url, now=stale), False, "yesterday's tab is not assumed")


@test("present_site: repeated starts reuse one tab and never open a second")
def _present():
    with tempfile.TemporaryDirectory() as tmp:
        site = Path(tmp)
        info = {"url": "http://127.0.0.1:5000/", "pid": 999}
        opened: list[str] = []
        saved_open, saved_reuse = serve_site.open_url, browser_tabs.reuse_tab
        try:
            serve_site.open_url = lambda url: opened.append(url) or True

            browser_tabs.reuse_tab = lambda url, aliases=(): browser_tabs.NONE
            eq(serve_site.present_site(site, info, forced=False), "opened", "first start opens the tab")

            # No browser can be asked from here on: the site's own record of what
            # it opened has to be what stops the duplicate.
            browser_tabs.reuse_tab = lambda url, aliases=(): browser_tabs.UNAVAILABLE
            eq(serve_site.present_site(site, info, forced=False), "already-open")
            eq(serve_site.present_site(site, dict(info, pid=1000), forced=False), "already-open",
               "a restarted server on the same URL still has the same tab")
            eq(len(opened), 1, "exactly one tab was ever opened")

            # A live browser that does have the tab wins over the bookkeeping.
            browser_tabs.reuse_tab = lambda url, aliases=(): browser_tabs.REUSED
            eq(serve_site.present_site(site, info, forced=False), "reused")
            eq(len(opened), 1, "reuse opens nothing")

            # --open is the escape hatch when the user closed the tab.
            browser_tabs.reuse_tab = lambda url, aliases=(): browser_tabs.UNAVAILABLE
            eq(serve_site.present_site(site, info, forced=True), "opened")
            eq(len(opened), 2)
        finally:
            serve_site.open_url, browser_tabs.reuse_tab = saved_open, saved_reuse


@test("present_site: a previous run's URL is offered as an alias to match on")
def _alias_passthrough():
    with tempfile.TemporaryDirectory() as tmp:
        site = Path(tmp)
        serve_site.record_tab_open(site, "http://127.0.0.1:4000/", 1)
        seen: dict[str, object] = {}
        saved_open, saved_reuse = serve_site.open_url, browser_tabs.reuse_tab
        try:
            serve_site.open_url = lambda url: True

            def fake_reuse(url, aliases=()):
                seen["aliases"] = list(aliases)
                return browser_tabs.REUSED

            browser_tabs.reuse_tab = fake_reuse
            serve_site.present_site(site, {"url": "http://127.0.0.1:5000/", "pid": 7}, forced=False)
            eq(seen["aliases"], ["http://127.0.0.1:4000/"])
        finally:
            serve_site.open_url, browser_tabs.reuse_tab = saved_open, saved_reuse


class StubRun:
    """Replaces subprocess.run in browser_tabs. `pgrep` answers from pids, `ps`
    from commands; everything else records the call and succeeds."""

    def __init__(self, pids=(), commands=None):
        self.pids = list(pids)
        self.commands = commands or {}
        self.calls: list[list[str]] = []

    def __enter__(self):
        self._saved = browser_tabs.subprocess.run
        browser_tabs.subprocess.run = self
        return self

    def __exit__(self, *exc):
        browser_tabs.subprocess.run = self._saved
        return False

    def __call__(self, cmd, **kwargs):
        self.calls.append(list(cmd))

        class Result:
            returncode = 0
            stdout = ""

        result = Result()
        if cmd[0] == "pgrep":
            result.stdout = "\n".join(self.pids)
        elif cmd[0] == "ps":
            result.stdout = self.commands.get(cmd[-1], "")
        return result


CHROME_APP = "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"
CHROME_AUTOMATED = CHROME_APP + " --remote-debugging-pipe --user-data-dir=/tmp/ms-playwright-mcp/x"


@test("a browser running only under test automation counts as not running")
def _automation_only():
    with StubRun(pids=["1738"], commands={"1738": CHROME_AUTOMATED}):
        eq(browser_tabs.has_user_instance("Google Chrome"), False)
    with StubRun(pids=["1738", "735"], commands={"1738": CHROME_AUTOMATED, "735": CHROME_APP}):
        eq(browser_tabs.has_user_instance("Google Chrome"), True, "the everyday instance still counts")


@test("testing builds are never a target for reuse")
def _no_testing_targets():
    ids = {bundle_id for bundle_id, *_ in browser_tabs.BROWSERS}
    assert not (ids & browser_tabs.TESTING_BUNDLE_IDS), ids


@test("open pins the browser bundle instead of letting LaunchServices choose")
def _open_pins_bundle():
    saved = browser_tabs.default_browser_bundle_id
    try:
        browser_tabs.default_browser_bundle_id = lambda: "com.google.chrome"
        with StubRun() as stub:
            eq(browser_tabs.open_url("http://127.0.0.1:5000/"), True)
            eq(stub.calls[0], ["open", "-b", "com.google.chrome", "http://127.0.0.1:5000/"])

        # A testing build registered as the URL handler must not receive it.
        browser_tabs.default_browser_bundle_id = lambda: "com.google.chrome.for.testing"
        with StubRun() as stub:
            browser_tabs.open_url("http://127.0.0.1:5000/")
            bundle = stub.calls[0][2] if stub.calls[0][1] == "-b" else None
            assert bundle != "com.google.chrome.for.testing", stub.calls[0]
    finally:
        browser_tabs.default_browser_bundle_id = saved


@test("tabs.json is a control file the server refuses to serve")
def _control_file():
    assert "tabs.json" in serve_site.NoListingHandler._CONTROL_FILES


def main() -> int:
    print(f"browser tab suite  ({len(TESTS)} tests)")
    print(f"  modules: {SCRIPTS}")
    print("-" * 72)
    passed, failed = [], []
    for name, fn in TESTS:
        try:
            fn()
        except AssertionError as exc:
            failed.append((name, str(exc)))
            print(f"FAIL  {name}")
        except Exception:
            failed.append((name, traceback.format_exc()))
            print(f"ERROR {name}")
        else:
            passed.append(name)
            print(f"ok    {name}")
    print("-" * 72)
    if failed:
        print("\nFAILURES:\n")
        for name, detail in failed:
            print(f"### {name}")
            for line in detail.rstrip().splitlines():
                print(f"    {line}")
            print()
    print(f"summary: {len(passed)} passed, {len(failed)} failed, {len(TESTS)} total")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
