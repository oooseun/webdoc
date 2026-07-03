#!/usr/bin/env python3
"""Tests for the serve_site.py editing write-guard (the /api/edit gate).

The write path mutates the canonical source file, so it must be reachable only
from the local machine. The Host header is client-supplied (spoofable under
--allow-lan), so the authoritative check is the real TCP peer address. These
tests exercise that gate without standing up a live socket.

Pure stdlib. Run directly:

    python3 tests/test_editmode_serve.py
"""

from __future__ import annotations

import io
import json
import sys
import tempfile
import traceback
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


def make_handler(peer: str, host: str):
    """A NoListingHandler with just enough state to run the /api/edit guard.

    Built via __new__ so no socket/HTTP machinery is touched; send_json is
    captured instead of written to a client."""
    h = serve_site.NoListingHandler.__new__(serve_site.NoListingHandler)
    h.client_address = (peer, 5555)
    h.headers = {"Host": host}
    h.captured = {}

    def send_json(status, payload):
        h.captured = {"status": status, "payload": payload}

    h.send_json = send_json
    return h


@test("addr_is_loopback: loopback addresses true, LAN/empty false")
def _():
    for ok in ("127.0.0.1", "127.5.6.7", "::1", "::ffff:127.0.0.1"):
        assert serve_site.addr_is_loopback(ok), ok
    for bad in ("192.168.1.50", "10.0.0.1", "::ffff:192.168.1.50", "8.8.8.8", "", "garbage"):
        assert not serve_site.addr_is_loopback(bad), bad


@test("/api/edit rejects a non-loopback peer even with a spoofed loopback Host")
def _():
    h = make_handler("192.168.1.50", "127.0.0.1:8000")
    h.handle_edit()
    assert h.captured.get("status") == 403, h.captured
    assert h.captured["payload"].get("error") == "loopback_only", h.captured


@test("/api/edit rejects a loopback peer with a non-loopback Host (DNS rebinding)")
def _():
    h = make_handler("127.0.0.1", "evil.example.com")
    h.handle_edit()
    assert h.captured.get("status") == 403, h.captured


@test("/api/edit rejects an IPv4-mapped LAN peer")
def _():
    h = make_handler("::ffff:10.0.0.9", "127.0.0.1:8000")
    h.handle_edit()
    assert h.captured.get("status") == 403, h.captured


@test("/api/undo is loopback-guarded too (non-loopback peer -> 403)")
def _():
    # Undo also mutates the source file, so it must carry the same write-guard.
    h = make_handler("192.168.1.50", "127.0.0.1:8000")
    h.handle_undo()
    assert h.captured.get("status") == 403, h.captured
    assert h.captured["payload"].get("error") == "loopback_only", h.captured


@test("/api/undo rejects a loopback peer with a non-loopback Host (DNS rebinding)")
def _():
    h = make_handler("127.0.0.1", "evil.example.com")
    h.handle_undo()
    assert h.captured.get("status") == 403, h.captured


def make_post_handler(payload: dict, site: Path):
    """A handler carrying a JSON body and a real (temp) site dir, so the
    feedback/annotation/audit paths run end-to-end minus the socket."""
    body = json.dumps(payload).encode("utf-8")
    h = serve_site.NoListingHandler.__new__(serve_site.NoListingHandler)
    h.client_address = ("127.0.0.1", 5555)
    h.headers = {"Host": "127.0.0.1:8000", "content-length": str(len(body)), "user-agent": "test"}
    h.rfile = io.BytesIO(body)
    h.site_dir = lambda: site
    h.captured = {}

    def send_json(status, payload):
        h.captured = {"status": status, "payload": payload}

    h.send_json = send_json
    return h


def read_entries(site: Path) -> list[dict]:
    path = serve_site.feedback_jsonl(site)
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


@test("/api/version: render-time meta stamp first, mtime fallback, '0' when missing")
def _():
    site = Path(tempfile.mkdtemp(prefix="webdoc-serve-test-"))
    h = make_post_handler({}, site)
    assert h._site_version() == "0", "no index.html yet"
    # A page built before the stamp existed: fall back to mtime_ns.
    (site / "index.html").write_text("<html>old build</html>", encoding="utf-8")
    v_fallback = h._site_version()
    assert v_fallback == str((site / "index.html").stat().st_mtime_ns), v_fallback
    # A stamped page: the meta content IS the version, exactly what the client
    # seeded its baseline from (no first-poll startup window).
    (site / "index.html").write_text(
        '<html><head><meta name="webdoc-version" content="1234567890123456789"></head></html>',
        encoding="utf-8")
    assert h._site_version() == "1234567890123456789", h._site_version()


