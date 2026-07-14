#!/usr/bin/env python3
"""Tests for the serve_site.py preview lifecycle: the 7-day TTL default, the
heartbeat endpoint + server-side snippet injection, the idle-shutdown watchdog,
and the control-file GET/HEAD 404 guard.

Some cases stand up a real loopback server on an OS-assigned port and drive it
with urllib; the idle/TTL cases run the real run_server watchdog in a thread with
sub-second timings. Pure stdlib. Run directly:

    python3 tests/test_serve_lifecycle.py
"""

from __future__ import annotations

import functools
import json
import sys
import tempfile
import threading
import time
import traceback
import urllib.error
import urllib.request
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
sys.path.insert(0, str(SCRIPTS))

import serve_site  # noqa: E402

TESTS: list[tuple[str, "callable"]] = []


def test(name: str):
    def deco(fn):
        TESTS.append((name, fn))
        return fn
    return deco


def eq(got, want, what=""):
    assert got == want, f"{what}\n  got:  {got!r}\n  want: {want!r}"


INDEX_HTML = "<!doctype html><html><head><title>t</title></head><body><h1>Hi</h1></body></html>"


def make_site() -> Path:
    """A temp site dir with the page assets plus every control file the guard
    must refuse to serve."""
    d = Path(tempfile.mkdtemp(prefix="webdoc-lifecycle-"))
    (d / "index.html").write_text(INDEX_HTML, encoding="utf-8")
    (d / "doc.html").write_text("<html><body>doc</body></html>", encoding="utf-8")
    (d / "style.css").write_text("body{color:#000}", encoding="utf-8")
    (d / "edit.js").write_text("// edit", encoding="utf-8")
    (d / "manifest.json").write_text('{"artifact_id":"x"}', encoding="utf-8")
    (d / "feedback.jsonl").write_text("", encoding="utf-8")
    (d / "server.json").write_text("{}", encoding="utf-8")
    (d / "server.log").write_text("log line\n", encoding="utf-8")
    (d / "doc.edits.json").write_text("[]", encoding="utf-8")
    (d / ".secret").write_text("nope", encoding="utf-8")
    # An .html file with no </body>, to prove the snippet is appended at the end.
    (d / "bare.html").write_text("<html><p>no body tag</p></html>", encoding="utf-8")
    return d


def live(site: Path, idle_enabled: bool = True):
    """A real server bound to an OS port, serving in a daemon thread. Returns
    (httpd, port, thread); stop with httpd.shutdown(); thread.join()."""
    handler = functools.partial(serve_site.NoListingHandler, directory=str(site))
    httpd = serve_site.ThreadingHTTPServer(("127.0.0.1", 0), handler)
    httpd.idle_shutdown_enabled = idle_enabled
    port = int(httpd.server_address[1])
    t = threading.Thread(target=httpd.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True)
    t.start()
    return httpd, port, t


def http_get(port: int, path: str, method: str = "GET"):
    req = urllib.request.Request(f"http://127.0.0.1:{port}{path}", method=method)
    try:
        with urllib.request.urlopen(req, timeout=5) as r:
            return r.status, dict(r.headers), r.read()
    except urllib.error.HTTPError as e:
        return e.code, dict(e.headers), e.read()


def http_post(port: int, path: str, body: bytes = b""):
    req = urllib.request.Request(f"http://127.0.0.1:{port}{path}", data=body, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=5) as r:
            return r.status, dict(r.headers), r.read()
    except urllib.error.HTTPError as e:
        return e.code, dict(e.headers), e.read()


def wait_for_port(site: Path, timeout: float = 5.0) -> int:
    deadline = time.time() + timeout
    while time.time() < deadline:
        info = serve_site.load_server_info(site)
        if info and info.get("port"):
            return int(info["port"])
        time.sleep(0.02)
    raise AssertionError("server did not report a port in server.json")


# --------------------------------------------------------------------------- #
# Constants + pure helpers
# --------------------------------------------------------------------------- #

