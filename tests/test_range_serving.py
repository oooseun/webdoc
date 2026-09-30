#!/usr/bin/env python3
"""Tests for HTTP Range (byte-serving) support in serve_site.py's static handler.

Python's http.server has never implemented Range requests: a Range: GET gets a
200 with the *entire* file body, which every browser reads as "this server
can't do partial content" and permanently refuses to seek that media element,
even via a direct currentTime assignment. NoListingHandler._maybe_serve_range
adds the missing 206/416 behavior so <video>/<audio> embeds (see
review-player.js) are actually scrubbable. Pure stdlib. Run directly:

    python3 tests/test_range_serving.py
"""

from __future__ import annotations

import functools
import socket
import sys
import tempfile
import threading
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


PAYLOAD = bytes(range(256)) * 40  # 10240 bytes, every value distinguishable by position % 256


def make_site() -> Path:
    d = Path(tempfile.mkdtemp(prefix="webdoc-range-"))
    (d / "index.html").write_text("<html><body>hi</body></html>", encoding="utf-8")
    (d / "server.json").write_text("{}", encoding="utf-8")  # control file: must stay 404 even with Range
    assets = d / "assets"
    assets.mkdir()
    (assets / "clip.bin").write_bytes(PAYLOAD)
    return d


def live(site: Path):
    handler = functools.partial(serve_site.NoListingHandler, directory=str(site))
    httpd = serve_site.ThreadingHTTPServer(("127.0.0.1", 0), handler)
    port = int(httpd.server_address[1])
    t = threading.Thread(target=httpd.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True)
    t.start()
    return httpd, port, t


def http(port: int, path: str, method: str = "GET", range_header: str | None = None):
    req = urllib.request.Request(f"http://127.0.0.1:{port}{path}", method=method)
    if range_header is not None:
        req.add_header("Range", range_header)
    try:
        with urllib.request.urlopen(req, timeout=5) as r:
            return r.status, dict(r.headers), r.read()
    except urllib.error.HTTPError as e:
        return e.code, dict(e.headers), e.read()


def raw_request(sock: socket.socket, path: str, range_header: str | None = None):
    """Send one request directly on an already-open socket and read exactly
    one response off it, without using urllib/http.client (both silently
    reconnect per request, which would hide the exact bug this is checking
    for). Returns (status, headers, status_line, body).
    """
    lines = [f"GET {path} HTTP/1.1", "Host: 127.0.0.1", "Connection: keep-alive"]
    if range_header is not None:
        lines.append(f"Range: {range_header}")
    sock.sendall(("\r\n".join(lines) + "\r\n\r\n").encode("ascii"))

    buf = b""
    while b"\r\n\r\n" not in buf:
        chunk = sock.recv(4096)
        if not chunk:
            raise RuntimeError("connection closed while reading headers")
        buf += chunk
    head, _, body = buf.partition(b"\r\n\r\n")
    head_lines = head.decode("iso-8859-1").split("\r\n")
    status_line = head_lines[0]
    status = int(status_line.split(" ", 2)[1])
    headers: dict[str, str] = {}
    for line in head_lines[1:]:
        if ":" in line:
            k, _, v = line.partition(":")
            headers[k.strip().lower()] = v.strip()
    # HTTP/1.1 frames a response body by Content-Length (or chunking, which this
    # server never uses); without either, a client reads the body until the
    # server closes the connection, so a keep-alive client would hang on it.
    # 1xx, 204 and 304 are bodiless by definition.
    if "content-length" not in headers and not (100 <= status < 200 or status in (204, 304)):
        raise AssertionError(f"{status_line!r} has no Content-Length: a keep-alive client would wait for close")
    length = int(headers.get("content-length", "0"))
    while len(body) < length:
        chunk = sock.recv(4096)
        if not chunk:
            break
        body += chunk
    return status, headers, status_line, body