@test("annotations: a batch lands as ONE feedback entry, items sanitized")
def _():
    site = Path(tempfile.mkdtemp(prefix="webdoc-serve-test-"))
    h = make_post_handler({
        "kind": "annotations",
        "page": "/",
        "items": [
            {"comment": "tighten this paragraph",
             "block": {"type": "paragraph", "start": 3, "end": 3, "hash": "abc123def456"},
             "excerpt": "Some paragraph text.",
             "selected": {"text": "this paragraph"},
             "queued_at": "2026-07-03T10:00:00Z"},
            {"comment": "   "},                     # blank comment: dropped
            "junk",                                  # non-dict: dropped
            {"comment": "x" * 5000,                  # comment capped at 2000
             "block": {"type": "tablecell", "line": 9, "cell": 2, "hash": "fff", "evil": "x"}},
        ],
    }, site)
    h.handle_feedback()
    assert h.captured.get("status") == 200, h.captured
    assert h.captured["payload"]["count"] == 2, h.captured
    entries = read_entries(site)
    assert len(entries) == 1, "one batch entry per send, not one per annotation"
    entry = entries[0]
    assert entry["kind"] == "annotations" and entry["count"] == 2, entry
    assert entry["items"][0]["block"] == {"type": "paragraph", "start": 3, "end": 3, "hash": "abc123def456"}
    assert entry["items"][0]["selected"] == "this paragraph"
    assert len(entry["items"][1]["comment"]) == 2000, "comment capped"
    assert "evil" not in entry["items"][1]["block"], "unknown block keys stripped"


@test("annotations: an empty or all-blank batch is rejected, nothing written")
def _():
    site = Path(tempfile.mkdtemp(prefix="webdoc-serve-test-"))
    for items in ([], [{"comment": "  "}], "nope"):
        h = make_post_handler({"kind": "annotations", "items": items}, site)
        h.handle_feedback()
        assert h.captured.get("status") == 400, (items, h.captured)
        assert h.captured["payload"]["error"] == "empty_annotations", h.captured
    assert read_entries(site) == [], "nothing written"


@test("plain feedback still works unchanged next to the annotations branch")
def _():
    site = Path(tempfile.mkdtemp(prefix="webdoc-serve-test-"))
    h = make_post_handler({"feedback": "looks good", "page": "/"}, site)
    h.handle_feedback()
    assert h.captured.get("status") == 200, h.captured
    entries = read_entries(site)
    assert len(entries) == 1 and entries[0]["feedback"] == "looks good", entries


@test("/api/audit: findings entry with error_count; junk findings filtered")
def _():
    site = Path(tempfile.mkdtemp(prefix="webdoc-serve-test-"))
    h = make_post_handler({
        "page": "/",
        "viewport": {"w": 1200, "h": 800},
        "findings": [
            {"kind": "clipped-text", "severity": "error", "selector": "main > p:nth-of-type(2)",
             "overflowPx": 12.34, "viewportWidth": 1200,
             "block": {"type": "paragraph", "start": 5, "end": 5}},
            {"kind": "overlapping-text", "severity": "warning", "selector": "main > p", "persistent": True},
            {"kind": "x", "severity": "catastrophic"},   # bad severity: dropped
            {"severity": "error"},                        # no kind: dropped
            17,                                           # non-dict: dropped
        ],
    }, site)
    h.handle_audit()
    assert h.captured.get("status") == 200, h.captured
    entries = read_entries(site)
    assert len(entries) == 1, entries
    entry = entries[0]
    assert entry["kind"] == "layout_warnings", entry
    assert entry["count"] == 2 and entry["error_count"] == 1, entry
    assert entry["findings"][0]["overflowPx"] == 12.3, "rounded to 0.1px"
    assert entry["findings"][1]["persistent"] is True, entry


@test("/api/audit: an empty findings list is the all-clear (recorded, count 0)")
def _():
    site = Path(tempfile.mkdtemp(prefix="webdoc-serve-test-"))
    h = make_post_handler({"findings": [], "page": "/"}, site)
    h.handle_audit()
    assert h.captured.get("status") == 200, h.captured
    entries = read_entries(site)
    assert len(entries) == 1 and entries[0]["count"] == 0 and entries[0]["error_count"] == 0, entries
    h2 = make_post_handler({"findings": "nope"}, site)
    h2.handle_audit()
    assert h2.captured.get("status") == 400, h2.captured


def main() -> int:
    print(f"editmode serve-guard suite  ({len(TESTS)} tests)")
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