@test("DEFAULT_TTL_SECONDS is 7 days; idle default is 30 minutes")
def _():
    eq(serve_site.DEFAULT_TTL_SECONDS, 604800, "7-day TTL")
    eq(serve_site.DEFAULT_TTL_SECONDS, 7 * 24 * 60 * 60)
    eq(serve_site.DEFAULT_IDLE_TIMEOUT_SECONDS, 30 * 60, "30-minute idle default")


@test("inject_heartbeat: snippet before the last </body>, appended when absent")
def _():
    out = serve_site.inject_heartbeat(b"<html><body>x</body></html>")
    eq(out.count(b"/api/heartbeat"), 1, "one snippet")
    assert out.index(b"/api/heartbeat") < out.rindex(b"</body>"), "before </body>"
    # No </body>: appended at the very end.
    out2 = serve_site.inject_heartbeat(b"<html><p>x</p></html>")
    assert out2.startswith(b"<html><p>x</p></html>"), out2
    assert out2.rstrip().endswith(b"</script>"), out2
    # Case-insensitive, and only the LAST close-body gets the snippet.
    out3 = serve_site.inject_heartbeat(b"<BODY>a</BODY><body>b</BODY>")
    eq(out3.count(b"/api/heartbeat"), 1)
    assert out3.index(b"/api/heartbeat") < out3.rindex(b"</BODY>")


# --------------------------------------------------------------------------- #
# Activity tracking + heartbeat endpoint
# --------------------------------------------------------------------------- #

@test("ThreadingHTTPServer.touch/idle_seconds track activity")
def _():
    handler = functools.partial(serve_site.NoListingHandler, directory=str(make_site()))
    httpd = serve_site.ThreadingHTTPServer(("127.0.0.1", 0), handler)
    try:
        i0 = httpd.idle_seconds()
        time.sleep(0.04)
        assert httpd.idle_seconds() > i0, "idle grows with time"
        httpd.touch()
        assert httpd.idle_seconds() < 0.03, "touch resets the clock"
    finally:
        httpd.server_close()


@test("POST /api/heartbeat returns 200 {ok:true} and the request touches activity")
def _():
    site = make_site()
    httpd, port, t = live(site)
    try:
        time.sleep(0.06)
        before = httpd.idle_seconds()
        assert before > 0.04, before
        status, headers, body = http_post(port, "/api/heartbeat")
        eq(status, 200, "heartbeat ok")
        eq(json.loads(body), {"ok": True}, "heartbeat body")
        assert httpd.idle_seconds() < before, "the heartbeat request touched activity"
    finally:
        httpd.shutdown()
        t.join(timeout=5)


@test("heartbeat body cap: an oversized body is rejected 413, a small one is fine")
def _():
    site = make_site()
    httpd, port, t = live(site)
    try:
        status, _, _ = http_post(port, "/api/heartbeat", body=b"x" * 5000)
        eq(status, 413, "over-cap body rejected")
        status2, _, body2 = http_post(port, "/api/heartbeat", body=b'{"beat":1}')
        eq(status2, 200, "small JSON body ok")
        eq(json.loads(body2), {"ok": True})
        # A zero-length, non-JSON body is tolerated too.
        status3, _, _ = http_post(port, "/api/heartbeat", body=b"")
        eq(status3, 200, "empty body ok")
    finally:
        httpd.shutdown()
        t.join(timeout=5)


# --------------------------------------------------------------------------- #
# Heartbeat snippet injection
# --------------------------------------------------------------------------- #

@test("GET index.html is served with the heartbeat snippet exactly once, Content-Length matches")
def _():
    site = make_site()
    httpd, port, t = live(site, idle_enabled=True)
    try:
        for path in ("/", "/index.html"):
            status, headers, body = http_get(port, path)
            eq(status, 200, path)
            assert headers.get("Content-Type", "").startswith("text/html"), (path, headers)
            eq(body.count(b"/api/heartbeat"), 1, f"one snippet in {path}")
            assert body.lower().index(b"/api/heartbeat") < body.lower().rindex(b"</body>"), path
            eq(int(headers["Content-Length"]), len(body), f"Content-Length matches body for {path}")
    finally:
        httpd.shutdown()
        t.join(timeout=5)


