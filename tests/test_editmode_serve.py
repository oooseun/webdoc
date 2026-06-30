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

import sys
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