@test("GET with a mid-file byte range returns 206 with the exact slice")
def _():
    site = make_site()
    httpd, port, t = live(site)
    try:
        status, headers, body = http(port, "/assets/clip.bin", range_header="bytes=10-19")
        eq(status, 206, "partial content")
        eq(headers.get("Content-Range"), f"bytes 10-19/{len(PAYLOAD)}")
        eq(headers.get("Content-Length"), "10")
        eq(headers.get("Accept-Ranges"), "bytes")
        eq(body, PAYLOAD[10:20], "exact byte slice")
    finally:
        httpd.shutdown()
        t.join(timeout=2)


@test("GET with an open-ended range (bytes=N-) returns 206 through EOF")
def _():
    site = make_site()
    httpd, port, t = live(site)
    try:
        start = len(PAYLOAD) - 25
        status, headers, body = http(port, "/assets/clip.bin", range_header=f"bytes={start}-")
        eq(status, 206)
        eq(headers.get("Content-Range"), f"bytes {start}-{len(PAYLOAD) - 1}/{len(PAYLOAD)}")
        eq(body, PAYLOAD[start:], "runs to end of file")
    finally:
        httpd.shutdown()
        t.join(timeout=2)


@test("GET with a suffix range (bytes=-N) returns the last N bytes")
def _():
    site = make_site()
    httpd, port, t = live(site)
    try:
        status, headers, body = http(port, "/assets/clip.bin", range_header="bytes=-30")
        eq(status, 206)
        want_start = len(PAYLOAD) - 30
        eq(headers.get("Content-Range"), f"bytes {want_start}-{len(PAYLOAD) - 1}/{len(PAYLOAD)}")
        eq(body, PAYLOAD[-30:], "last 30 bytes")
    finally:
        httpd.shutdown()
        t.join(timeout=2)


@test("HEAD with a byte range returns 206 headers and no body")
def _():
    site = make_site()
    httpd, port, t = live(site)
    try:
        status, headers, body = http(port, "/assets/clip.bin", method="HEAD", range_header="bytes=0-9")
        eq(status, 206)
        eq(headers.get("Content-Length"), "10")
        eq(body, b"", "HEAD carries no body")
    finally:
        httpd.shutdown()
        t.join(timeout=2)


@test("an unsatisfiable range (start beyond EOF) is 416 with Content-Range: bytes */size")
def _():
    site = make_site()
    httpd, port, t = live(site)
    try:
        status, headers, body = http(port, "/assets/clip.bin", range_header=f"bytes={len(PAYLOAD) + 100}-")
        eq(status, 416)
        eq(headers.get("Content-Range"), f"bytes */{len(PAYLOAD)}")
    finally:
        httpd.shutdown()
        t.join(timeout=2)


@test("a malformed Range header is ignored: falls back to a normal full 200")
def _():
    site = make_site()
    httpd, port, t = live(site)
    try:
        status, headers, body = http(port, "/assets/clip.bin", range_header="bytes=not-a-range")
        eq(status, 200, "malformed Range is not an error, just ignored")
        eq(body, PAYLOAD, "full file served")
    finally:
        httpd.shutdown()
        t.join(timeout=2)


@test("GET without a Range header still serves the full file at 200 (no regression)")
def _():
    site = make_site()
    httpd, port, t = live(site)
    try:
        status, headers, body = http(port, "/assets/clip.bin")
        eq(status, 200)
        eq(body, PAYLOAD)
    finally:
        httpd.shutdown()
        t.join(timeout=2)


@test("a Range request for a control file still 404s (forbidden-path guard runs first)")
def _():
    site = make_site()
    httpd, port, t = live(site)
    try:
        status, headers, body = http(port, "/server.json", range_header="bytes=0-5")
        eq(status, 404, "control-file guard takes priority over range serving")
    finally:
        httpd.shutdown()
        t.join(timeout=2)


@test("a Range request for a nonexistent file 404s, not a range error")
def _():
    site = make_site()
    httpd, port, t = live(site)
    try:
        status, headers, body = http(port, "/assets/missing.bin", range_header="bytes=0-5")
        eq(status, 404)
    finally:
        httpd.shutdown()
        t.join(timeout=2)