@test("HEAD of an HTML page reports the same Content-Length its GET sends")
def _():
    site = make_site()
    httpd, port, t = live(site, idle_enabled=True)
    try:
        get_status, get_headers, get_body = http_get(port, "/index.html")
        head_status, head_headers, head_body = http_get(port, "/index.html", method="HEAD")
        eq(get_status, 200)
        eq(head_status, 200)
        eq(head_body, b"", "HEAD sends no body")
        eq(int(head_headers["Content-Length"]), len(get_body),
           "HEAD Content-Length equals the injected GET body length")
    finally:
        httpd.shutdown()
        t.join(timeout=5)


@test("doc.html is served byte-identical: no snippet, on GET or HEAD")
def _():
    site = make_site()
    httpd, port, t = live(site, idle_enabled=True)
    try:
        on_disk = (site / "doc.html").read_bytes()
        status, headers, body = http_get(port, "/doc.html")
        eq(status, 200)
        assert b"/api/heartbeat" not in body, "doc export must stay script-free"
        eq(body, on_disk, "served doc.html identical to the on-disk export")
        head_headers = http_get(port, "/doc.html", method="HEAD")[1]
        eq(int(head_headers["Content-Length"]), len(on_disk), "HEAD matches the raw file")
    finally:
        httpd.shutdown()
        t.join(timeout=5)


@test("GET a non-HTML file is served unmodified (no snippet)")
def _():
    site = make_site()
    httpd, port, t = live(site, idle_enabled=True)
    try:
        status, headers, body = http_get(port, "/style.css")
        eq(status, 200)
        assert b"/api/heartbeat" not in body, "no snippet in CSS"
        eq(body, b"body{color:#000}", "css byte-identical")
    finally:
        httpd.shutdown()
        t.join(timeout=5)


@test("GET an HTML file without </body> gets the snippet appended at the end")
def _():
    site = make_site()
    httpd, port, t = live(site, idle_enabled=True)
    try:
        status, headers, body = http_get(port, "/bare.html")
        eq(status, 200)
        eq(body.count(b"/api/heartbeat"), 1)
        assert body.startswith(b"<html><p>no body tag</p></html>"), body
        assert body.rstrip().endswith(b"</script>"), body
    finally:
        httpd.shutdown()
        t.join(timeout=5)


# --------------------------------------------------------------------------- #
# Control-file + dotfile guard (P0)
# --------------------------------------------------------------------------- #

@test("GET control files 404 while page assets keep serving")
def _():
    site = make_site()
    httpd, port, t = live(site, idle_enabled=True)
    try:
        for name in ("server.json", "server.log", "feedback.jsonl", "manifest.json", "doc.edits.json"):
            eq(http_get(port, "/" + name)[0], 404, f"/{name} must 404")
        # The bundled page assets are unaffected.
        eq(http_get(port, "/index.html")[0], 200, "index.html serves")
        eq(http_get(port, "/doc.html")[0], 200, "doc.html serves")
        eq(http_get(port, "/style.css")[0], 200, "style.css serves")
        eq(http_get(port, "/edit.js")[0], 200, "edit.js serves")
        # /api GET routes still work.
        eq(http_get(port, "/api/health")[0], 200, "/api/health serves")
    finally:
        httpd.shutdown()
        t.join(timeout=5)


@test("case-varied control-file paths 404 on GET and HEAD (macOS case-insensitive FS)")
def _():
    # macOS's default filesystem opens SERVER.JSON as server.json while
    # Path.resolve() preserves the requested case, so the guard must compare
    # case-insensitively. On a case-sensitive filesystem these names simply do
    # not exist, so 404 is the right answer everywhere.
    site = make_site()
    httpd, port, t = live(site, idle_enabled=True)
    try:
        for name in ("SERVER.JSON", "Server.json", "sErVeR.jSoN", "SERVER.LOG",
                     "FEEDBACK.JSONL", "Manifest.json", "MANIFEST.JSON",
                     "doc.EDITS.json", "DOC.EDITS.JSON"):
            eq(http_get(port, "/" + name)[0], 404, f"GET /{name} must 404")
            eq(http_get(port, "/" + name, method="HEAD")[0], 404, f"HEAD /{name} must 404")
        eq(http_get(port, "/.SECRET")[0], 404, "case-varied dotfile refused")
    finally:
        httpd.shutdown()
        t.join(timeout=5)


@test("GET a dotfile or dot-directory path 404s (existing or not)")
def _():
    site = make_site()
    httpd, port, t = live(site, idle_enabled=True)
    try:
        eq(http_get(port, "/.secret")[0], 404, "existing dotfile refused")
        eq(http_get(port, "/.git/config")[0], 404, "dot-directory refused")
    finally:
        httpd.shutdown()
        t.join(timeout=5)


@test("HEAD honors the control-file guard and touches activity")
def _():
    site = make_site()
    httpd, port, t = live(site, idle_enabled=True)
    try:
        eq(http_get(port, "/index.html", method="HEAD")[0], 200, "HEAD index ok")
        eq(http_get(port, "/server.json", method="HEAD")[0], 404, "HEAD control file refused")
        time.sleep(0.05)
        before = httpd.idle_seconds()
        http_get(port, "/index.html", method="HEAD")
        assert httpd.idle_seconds() < before, "HEAD touched activity"
    finally:
        httpd.shutdown()
        t.join(timeout=5)


# --------------------------------------------------------------------------- #
# Watchdog: idle shutdown + TTL expiry (real run_server, tiny timings)
# --------------------------------------------------------------------------- #

@test("idle shutdown: stays up while pinged, then stops with reason 'idle'")
def _():
    site = make_site()
    th = threading.Thread(
        target=serve_site.run_server,
        args=(site, "127.0.0.1", 0, 0, 0.3),  # ttl 0 (disabled), idle 0.3s
        kwargs={"poll_interval": 0.05, "idle_grace": 0.3},
        daemon=True,
    )
    th.start()
    port = wait_for_port(site)
    # (a) heartbeats every 0.1s keep it alive well past the 0.3s idle threshold.
    deadline = time.time() + 1.0
    while time.time() < deadline:
        eq(http_post(port, "/api/heartbeat")[0], 200)
        time.sleep(0.1)
    assert th.is_alive(), "server must stay up while heartbeats arrive"
    # (b) stop pinging (and stop touching it): it shuts down after idle + grace.
    th.join(timeout=5)
    assert not th.is_alive(), "server should shut down after heartbeats stop"
    info = json.loads((site / "server.json").read_text(encoding="utf-8"))
    eq(info.get("shutdown_reason"), "idle", "server.json records the idle shutdown")
    assert info.get("stopped_at"), "stopped_at recorded"
    eq(info.get("idle_timeout_seconds"), 0.3, "idle timeout recorded in server.json")


@test("ttl expiry: stops with reason 'ttl'")
def _():
    site = make_site()
    th = threading.Thread(
        target=serve_site.run_server,
        args=(site, "127.0.0.1", 0, 1, 0),  # ttl 1s, idle disabled
        kwargs={"poll_interval": 0.05},
        daemon=True,
    )
    th.start()
    wait_for_port(site)
    th.join(timeout=5)
    assert not th.is_alive(), "server should shut down at TTL"
    info = json.loads((site / "server.json").read_text(encoding="utf-8"))
    eq(info.get("shutdown_reason"), "ttl", "server.json records the ttl shutdown")
    eq(info.get("ttl_seconds"), 1)


def main() -> int:
    print(f"serve lifecycle suite  ({len(TESTS)} tests)")
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