@test("HTTP/1.1 keep-alive: two sequential range requests succeed on one TCP connection")
def _():
    # Python's http.server defaults to HTTP/1.0 unless protocol_version is
    # explicitly raised on the handler class, and in that default state the
    # server closes the TCP connection after every single response no matter
    # what the client sent, forcing a brand-new (and, behind tailscale serve,
    # freshly re-proxied) connection for every Range request a video player
    # makes while scrubbing. NoListingHandler sets protocol_version =
    # "HTTP/1.1" specifically so one connection can serve many requests.
    # urllib/http.client silently open a fresh connection per call, which
    # would hide this exact regression, so this drives a raw socket instead.
    site = make_site()
    httpd, port, t = live(site)
    try:
        sock = socket.create_connection(("127.0.0.1", port), timeout=5)
        try:
            status1, headers1, line1, body1 = raw_request(sock, "/assets/clip.bin", "bytes=0-9")
            eq(status1, 206, "first request on the connection")
            assert line1.startswith("HTTP/1.1"), f"server must declare HTTP/1.1: {line1!r}"
            assert headers1.get("connection", "").lower() != "close", "must not announce an immediate close"
            eq(body1, PAYLOAD[:10])

            # The actual regression check: reuse the SAME socket for a second,
            # independent request. Under the old HTTP/1.0 default the server
            # would already have closed its end after the first response, and
            # sendall/recv here would raise or return an empty read instead of
            # a second real response.
            status2, headers2, line2, body2 = raw_request(sock, "/assets/clip.bin", "bytes=10-19")
            eq(status2, 206, "second request reused the connection instead of needing a new one")
            eq(body2, PAYLOAD[10:20])

            # And a third, non-range request, to prove reuse isn't range-specific.
            status3, headers3, line3, body3 = raw_request(sock, "/assets/clip.bin")
            eq(status3, 200)
            eq(body3, PAYLOAD)
        finally:
            sock.close()
    finally:
        httpd.shutdown()
        t.join(timeout=2)


@test("a 416 is framed (Content-Length: 0) so the keep-alive connection serves the next request")
def _():
    # A player whose cached length is stale (the file behind the URL was
    # re-rendered shorter) asks for a range past the new EOF. Under HTTP/1.1
    # a 416 without Content-Length leaves the client reading an unframed body
    # until the connection's idle timeout closes it.
    site = make_site()
    httpd, port, t = live(site)
    try:
        sock = socket.create_connection(("127.0.0.1", port), timeout=5)
        try:
            status1, headers1, _, body1 = raw_request(sock, "/assets/clip.bin", f"bytes={len(PAYLOAD) + 100}-")
            eq(status1, 416)
            eq(headers1.get("content-length"), "0", "416 must be framed")
            eq(headers1.get("content-range"), f"bytes */{len(PAYLOAD)}")
            status2, _, _, body2 = raw_request(sock, "/assets/clip.bin", "bytes=0-9")
            eq(status2, 206, "the same connection serves the next request")
            eq(body2, PAYLOAD[:10])
        finally:
            sock.close()
    finally:
        httpd.shutdown()
        t.join(timeout=2)


@test("a plain HTTP/1.0 request (no keep-alive) still gets a correct response")
def _():
    # Regression guard for the HTTP/1.1 upgrade above: an older client that
    # only speaks HTTP/1.0 and never asks for a persistent connection must
    # still get a normal, correct, complete response.
    site = make_site()
    httpd, port, t = live(site)
    try:
        sock = socket.create_connection(("127.0.0.1", port), timeout=5)
        try:
            sock.sendall(b"GET /assets/clip.bin HTTP/1.0\r\n\r\n")
            buf = b""
            while b"\r\n\r\n" not in buf:
                chunk = sock.recv(4096)
                if not chunk:
                    break
                buf += chunk
            head, _, body = buf.partition(b"\r\n\r\n")
            status_line = head.decode("iso-8859-1").split("\r\n")[0]
            eq(status_line.split(" ")[1], "200")
            while len(body) < len(PAYLOAD):
                chunk = sock.recv(4096)
                if not chunk:
                    break
                body += chunk
            eq(body, PAYLOAD)
        finally:
            sock.close()
    finally:
        httpd.shutdown()
        t.join(timeout=2)


def main() -> int:
    print(f"range-serving suite  ({len(TESTS)} tests)")
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
